#!/usr/bin/env python3
"""
Self-Correction Engine for a console-based GPT-4o-mini chatbot.

ARCHITECTURE NOTE (2026 Refactor):
  This script can be run in two modes:

  1. STANDALONE MODE  — run directly: `python self_correction_chatbot.py`
     Owns its own input loop, calls OpenRouter directly via `requests`.

  2. CORRECTION-COMMAND MODE — invoked by the C++ executable via a
     `--wrong` flag after the user types /wrong in the terminal.
     Reads `runtime_state.json` (written by api.py after every successful
     turn) to extract the previous query/response, then writes the
     correction into `corrections.json`.

     C++ integration flow:
       chatbot.exe → api.py (every turn, writes runtime_state.json)
       chatbot.exe → self_correction_chatbot.py --wrong  (on /wrong command)

STATE FILES  (all anchored to the PROJECT ROOT — D:\\AI_Chatbot_Project\\):
  runtime_state.json  — last successful query + bot response (written by api.py)
  corrections.json    — cumulative correction registry (written by this script)

PATH RESOLUTION:
  Both this script and api.py use an identical anchor: if __file__ resolves
  inside a directory named 'build', navigate one level up to the project root.
  This guarantees both scripts always share the same BASE_DIR regardless of
  which copy C++ invokes.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure the project root is importable even when the process is launched
# from the build/ subdirectory (for example by the compiled C++ binary).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR) if os.path.basename(_SCRIPT_DIR).lower() == "build" else _SCRIPT_DIR
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from correction_utils import atomic_write_json, build_negative_constraint, compute_match_score, extract_keywords, keyword_match

# ---------------------------------------------------------------------------
# Unified Project Root Anchor
# ---------------------------------------------------------------------------
# If this script is executed from the build/ output directory (as C++ does),
# navigate one level up to reach the project root where all state files live.
# This exact logic is mirrored in api.py so both scripts always share the
# same PROJECT_ROOT regardless of which directory C++ happens to run from.
# ---------------------------------------------------------------------------
_current_dir = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_current_dir).lower() == "build":
    PROJECT_ROOT = os.path.dirname(_current_dir)
else:
    PROJECT_ROOT = _current_dir

BASE_DIR         = PROJECT_ROOT          # alias kept for internal compatibility
CORRECTIONS_FILE = os.path.join(PROJECT_ROOT, "corrections.json")
RUNTIME_STATE_FILE = os.path.join(PROJECT_ROOT, "runtime_state.json")

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

BASE_PATH = Path(BASE_DIR)  # Path-typed alias used by classes below

# ---------------------------------------------------------------------------
# Logging — writes to the project root, never to the CWD of the caller.
# ---------------------------------------------------------------------------
_LOG_PATH = BASE_PATH / "self_correction.log"
logging.basicConfig(
    filename=str(_LOG_PATH),
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    encoding="utf-8",
)
log = logging.getLogger("SelfCorrectionChatbot")

# File paths — all resolved against BASE_DIR, never relative to CWD.
CORRECTIONS_PATH:    Path = Path(CORRECTIONS_FILE)
RUNTIME_STATE_PATH:  Path = Path(RUNTIME_STATE_FILE)


# ===========================================================================
# CorrectionEngine
# ===========================================================================

class CorrectionEngine:
    """
    Manages fuzzy correction lookups and persistent correction storage.

    corrections.json schema:
      { 
        "<user query>": {
          "response": "<wrong bot response>",
          "timestamp": "<iso timestamp>",
          "severity": "<low|medium|high>"
        }, 
        ... 
      }

    The value stored contains the REJECTED response. The negative constraint
    injected into the API payload is built from this value to steer the
    model away from it.
    """

    def __init__(self, registry_path: Path | None = None) -> None:
        # Always anchored to BASE_DIR unless an explicit override is given.
        self.registry_path: Path = registry_path or CORRECTIONS_PATH
        log.debug("[CorrectionEngine] registry_path resolved to: %s", self.registry_path)
        self.corrections: dict[str, dict] = self._load_registry()

    # -----------------------------------------------------------------------
    # Registry I/O
    # -----------------------------------------------------------------------

    def _load_registry(self) -> dict[str, dict]:
        log.debug("[CorrectionEngine] Loading registry from: %s", self.registry_path)
        if not self.registry_path.exists():
            log.info("[CorrectionEngine] No registry file found — starting with empty registry.")
            return {}

        try:
            with self.registry_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            log.warning("[CorrectionEngine] Registry file disappeared between exists() and open().")
            return {}
        except json.JSONDecodeError as exc:
            log.error("[CorrectionEngine] Registry JSON is malformed: %s — using empty registry.", exc)
            print("[SYSTEM LOG] Existing corrections registry is invalid JSON; using an empty registry.")
            return {}
        except OSError as exc:
            log.error("[CorrectionEngine] Cannot read registry: %s", exc)
            print(f"[SYSTEM LOG] Unable to read corrections registry: {exc}")
            return {}

        # Support both dict format and legacy list-of-objects format.
        if isinstance(payload, dict):
            normalized: dict[str, dict] = {}
            for k, v in payload.items():
                if not isinstance(k, str):
                    continue
                if isinstance(v, str):
                    normalized[k.strip()] = {
                        "response": v.strip(),
                        "timestamp": "N/A",
                        "severity": "medium"
                    }
                elif isinstance(v, dict):
                    res_val = v.get("response")
                    if isinstance(res_val, str):
                        normalized[k.strip()] = {
                            "response": res_val.strip(),
                            "timestamp": v.get("timestamp", "N/A"),
                            "severity": v.get("severity", "medium")
                        }
            log.info("[CorrectionEngine] Loaded %d corrections (dict format).", len(normalized))
            return normalized

        if isinstance(payload, list):
            normalized = {}
            for entry in payload:
                if isinstance(entry, dict):
                    q = entry.get("query")
                    r = entry.get("response")
                    if isinstance(q, str):
                        if isinstance(r, str):
                            normalized[q.strip()] = {
                                "response": r.strip(),
                                "timestamp": "N/A",
                                "severity": "medium"
                            }
                        elif isinstance(r, dict):
                            res_val = r.get("response")
                            if isinstance(res_val, str):
                                normalized[q.strip()] = {
                                    "response": res_val.strip(),
                                    "timestamp": r.get("timestamp", "N/A"),
                                    "severity": r.get("severity", "medium")
                                }
            log.info("[CorrectionEngine] Loaded %d corrections (list format).", len(normalized))
            return normalized

        log.warning("[CorrectionEngine] Unknown registry format — using empty registry.")
        print("[SYSTEM LOG] Unsupported corrections registry format; using an empty registry.")
        return {}

    def _save_registry(self) -> None:
        try:
            atomic_write_json(self.registry_path, self.corrections)
            log.debug("[CorrectionEngine] Registry saved — %d entries.", len(self.corrections))
        except OSError as exc:
            log.error("[CorrectionEngine] Cannot save registry: %s", exc)
            print(f"[SYSTEM LOG] Unable to save corrections registry: {exc}")

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def record_correction(self, user_query: str, bot_response: str, severity: str = "medium") -> None:
        """
        Record that `bot_response` was wrong for `user_query`.
        The stored value IS the bad response object — used later to build the
        negative constraint that tells the model what NOT to repeat.
        """
        if not user_query.strip() or not bot_response.strip():
            log.warning("[CorrectionEngine] record_correction called with empty query or response.")
            print("[SYSTEM LOG] No valid exchange was available to save.")
            return

        normalized_query = user_query.strip()
        self.corrections[normalized_query] = {
            "response": bot_response.strip(),
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "severity": severity
        }
        self._save_registry()
        log.info("[CorrectionEngine] Correction saved for query: '%s' with severity %s", normalized_query, severity)

    def find_fuzzy_match(self, user_query: str) -> Optional[tuple[str, str]]:
        """
        Dual-layer keyword + fuzzy interceptor.
        1. Keyword Trigger: matches core keywords/nouns.
        2. Fuzzy Fallback: SequenceMatcher ratio >= 0.40.
        Returns (matched_query, wrong_bot_response) or None.
        """
        stripped = user_query.strip()
        if not stripped or not self.corrections:
            return None

        # 1. Keyword Trigger
        for known_query, known_data in self.corrections.items():
            known_res = known_data if isinstance(known_data, str) else known_data.get("response", "")
            if not known_query.strip() or not known_res.strip():
                continue
            curr_keywords = extract_keywords(stripped)
            known_keywords = extract_keywords(known_query)
            if curr_keywords and known_keywords and keyword_match(curr_keywords, known_keywords):
                log.info("[CorrectionEngine] Keyword match hit: current keywords %s matched known keywords %s (known query: '%s')",
                         curr_keywords, known_keywords, known_query)
                return known_query, known_res

        # 2. Fuzzy Fallback with stricter threshold and semantic guard
        best_key: Optional[str] = None
        best_response: Optional[str] = None
        best_score = 0.0
        for known_query, known_data in self.corrections.items():
            known_res = known_data if isinstance(known_data, str) else known_data.get("response", "")
            if not known_query.strip() or not known_res.strip():
                continue
            score = compute_match_score(stripped, known_query)
            if score >= 0.86 and score > best_score:
                best_score = score
                best_key = known_query
                best_response = known_res

        if best_key and best_response:
            log.info("[CorrectionEngine] Fuzzy match fallback hit (score=%.4f): '%s' vs known '%s'",
                     best_score, stripped, best_key)
            return best_key, best_response

        return None

    def build_negative_constraint(self, blocked_response: str) -> str:
        """Build the ultimate strict prompt override constraint."""
        return build_negative_constraint(blocked_response)


# ===========================================================================
# RuntimeStateManager
# ===========================================================================

class RuntimeStateManager:
    """
    Reads and writes runtime_state.json — the disk-persistent store for
    the most recently completed query/response pair.

    This file is written by api.py after every successful API call so that
    a subsequent /wrong command can always recover the previous exchange,
    even across process restarts.

    Schema:
      {
        "user_query":       "<string>",
        "chatbot_response": "<string>"
      }
    """

    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path: Path = state_path or RUNTIME_STATE_PATH
        log.debug("[RuntimeState] state_path resolved to: %s", self.state_path)

    def load(self) -> tuple[Optional[str], Optional[str]]:
        """
        Read the persisted last query and response.
        Returns (last_user_query, last_bot_response) or (None, None) on any error.
        """
        if not self.state_path.exists():
            log.info("[RuntimeState] No runtime_state.json found at %s.", self.state_path)
            return None, None

        try:
            with self.state_path.open("r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            log.error("[RuntimeState] Cannot read state file: %s", exc)
            return None, None

        query    = state.get("user_query")
        response = state.get("chatbot_response")

        if not isinstance(query, str) or not isinstance(response, str):
            query    = state.get("last_user_query")
            response = state.get("last_bot_response")

        if not isinstance(query, str) or not isinstance(response, str):
            log.warning("[RuntimeState] State file has unexpected types: %s", state)
            return None, None

        log.info(
            "[RuntimeState] Loaded — query: '%s...' response: '%s...'",
            query[:60], response[:60],
        )
        return query, response

    def save(self, user_query: str, bot_response: str) -> None:
        """
        Persist the current turn's query and response to disk.
        Called by api.py (and the standalone loop) after every successful turn.
        """
        state = {
            "user_query":       user_query.strip(),
            "chatbot_response": bot_response.strip(),
        }
        try:
            atomic_write_json(self.state_path, state)
            log.info("[RuntimeState] Saved runtime state for query: '%s...'", user_query[:60])
        except OSError as exc:
            log.error("[RuntimeState] Cannot write state file: %s", exc)
            print(f"[SYSTEM LOG] Warning: Could not persist runtime state: {exc}")


# ===========================================================================
# WrongCommandHandler  (used when invoked by C++ via --wrong flag)
# ===========================================================================

def handle_wrong_command(severity: str = "medium") -> int:
    """
    Entry point for the --wrong flag mode.

    Reads runtime_state.json to recover the previous turn's exchange, then
    pushes it into corrections.json. This is the production path when C++
    invokes: `python self_correction_chatbot.py --wrong --severity <severity>`

    Returns 0 on success, 1 on failure (propagated to C++ as exit code).
    """
    log.info("[WrongHandler] --wrong command handler invoked by C++ runtime with severity %s.", severity)

    state_mgr  = RuntimeStateManager()
    engine     = CorrectionEngine()

    last_query, last_response = state_mgr.load()

    if not last_query or not last_response:
        msg = (
            "[SYSTEM LOG] No previous exchange found in runtime_state.json. "
            "Either this is the first turn or the state file was not written by api.py."
        )
        log.warning("[WrongHandler] %s", msg)
        print(msg)
        return 1

    log.info(
        "[WrongHandler] Registering correction — query: '%s...' response: '%s...' severity: %s",
        last_query[:60], last_response[:60], severity
    )
    engine.record_correction(last_query, last_response, severity)
    print("Status                     : Command accepted successfully.")
    return 0


# ===========================================================================
# ChatbotApp  (standalone mode — direct Python execution only)
# ===========================================================================

class ChatbotApp:
    """
    High-level console chatbot with correction-aware routing.

    This class is used when running self_correction_chatbot.py DIRECTLY
    (not through chatbot.exe). In this mode, the correction engine is
    consulted before every API call and the runtime state is persisted to
    disk after every successful turn.
    """

    DEFAULT_SYSTEM_PROMPT = (
        "You are a helpful assistant. "
        "Answer concisely, accurately, and safely."
    )

    def __init__(
        self,
        api_key:   Optional[str] = None,
        model:     str = "openai/gpt-4o-mini",
    ) -> None:
        self.api_key    = api_key or os.getenv("OPENROUTER_KEY")
        self.model      = model

        # Both anchored via BASE_DIR inside their constructors.
        self.correction_engine = CorrectionEngine()
        self.state_manager     = RuntimeStateManager()

        # In-memory mirrors — also persisted to runtime_state.json after each turn.
        self.last_user_query:   Optional[str] = None
        self.last_bot_response: Optional[str] = None

        self.conversation_history: list[dict[str, str]] = []

        log.info(
            "[ChatbotApp] Initialised — model=%s, corrections_path=%s, state_path=%s",
            self.model,
            self.correction_engine.registry_path,
            self.state_manager.state_path,
        )

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    def run(self) -> None:
        print("[SYSTEM LOG] Self-Correction Chatbot ready (standalone mode). Type /exit to quit.")
        print(f"[SYSTEM LOG] BASE_DIR      : {BASE_DIR}")
        print(f"[SYSTEM LOG] corrections   : {self.correction_engine.registry_path}")
        print(f"[SYSTEM LOG] runtime_state : {self.state_manager.state_path}")

        if not self.api_key:
            print(
                "[SYSTEM LOG] OPENROUTER_KEY is not set; "
                "API calls will be unavailable until configured."
            )

        while True:
            try:
                raw_input = input("You: ").strip()
            except EOFError:
                print("\n[SYSTEM LOG] Input stream closed. Exiting.")
                break
            except KeyboardInterrupt:
                print("\n[SYSTEM LOG] Interrupted. Exiting.")
                break

            if not raw_input:
                continue

            if raw_input == "/exit":
                print("[SYSTEM LOG] Goodbye.")
                break

            if raw_input == "/help":
                self._print_help()
                continue

            if raw_input == "/wrong":
                self._handle_wrong_command()
                continue

            if raw_input == "/state":
                self._print_state()
                continue

            response = self._handle_query(raw_input)
            print(f"Bot: {response}")

    # -----------------------------------------------------------------------
    # Command handlers
    # -----------------------------------------------------------------------

    def _print_help(self) -> None:
        print(
            "[SYSTEM LOG] Available commands:\n"
            "  /help   — show this menu\n"
            "  /wrong  — mark the last response as wrong (writes to corrections.json)\n"
            "  /state  — show the persisted runtime state\n"
            "  /exit   — quit"
        )

    def _handle_wrong_command(self) -> None:
        """
        In standalone mode: use in-memory state first, then fall back to
        the persisted runtime_state.json so the command survives restarts.
        """
        query    = self.last_user_query
        response = self.last_bot_response

        if not query or not response:
            # Attempt disk fallback (e.g., script was restarted after C++ turn)
            log.info("[ChatbotApp] In-memory state missing — falling back to runtime_state.json.")
            query, response = self.state_manager.load()

        if not query or not response:
            msg = "[SYSTEM LOG] No previous exchange is available to register."
            log.warning("[ChatbotApp] %s", msg)
            print(msg)
            return

        self.correction_engine.record_correction(query, response, "medium")
        print("Status                     : Command accepted successfully.")
        return 0

    def _print_state(self) -> None:
        q, r = self.state_manager.load()
        r_preview = (r[:120] + "...") if r and len(r) > 120 else r
        print(
            f"\n[RUNTIME STATE] (from {self.state_manager.state_path})\n"
            f"  last_user_query:   {q!r}\n"
            f"  last_bot_response: {r_preview!r}\n"
        )

    # -----------------------------------------------------------------------
    # Query routing — fuzzy match → constraint injection → API call
    # -----------------------------------------------------------------------

    def _handle_query(self, user_query: str) -> str:
        self.last_user_query = user_query

        if not self.api_key:
            response = "API access is unavailable because OPENROUTER_KEY is not configured."
            self.last_bot_response = response
            return response

        # ----------------------------------------------------------------
        # PHASE 1: Fuzzy Match Lookup
        # Consult the corrections registry BEFORE compiling the API payload.
        # ----------------------------------------------------------------
        constraint: Optional[str] = None
        match = self.correction_engine.find_fuzzy_match(user_query)
        if match:
            known_query, blocked_response = match
            log.info(
                "[ChatbotApp] Fuzzy match hit — known_query='%s', building negative constraint.",
                known_query,
            )
            constraint = self.correction_engine.build_negative_constraint(blocked_response)
        else:
            log.debug("[ChatbotApp] No fuzzy match — sending clean payload for: '%s'", user_query)

        # ----------------------------------------------------------------
        # PHASE 2: API call with constraint injected into system message
        # ----------------------------------------------------------------
        response_text = self._call_openrouter(user_query, constraint)

        # ----------------------------------------------------------------
        # PHASE 3: Persist state to disk AFTER every successful turn.
        # This is what makes /wrong work even after a restart.
        # ----------------------------------------------------------------
        self.last_bot_response = response_text
        self.state_manager.save(user_query, response_text)

        return response_text

    # -----------------------------------------------------------------------
    # System prompt builder
    # -----------------------------------------------------------------------

    def get_system_message(self, constraint: Optional[str] = None) -> str:
        """
        Build the final system prompt string.
        The negative constraint is appended AFTER the base prompt so it
        takes priority in the model's attention window.
        """
        if constraint:
            return f"{self.DEFAULT_SYSTEM_PROMPT}\n\n{constraint}"
        return self.DEFAULT_SYSTEM_PROMPT

    # -----------------------------------------------------------------------
    # OpenRouter API call
    # -----------------------------------------------------------------------

    def _call_openrouter(
        self,
        user_query: str,
        constraint: Optional[str] = None,
    ) -> str:
        if requests is None:
            return "The requests library is not installed."

        system_prompt = self.get_system_message(constraint)
        log.debug("[ChatbotApp._call_openrouter] System prompt length: %d chars", len(system_prompt))
        if constraint:
            log.debug("[ChatbotApp._call_openrouter] Negative constraint IS present in payload.")
            print(f"[API PAYLOAD] system prompt length: {len(system_prompt)} chars (constraint included)")
        else:
            print(f"[API PAYLOAD] system prompt length: {len(system_prompt)} chars (no constraint)")

        # Build the messages array with the (possibly constraint-injected) system prompt.
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_query},
        ]
        log.debug("[ChatbotApp._call_openrouter] messages array: %s", json.dumps(messages, indent=2))

        payload = {
            "model":       self.model,
            "messages":    messages,
            "temperature": 0.2,
        }

        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type":  "application/json",
                },
                json=payload,
                timeout=(10, 60),
            )
            response.raise_for_status()
            result = response.json()
        except requests.exceptions.Timeout:
            log.error("[ChatbotApp] API request timed out.")
            return "Error: API Request Timed Out."
        except requests.exceptions.ConnectionError:
            log.error("[ChatbotApp] Network connection failed.")
            return "Error: Network connection failed."
        except requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response else "unknown"
            log.error("[ChatbotApp] HTTP error %s.", status_code)
            return f"Error: API request failed with HTTP {status_code}."
        except requests.exceptions.RequestException as exc:
            log.error("[ChatbotApp] RequestException: %s", exc)
            return f"Error: Request failed: {exc}"
        except ValueError:
            log.error("[ChatbotApp] Invalid JSON in API response.")
            return "Error: The API returned an invalid response payload."

        try:
            content = result["choices"][0]["message"]["content"].strip()
            log.info("[ChatbotApp] API call successful — %d chars returned.", len(content))
            return content
        except (KeyError, IndexError, TypeError) as exc:
            log.error("[ChatbotApp] Unexpected API response structure: %s — raw: %s", exc, result)
            return "Error: The API response format was unexpected."


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Self-Correction Chatbot — standalone or C++ integration mode.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--wrong",
        action="store_true",
        help=(
            "C++ integration mode: read runtime_state.json and record a correction. "
            "Used by chatbot.exe when the user types /wrong."
        ),
    )
    parser.add_argument(
        "--severity",
        type=str,
        default="medium",
        help="The severity level of the wrong reply (low, medium, high).",
    )
    args = parser.parse_args()

    log.info("[main] Script invoked — BASE_DIR=%s, args=%s", BASE_DIR, args)

    if args.wrong:
        return handle_wrong_command(args.severity)

    # Standalone interactive mode
    app = ChatbotApp()
    try:
        app.run()
    except Exception as exc:  # pragma: no cover
        log.exception("[main] Unexpected runtime error: %s", exc)
        print(f"[SYSTEM LOG] Unexpected runtime error: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
