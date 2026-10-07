"""
api.py — Gemini API bridge with Self-Correction Enforcement.
Keeps a stable request/response contract for the C++ caller and exposes run_turn()
for the WhatsApp agent.
Usage:  python api.py <request_file> <response_file>

C++ INTEGRATION ARCHITECTURE:
  chatbot.exe (build/) invokes this script as a subprocess on EVERY turn.
  It inherits the CWD of chatbot.exe (typically build/).

  This script uses BASE_DIR = Path(__file__).resolve().parent to locate all
  state files relative to its OWN physical location (the project root),
  regardless of what directory chatbot.exe happens to run from.

STATE FILES READ/WRITTEN (all in BASE_DIR = project root):
  corrections.json    — fuzzy correction registry  (written by self_correction_chatbot.py)
  runtime_state.json  — last query/response pair   (written by THIS script after every turn)

CORRECTION ENFORCEMENT FLOW (per turn):
  1. Read and validate the JSON request payload from C++ (contains model + messages).
  2. Extract the current user query (last "user" role message in the payload).
  3. Load corrections.json and run SequenceMatcher fuzzy lookup (>= 0.85 ratio).
  4. If a match is found: inject the negative constraint into the system message
     of the ACTUAL payload sent to Gemini.
  5. call Gemini, write the response file.
  6. Write runtime_state.json (last query + response) for the /wrong handler.

UUID-named temp files (passed in from C++) prevent concurrent-session collisions.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

# Ensure the project root is importable even when the process is launched
# from the build/ subdirectory (for example by the compiled C++ binary).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR) if os.path.basename(_SCRIPT_DIR).lower() == "build" else _SCRIPT_DIR
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

try:
    import fcntl
except ImportError:  # Windows does not expose fcntl
    fcntl = None  # type: ignore[assignment]

from correction_utils import atomic_write_json, build_negative_constraint, compute_match_score, extract_keywords, keyword_match

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Unified Project Root Anchor
# ---------------------------------------------------------------------------
# If this script is executed from the build/ output directory (as C++ does),
# navigate one level up to reach the project root where all state files live.
# This exact logic is mirrored in self_correction_chatbot.py so both scripts
# always share the same PROJECT_ROOT regardless of which directory C++ runs from.
# ---------------------------------------------------------------------------
_current_dir = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_current_dir).lower() == "build":
    PROJECT_ROOT = os.path.dirname(_current_dir)
else:
    PROJECT_ROOT = _current_dir

BASE_DIR: Path       = Path(PROJECT_ROOT)   # Path-typed; used throughout this file
CORRECTIONS_PATH:   Path = BASE_DIR / "corrections.json"
RUNTIME_STATE_PATH: Path = BASE_DIR / "runtime_state.json"

# ---------------------------------------------------------------------------
# Logging — always written to BASE_DIR/api.log, never to a build/ shadow.
# ---------------------------------------------------------------------------
_LOG_PATH = BASE_DIR / "api.log"
logging.basicConfig(
    filename=str(_LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. Robust Initialization — persistent memory cache loaded at script bootup
# ---------------------------------------------------------------------------
CORRECTIONS_CACHE: dict[str, str] = {}

@contextmanager
def _locked_read(handle):
    if fcntl is None:
        yield handle
        return

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def _load_corrections_payload() -> dict[str, str]:
    if not CORRECTIONS_PATH.exists():
        return {}

    try:
        with CORRECTIONS_PATH.open("r", encoding="utf-8") as fh:
            with _locked_read(fh):
                payload = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        log.error("[Cold-Start Cache Init] Cannot read corrections.json: %s", exc)
        return {}

    if isinstance(payload, dict):
        normalized: dict[str, str] = {}
        for k, v in payload.items():
            if not isinstance(k, str):
                continue
            if isinstance(v, str):
                normalized[k.strip()] = v.strip()
            elif isinstance(v, dict):
                res_val = v.get("response")
                if isinstance(res_val, str):
                    normalized[k.strip()] = res_val.strip()
        return normalized

    if isinstance(payload, list):
        normalized = {}
        for entry in payload:
            if isinstance(entry, dict):
                q, r = entry.get("query"), entry.get("response")
                if isinstance(q, str) and isinstance(r, str):
                    normalized[q.strip()] = r.strip()
                elif isinstance(q, str) and isinstance(r, dict):
                    res_val = r.get("response")
                    if isinstance(res_val, str):
                        normalized[q.strip()] = res_val.strip()
        return normalized

    return {}


def _refresh_corrections_cache() -> None:
    global CORRECTIONS_CACHE
    CORRECTIONS_CACHE = _load_corrections_payload()
    log.info("[Cold-Start Cache Init] Loaded %d corrections from disk into memory cache.", len(CORRECTIONS_CACHE))


_initialize_corrections_cache = _refresh_corrections_cache
_initialize_corrections_cache()

# Log startup path resolution so operators can verify at a glance.
log.info("api.py startup — BASE_DIR=%s", BASE_DIR)
log.info("api.py startup — corrections_path=%s", CORRECTIONS_PATH)
log.info("api.py startup — runtime_state_path=%s", RUNTIME_STATE_PATH)


# ===========================================================================
# Utility helpers
# ===========================================================================

def write_error(path: str, message: str) -> bool:
    """Write a JSON error payload to the response file and log it."""
    try:
        response_path = Path(path)
        atomic_write_json(response_path, {"error": message})
        log.error("Error response written: %s", message)
        return True
    except OSError as exc:
        log.critical("Cannot write error response to %s: %s", path, exc)
        return False


def validate_payload(data: object) -> Optional[str]:
    """Return an error string if the payload is structurally invalid, else None."""
    if not isinstance(data, dict):
        return "Payload must be a JSON object"
    if not isinstance(data.get("model"), str):
        return "Missing 'model'"
    msgs = data.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return "Missing or empty 'messages'"
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            return f"messages[{i}] must be an object"
        if m.get("role") not in ("system", "user", "assistant"):
            return f"messages[{i}] invalid role"
        if not isinstance(m.get("content"), str):
            return f"messages[{i}] missing content"
        if len(m["content"]) > 32_000:
            return f"messages[{i}] exceeds 32k chars"
    return None


# ===========================================================================
# Correction enforcement layer
# ===========================================================================

def _evaluate_interception(user_query: str) -> Optional[tuple[str, str]]:
    """
    Dual-layer keyword + fuzzy interceptor right before the API call.
    1. Keyword Trigger: matches core keywords/nouns.
    2. Fuzzy Fallback: SequenceMatcher ratio >= 0.40.
    Returns (matched_query, blocked_response) or None.
    """
    stripped = user_query.strip()
    if not stripped or not CORRECTIONS_CACHE:
        return None

    # 1. Keyword Trigger
    for known_query, known_response in CORRECTIONS_CACHE.items():
        if not known_query.strip() or not known_response.strip():
            continue
        known_keywords = extract_keywords(known_query)
        curr_keywords = extract_keywords(stripped)
        if curr_keywords and known_keywords and keyword_match(curr_keywords, known_keywords):
            log.info("[Interceptor] Keyword match hit: current keywords %s matched known keywords %s (known query: '%s')",
                     curr_keywords, known_keywords, known_query)
            return known_query, known_response

    # 2. Fuzzy Fallback with stricter threshold and semantic guard
    best_query: Optional[str] = None
    best_response: Optional[str] = None
    best_score = 0.0
    for known_query, known_response in CORRECTIONS_CACHE.items():
        if not known_query.strip() or not known_response.strip():
            continue
        score = compute_match_score(stripped, known_query)
        if score >= 0.86 and score > best_score:
            best_score = score
            best_query = known_query
            best_response = known_response

    if best_query and best_response:
        log.info("[Interceptor] Fuzzy match fallback hit (score=%.4f): '%s' vs known '%s'",
                 best_score, stripped, best_query)
        return best_query, best_response

    return None


def _build_negative_constraint(blocked_response: str) -> str:
    """Build the ultimate strict prompt override constraint."""
    return build_negative_constraint(blocked_response)


def _build_interception_prompt(user_query: str, rejected_response: str, matched_query: str) -> str:
    """Build an enforcement-heavy prompt that blocks the rejected fact and forces an alternative-context pivot."""
    rejected_tokens = []
    for piece in rejected_response.split():
        cleaned = ''.join(ch for ch in piece if ch.isalnum())
        if len(cleaned) >= 3:
            rejected_tokens.append(cleaned)

    rejected_summary = ", ".join(rejected_tokens[:8]) or rejected_response
    return (
        "[CRITICAL ENFORCEMENT - PREVIOUS RESPONSE FLAGGED AS INCORRECT]\n"
        "The user has explicitly rejected your previous response layout/fact string for this query.\n\n"
        "CRITICAL RULES FOR THIS TURN:\n"
        "1. ABSOLUTE FACT BANNING: You are STRICTLY FORBIDDEN from outputting the exact answer, exact string, or primary factual noun from the rejected history.\n"
        "2. Do NOT print the exact string or the single-word repetition of the previous fact.\n"
        "3. POSITIVE COMPENSATORY SHIFT: Instantly pivot to a useful alternative, multi-perspective explanation; add relevant context rather than returning only a refusal.\n"
        "4. ALTERNATIVE CONTEXT: If the query matches a historical correction, change your approach entirely by giving geographic coordinates, administrative structure, history of the selection, or a different educational perspective.\n"
        "5. LANGUAGE SYNCHRONIZATION: Maintain strict linguistic alignment with the user's current query (if they ask in Roman Urdu, adapt the explanation fluently while avoiding the rejected wording).\n"
        f"Rejected response anchor: {rejected_response}\n"
        f"Rejected response summary tokens: {rejected_summary}\n"
        f"Matched historical intent: {matched_query}\n"
        f"Current user intent: {user_query}"
    )


def _inject_constraint_into_messages(
    messages: list[dict],
    constraint: str,
) -> list[dict]:
    """
    Inject the negative constraint into the system message of the messages array.

    Strategy:
      - If messages[0] has role="system", append the constraint to its content.
      - Otherwise, prepend a new system message containing only the constraint.

    A copy is returned — the original list is never mutated.
    """
    patched = [m.copy() for m in messages]

    if patched and patched[0].get("role") == "system":
        original_system = patched[0]["content"]
        patched[0] = {
            "role":    "system",
            "content": f"{original_system}\n\n{constraint}",
        }
        log.info(
            "[Correction] Negative constraint injected into existing system message "
            "(total system prompt: %d chars).",
            len(patched[0]["content"]),
        )
    else:
        patched.insert(0, {"role": "system", "content": constraint})
        log.info(
            "[Correction] No system message found — prepended constraint-only system message (%d chars).",
            len(constraint),
        )

    return patched


def _extract_user_query(messages: list[dict]) -> Optional[str]:
    """
    Extract the most recent user-role message content from the messages array.
    This is the query the correction lookup is performed against.
    """
    for m in reversed(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"].strip()
    return None


# ===========================================================================
# Runtime state persistence
# ===========================================================================

def _save_runtime_state(user_query: str, bot_response: str) -> None:
    """
    Overwrite runtime_state.json with the current turn's query and response.
    Called immediately after EVERY successful API response so the /wrong
    handler can recover the same exchange from disk.
    """
    state_data = {
        "user_query":       user_query,
        "chatbot_response": bot_response,
    }
    try:
        atomic_write_json(RUNTIME_STATE_PATH, state_data)
        log.info(
            "[RuntimeState] Saved — query: '%s...' response: '%s...'",
            user_query[:60], bot_response[:60],
        )
    except (OSError, TypeError, ValueError) as exc:
        # Non-fatal: API still returns successfully; /wrong will just fail gracefully.
        log.error("[RuntimeState] Cannot write runtime_state.json: %s", exc)


# ===========================================================================
# Core API bridge  (Gemini)
# ===========================================================================

# Keep model selection on currently active generateContent models.
DEFAULT_MODEL = "gemini-3.5-flash-lite"

SUPPORTED_MODELS = frozenset({"gemini-3.5-flash-lite", "gemini-3.1-pro-preview"})

_configured_fallbacks = [
    model.strip()
    for model in os.getenv(
        "GEMINI_FALLBACKS",
        "gemini-3.5-flash-lite,gemini-3.1-pro-preview",
    ).split(",")
    if model.strip()
]
FALLBACK_MODELS = [model for model in _configured_fallbacks if model in SUPPORTED_MODELS]
for _invalid_model in set(_configured_fallbacks) - SUPPORTED_MODELS:
    log.warning("[Gemini] Ignoring unsupported fallback model %r", _invalid_model)
GEMINI_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
THINK_CACHE_PATH: Path = BASE_DIR / "gemini_thinking.json"   # remembers which thinking setting each model accepts


class BridgeError(Exception):
    """User-presentable failure; the message is shown in the C++ console / WhatsApp."""


def _media_bytes(data: object, mime: str) -> bytes:
    """Serialize supported media inputs to bytes and validate image payloads."""
    normalized_mime = mime.split(";", 1)[0].strip().lower()
    if isinstance(data, (bytes, bytearray, memoryview)):
        payload = bytes(data)
    elif isinstance(data, (str, os.PathLike)):
        candidate = Path(data).expanduser()
        allowed_roots = (Path("/tmp").resolve(), BASE_DIR.resolve(), Path.cwd().resolve())
        candidates = (candidate,) if candidate.is_absolute() else (BASE_DIR / candidate, Path.cwd() / candidate)
        resolved = None
        for path in candidates:
            try:
                path = path.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if any(path == root or root in path.parents for root in allowed_roots):
                resolved = path
                break
        if resolved is None or not resolved.is_file():
            raise BridgeError("Media file path must resolve to a file in /tmp or the application directory.")
        try:
            if resolved.stat().st_size > 20 * 1024 * 1024:
                raise BridgeError("Media file exceeds the 20 MB upload limit.")
            payload = resolved.read_bytes()
        except OSError as exc:
            raise BridgeError("Media file could not be read.") from exc
    else:
        from PIL import Image

        if not isinstance(data, Image.Image):
            raise BridgeError("Media payload must be bytes, a local file path, or a PIL image.")
        image_formats = {
            "image/jpeg": "JPEG",
            "image/png": "PNG",
            "image/webp": "WEBP",
            "image/gif": "GIF",
            "image/bmp": "BMP",
            "image/tiff": "TIFF",
        }
        image_format = image_formats.get(normalized_mime)
        if image_format is None:
            raise BridgeError(f"Unsupported PIL image MIME type: {normalized_mime or '(empty)'}")
        image_buffer = io.BytesIO()
        try:
            image = data
            if image_format == "JPEG" and image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            image.save(image_buffer, format=image_format)
        except Exception as exc:
            raise BridgeError("PIL image could not be encoded for Gemini.") from exc
        payload = image_buffer.getvalue()

    if not payload:
        raise BridgeError("Media payload is empty.")
    if len(payload) > 20 * 1024 * 1024:
        raise BridgeError("Media payload exceeds the 20 MB upload limit.")
    if normalized_mime.startswith("image/"):
        try:
            from PIL import Image

            with Image.open(io.BytesIO(payload)) as image:
                image.verify()
        except Exception as exc:
            raise BridgeError("Image payload is not a valid or supported image.") from exc
    return payload


def media_part(data: object, mime: str) -> dict:
    """Inline image / PDF / audio part (REST format) for run_turn(media_parts=...)."""
    try:
        normalized_mime = mime.split(";", 1)[0].strip().lower()
        payload = _media_bytes(data, normalized_mime)
        return {"inline_data": {
            "mime_type": normalized_mime,
            "data": base64.b64encode(payload).decode("ascii"),
        }}
    except BridgeError:
        raise
    except Exception as exc:
        raise BridgeError("Media payload could not be prepared for Gemini.") from exc


def _pick_model(requested: Optional[str]) -> str:
    """Select only an active, supported generateContent model."""
    candidate = os.getenv("GEMINI_MODEL") or requested
    if isinstance(candidate, str) and "gemini" in candidate.lower():
        model = candidate.strip().split("/")[-1]
        if model in SUPPORTED_MODELS:
            return model
        log.warning("[Gemini] Ignoring unsupported model %r; using %s", model, DEFAULT_MODEL)
    return DEFAULT_MODEL


def _to_gemini(messages: list[dict], media_parts=None):
    """OpenAI-style messages -> (system_instruction, Gemini REST contents)."""
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    # Ultra-terse instruction: eliminates preamble, greetings, and padding.
    # This is the single biggest latency win — fewer output tokens = faster response.
    system_parts.append(
        "Reply in the same language the user wrote in. "
        "Be direct and concise — no greetings, no preamble, no sign-offs. "
        "For numbered steps, put each on its own line. "
        "For WhatsApp: use plain text, no markdown headers (#), keep paragraphs short."
    )
    system = "\n\n".join(system_parts)
    contents: list[dict] = []
    for m in messages:
        if m["role"] == "system":
            continue
        role = "user" if m["role"] == "user" else "model"
        if not contents and role == "model":
            continue  # Gemini history must start with a user turn
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    if media_parts and contents and contents[-1]["role"] == "user":
        contents[-1] = {"role": "user", "parts": list(media_parts) + contents[-1]["parts"]}
    return system, contents


def _friendly_status(status: int, body: str) -> str:
    low = body.lower()
    if status in (401, 403) or "api_key_invalid" in low or "api key not valid" in low:
        return "Authentication failed. Check GEMINI_API_KEY."
    if status == 429:
        return "API rate limit exceeded. Please try again later."
    if status in (500, 502, 503, 504):
        return "Gemini is busy right now (high demand). Please try again in a moment."
    if status == 404:
        try:
            detail = json.loads(body).get("error", {}).get("message")
        except (AttributeError, TypeError, ValueError):
            detail = None
        if isinstance(detail, str) and detail.strip():
            return f"Gemini returned HTTP 404: {detail.strip()}"
        return f"Gemini returned HTTP 404: {body[:500] or 'The model or endpoint was not found.'}"
    return f"Gemini request failed (HTTP {status})."


def _thinking_variants(model: str) -> list:
    """Thinking settings to try. Flash models disable thinking entirely for minimum latency.
    Unsupported variants get a 400 and we fall through (the working one is cached on disk)."""
    if os.getenv("GEMINI_THINKING", "").lower() == "default":
        return [None]
    m = model.lower()
    # All flash variants: disable thinking — saves 2-4s per call.
    if "flash" in m:
        return [{"thinkingBudget": 0}, None]
    # Pro/experimental models: try minimal thinking first, then model default.
    if "2.5" in m or "2.0" in m or "pro" in m:
        return [{"thinkingBudget": 0}, None]
    return [None]


def _load_think_cache() -> dict:
    try:
        cache = json.loads(THINK_CACHE_PATH.read_text(encoding="utf-8"))
        return cache if isinstance(cache, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_think_cache(cache: dict) -> None:
    try:
        atomic_write_json(THINK_CACHE_PATH, cache)
    except (OSError, TypeError, ValueError) as exc:
        log.warning("[Gemini] Could not persist thinking configuration: %s", exc)


def _generate(model: str, key: str, system: str, contents: list, temperature: float, read_timeout: float):
    """One REST call (with thinking-variant negotiation). Returns the requests.Response."""
    cache = _load_think_cache()
    cached_variant = cache.get(model)
    variants = [cached_variant] if cached_variant in (None, {"thinkingBudget": 0}) else _thinking_variants(model)
    last = None
    deadline = time.monotonic() + read_timeout
    for think in variants:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # 512 tokens covers ~380 words — enough for any WhatsApp reply.
        # Keeping this tight is the second-biggest latency win after model selection.
        gen_cfg: dict = {"temperature": temperature, "maxOutputTokens": 512}
        if think:
            gen_cfg["thinkingConfig"] = think
        body: dict = {"contents": contents, "generationConfig": gen_cfg}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        connect_timeout = min(4.0, max(0.25, remaining / 3))
        r = requests.post(
            f"{GEMINI_URL}/models/{model}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=body,
            timeout=(connect_timeout, max(0.25, remaining - connect_timeout)))
        if r.status_code == 400 and think is not None and "think" in r.text.lower():
            log.info("[Gemini] %s rejected thinking=%s, trying next setting", model, think)
            last = r
            continue
        if r.ok and model not in cache:
            cache[model] = think
            _save_think_cache(cache)
        return r
    return last


def _call_gemini(model: str, system: str, contents: list, temperature: float = 0.1,
                 budget_s: float = 15.0) -> tuple[str, dict]:
    """Call Gemini with 503-retry and automatic model fallback. Returns (text, usage)."""
    if requests is None:
        raise BridgeError("requests module not installed (run: python -m pip install requests)")
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise BridgeError("GEMINI_API_KEY environment variable not set")
    if model not in SUPPORTED_MODELS:
        log.warning("[Gemini] Replacing unsupported requested model %r with %s", model, DEFAULT_MODEL)
        model = DEFAULT_MODEL

    budget_s = min(budget_s, 15.0)
    t0 = time.monotonic()
    deadline = t0 + budget_s
    last_msg = None

    for name in [model] + [m for m in FALLBACK_MODELS if m != model]:
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining < 3:
                break
            try:
                r = _generate(name, key, system, contents, temperature, remaining)
            except requests.exceptions.Timeout:
                last_msg = "API Request Timed Out."
                log.warning("[Gemini] %s timed out", name)
                break
            except requests.exceptions.ConnectionError:
                raise BridgeError("No Internet Connection. Please check your network and try again.")
            except requests.exceptions.RequestException as exc:
                last_msg = f"Network error: {type(exc).__name__}."
                break

            if r is None:
                last_msg = "Gemini request deadline expired before receiving a response."
                break

            if r.ok:
                try:
                    j = r.json()
                except ValueError:
                    last_msg = "Invalid JSON response from API."
                    break
                block = (j.get("promptFeedback") or {}).get("blockReason")
                cands = j.get("candidates") or []
                parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
                text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                if not text:
                    last_msg = ("Gemini blocked this request (%s)." % block) if block else \
                               "Gemini returned an empty reply."
                    log.warning("[Gemini] %s empty reply, block=%s", name, block)
                    break
                um = j.get("usageMetadata") or {}
                usage = {
                    "prompt_tokens":     int(um.get("promptTokenCount") or 0),
                    "completion_tokens": int(um.get("candidatesTokenCount") or 0),
                    "total_tokens":      int(um.get("totalTokenCount") or 0),
                }
                log.info("[Gemini] %s replied in %.1fs (tokens=%s)%s", name, time.monotonic() - t0,
                         usage["total_tokens"], "" if name == model else " [FALLBACK]")
                return text, usage

            log.warning("[Gemini] %s attempt %d -> HTTP %s: %s", name, attempt + 1, r.status_code, r.text[:200])
            last_msg = _friendly_status(r.status_code, r.text)
            if r.status_code in (401, 403) or "api_key_invalid" in r.text.lower():
                raise BridgeError(last_msg)         # no point trying other models with a bad key
            if r.status_code in (500, 502, 503, 504):
                time.sleep(0.25)
                continue                            # temporary overload: retry once
            break                                   # 404 / 429 / other: next model

    raise BridgeError(last_msg or "Gemini request could not be completed.")


def run_turn(messages: list[dict], model: Optional[str] = None, media_parts=None,
             temperature: float = 0.1, budget_s: float = 15.0) -> tuple[str, dict]:
    """
    One full chatbot turn: history pruning -> self-correction enforcement ->
    Gemini call -> runtime_state.json save.  Used by main() (C++ subprocess path)
    and by whatsapp_agent.py.  media_parts = optional media_part() dicts (image/PDF/audio)
    attached to the last user message.  Raises BridgeError on failure.
    """
    # --- History pruning: keep system prompt + last 20 messages ---
    if messages and messages[0].get("role") == "system":
        messages = [messages[0]] + messages[1:][-20:]
    else:
        messages = messages[-20:]

    # --- CORRECTION ENFORCEMENT (right before the payload is compiled) ---
    user_query: Optional[str] = _extract_user_query(messages)
    constraint_applied = False

    if user_query:
        log.info("[Correction] Extracted user query (%d chars): '%s...'", len(user_query), user_query[:60])
        _refresh_corrections_cache()
        match = _evaluate_interception(user_query)
        if match:
            known_query, blocked_response = match
            if blocked_response.strip():
                log.info("[Correction] MATCH for query '%s' -> applying enhanced constraint.", known_query)
                constraint = _build_interception_prompt(user_query, blocked_response, known_query)
                if constraint.strip():
                    messages = _inject_constraint_into_messages(messages, constraint)
                    constraint_applied = True
        else:
            log.info("[Correction] No match found - sending clean payload.")
    else:
        log.warning("[Correction] Could not extract user query - skipping correction lookup.")

    chosen = _pick_model(model)
    system, contents = _to_gemini(messages, media_parts)
    log.info("[API] Sending to Gemini - model=%s messages=%d constraint=%s media=%d",
             chosen, len(messages), constraint_applied, len(media_parts or []))

    text, usage = _call_gemini(chosen, system, contents, temperature, budget_s)
    log.info("Success - tokens=%s constraint_applied=%s", usage.get("total_tokens", "?"), constraint_applied)

    # --- RUNTIME STATE: self_correction_chatbot.py --wrong reads this ---
    if user_query and text:
        _save_runtime_state(user_query, text)
    return text, usage


def main(request_file: str, response_file: str) -> int:
    log.info("Starting - request=%s response=%s", request_file, response_file)

    try:
        with open(request_file, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        log.error("Cannot read request: %s", exc)
        return 0 if write_error(response_file, "System Error: Failed to read session request payload.") else 1

    err = validate_payload(data)
    if err:
        return 0 if write_error(response_file, f"Invalid payload: {err}") else 1

    try:
        text, usage = run_turn(data["messages"], model=data.get("model"),
                               temperature=data.get("temperature", 0.1))
    except BridgeError as exc:
        return 0 if write_error(response_file, str(exc)) else 1

    # Same shape the C++ side already parses (OpenAI-style): choices[0].message.content + usage
    result = {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": usage,
    }
    try:
        atomic_write_json(Path(response_file), result)
    except OSError as exc:
        log.error("Cannot write response file: %s", exc)
        return 1
    return 0


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    if len(sys.argv) == 3:
        req_file, res_file = sys.argv[1], sys.argv[2]
    else:
        req_file, res_file = "request.json", "response.json"

    try:
        sys.exit(main(req_file, res_file))
    except KeyboardInterrupt:
        write_error(res_file, "Process interrupted")
        sys.exit(130)
    except Exception as exc:
        log.exception("Unhandled exception in api.py main()")
        sys.exit(0 if write_error(res_file, "System Error: An unexpected error occurred inside the API bridge.") else 1)