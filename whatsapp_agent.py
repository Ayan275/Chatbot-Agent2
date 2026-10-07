#!/usr/bin/env python3
"""
WhatsApp front-end for the FULL Hybrid C++/Python AI Chatbot (Gemini edition).

This script does NOT re-implement your chatbot. It starts your real C++ program once
(`chatbot --agent <user>`) and types every WhatsApp message into it, exactly like the terminal:
  - all commands work: /trust /audit last|list|<id>|verify /wrong [sev] /feedback ... /corrections /history /export /clear /help
  - AuditLogger hash-chain, TrustTracker scores, Self-Correction Engine, input filter, 500-word cap, session memory
Gemini is used only as "ears and eyes":
  - voice note  -> transcript -> chatbot -> text + voice reply
  - image / PDF / TXT / DOCX -> short digest -> chatbot answers your request about it

Keep this file in the project root next to api.py and the C++ chatbot binary, then run:
    python whatsapp_agent.py
Env vars: WA_AGENT_TOKEN, GEMINI_API_KEY, CHATBOT_USER (needed if users.json has several users)
Optional: CHATBOT_EXE (full path to chatbot binary), GEMINI_MODEL
Disabled over WhatsApp: /exit, exit, /changepassword (they need the terminal).
"""
import os, io, re, json, time, queue, asyncio, logging, tempfile, threading, subprocess
import hashlib, secrets
from pathlib import Path
from PIL import Image
import requests, edge_tts
from correction_utils import atomic_write_json

import api as gemini   # your bridge (used here only for voice/image/document understanding)

def clean_whatsapp_text(text: str) -> str:
    """Normalize line endings and restore readable WhatsApp paragraph boundaries."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\\+n", "\n", text)

    text = re.sub(r"(?<!\n)[ \t]+(?=\d+\.\s+)", "\n\n", text)
    text = re.sub(r"(?<!\n)\n(?=\d+\.\s+)", "\n\n", text)

    text = re.sub(r"(?<!\d)([.!?])[ \t]+(?=\S)", r"\1\n\n", text)
    text = re.sub(r"(?<!\d)([.!?])\n(?!\n)", r"\1\n\n", text)
    text = re.sub(r"(?<!\d):[ \t]+(?=\S)", ":\n\n", text)
    text = re.sub(r"(?<!\d):\n(?!\n)", ":\n\n", text)
    return text.strip()

BASE_DIR = Path(__file__).resolve().parent
BASE = "https://api.whatsapp.com/agent/v1"
HTTP = requests.Session()
HDR = {"Authorization": f"Bearer {os.environ['WA_AGENT_TOKEN']}"}
STATE_FILE = BASE_DIR / "wa_state.json"
BLOCKED_CMDS = ("/exit", "exit", "/changepassword")
MAX_LINE = 1900            # chatbot hard limit is 2000 characters per message
TURN_TIMEOUT = 40          # C++ API bridge has a 25 s hard timeout
MEDIA_BUDGET = 15          # bridge bounds Gemini calls to 15 seconds
MAX_MEDIA_BYTES = 20 * 1024 * 1024

log = logging.getLogger("wa_agent")
log.setLevel(logging.INFO)
_h = logging.StreamHandler()
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_h)


# ---------- your C++ chatbot as a long-lived child process ----------
class ChatbotDied(RuntimeError):
    pass


class ChatbotProc:
    MARK = b"<<READY>>"

    def __init__(self, exe: Path, user: str):
        self.exe, self.user = exe, user
        self.proc = None
        self.q: "queue.Queue[bytes | None]" = queue.Queue()
        self.stdout_buffer = bytearray()
        self.lock = threading.Lock()

    @staticmethod
    def _pump(proc, q):
        fd = proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            q.put(chunk or None)
            if not chunk:
                return

    def _start(self):
        for attempt in range(2):
            # Propagate PYTHON_EXECUTABLE so the C++ binary uses python3 on Linux/Docker.
            env = dict(os.environ,
                       PYTHONIOENCODING="utf-8",
                       PYTHON_EXECUTABLE=os.environ.get("PYTHON_EXECUTABLE", "python3"))
            self.q = queue.Queue()
            self.stdout_buffer.clear()
            try:
                self.proc = subprocess.Popen([str(self.exe), "--agent", self.user], cwd=str(BASE_DIR),
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, env=env, bufsize=0)
            except OSError as exc:
                raise ChatbotDied(f"Could not start chatbot executable '{self.exe}': {exc}") from exc
            threading.Thread(target=self._pump, args=(self.proc, self.q), daemon=True).start()
            try:
                banner = self._read_ready(60)
            except ChatbotDied as exc:
                if attempt or "unknown user" not in str(exc).lower():
                    raise
                try:
                    _ensure_user_profile(self.user)
                except (OSError, RuntimeError, ValueError) as init_error:
                    raise ChatbotDied(
                        f"Could not initialize user profile '{self.user}': {init_error}"
                    ) from init_error
                log.warning("Provisioned missing user profile '%s'; retrying chatbot startup", self.user)
                continue
            log.info("chatbot started: %s", (banner.strip().splitlines() or ["?"])[-1])
            return

        raise ChatbotDied(f"Could not initialize chatbot user '{self.user}'.")

    def _kill(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass
        self.proc = None

    def _read_ready(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            marker_index = self.stdout_buffer.find(self.MARK)
            if marker_index >= 0:
                marker_end = marker_index + len(self.MARK)
                if self.stdout_buffer[marker_end:marker_end + 2] == b"\r\n":
                    marker_end += 2
                elif self.stdout_buffer[marker_end:marker_end + 1] == b"\n":
                    marker_end += 1
                output = bytes(self.stdout_buffer[:marker_index])
                del self.stdout_buffer[:marker_end]
                return output.decode("utf-8", "replace")

            left = deadline - time.monotonic()
            if left <= 0:
                self._kill()
                raise ChatbotDied("chatbot did not answer in time")
            try:
                chunk = self.q.get(timeout=left)
            except queue.Empty:
                continue
            if chunk is None:
                self._kill()
                output = bytes(self.stdout_buffer).decode("utf-8", "replace").strip()[-300:]
                raise ChatbotDied(output or "chatbot exited")
            self.stdout_buffer.extend(chunk)

    def ask(self, line: str) -> str:
        """Type one line into the chatbot; return everything it printed before the next prompt."""
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            try:
                self.proc.stdin.write((line.replace("\r", " ").replace("\n", " ") + "\n").encode("utf-8"))
                self.proc.stdin.flush()
                out = self._read_ready(TURN_TIMEOUT)
            except (BrokenPipeError, OSError) as exc:
                self._kill()
                raise ChatbotDied(f"Chatbot IPC write failed: {exc}") from exc
        out = out.replace("\r\n", "\n")
        return re.sub(r"\x1b\[[0-9;]*m", "", out).strip()

    def close(self):
        self._kill()


def find_exe() -> Path:
    env = os.getenv("CHATBOT_EXE")
    cands = [Path(env)] if env else []
    executable_names = ("chatbot.exe", "chatbot") if os.name == "nt" else ("chatbot_linux", "chatbot")
    for d in (BASE_DIR / "build", BASE_DIR / "build" / "Release", BASE_DIR / "build" / "Debug", BASE_DIR):
        cands.extend(d / name for name in executable_names)
    for c in cands:
        if c.is_file():
            return c
    raise ChatbotDied("Chatbot executable not found. Build it with CMake or set CHATBOT_EXE.")


def find_user() -> str:
    u = os.getenv("CHATBOT_USER", "").strip()
    if u:
        return u
    users_path = BASE_DIR / "users.json"
    try:
        with users_path.open(encoding="utf-8") as users_file:
            users = json.load(users_file)
        if not isinstance(users, list):
            raise ValueError("users.json must contain a JSON array.")
        names = [entry["username"] for entry in users
                 if isinstance(entry, dict) and isinstance(entry.get("username"), str)]
    except FileNotFoundError:
        names = []
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ChatbotDied(f"Could not read chatbot user registry '{users_path}': {exc}") from exc
    if len(names) == 1:
        return names[0]
    if not names:
        return "Ayan"
    raise ChatbotDied(
        "Set CHATBOT_USER to one of the configured chatbot users: " + ", ".join(names)
    )


def _ensure_user_profile(username: str) -> None:
    """Create an auth-compatible, non-interactive profile if an old binary requires it."""
    if not re.fullmatch(r"[A-Za-z0-9 -]{1,50}", username):
        raise ValueError("Configured chatbot username contains unsupported characters.")

    users_path = BASE_DIR / "users.json"
    lock_path = BASE_DIR / ".users.json.lock"
    deadline = time.monotonic() + 10
    lock_fd = None
    while lock_fd is None:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for the user registry lock.")
            time.sleep(0.05)

    try:
        if users_path.exists():
            try:
                with users_path.open("r", encoding="utf-8") as user_file:
                    users = json.load(user_file)
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("Cannot read the user registry; refusing to overwrite it.") from exc
            if not isinstance(users, list):
                raise RuntimeError("User registry must be a JSON array; refusing to overwrite it.")
        else:
            users = []

        if any(isinstance(entry, dict) and entry.get("username") == username for entry in users):
            return

        salt = secrets.token_hex(16)
        password = secrets.token_urlsafe(48)
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        users.append({
            "username": username,
            "password_hash": hashlib.sha256((salt + password).encode("utf-8")).hexdigest(),
            "salt": salt,
            "created_at": now,
            "last_login": now,
        })

        temp_path = users_path.with_name(f"{users_path.name}.{os.getpid()}.tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as user_file:
                json.dump(users, user_file, indent=2)
                user_file.write("\n")
            os.replace(temp_path, users_path)
        finally:
            temp_path.unlink(missing_ok=True)
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)


bot: ChatbotProc | None = None  # initialized before polling starts


def split_reply(out: str):
    """'[Turn N]\\nBot: text' -> (text, True). Anything else (commands, notices) -> (out, False)."""
    m = re.match(r"\[Turn \d+\]\s*\nBot:\s*(.*)", out, re.S)
    if not m:
        return out, False
    reply = m.group(1)
    if reply.endswith("\n"):
        reply = reply[:-1]  # Remove the CLI framing newline, not model-provided line breaks.
    return reply, True


def safe(s: str, limit=MAX_LINE) -> str:
    """Make machine-made text pass the chatbot's input filter (it rejects '/', backslash and '..')."""
    s = s.replace("\\", " ").replace("/", " ").replace("..", ".")
    return re.sub(r"\s+", " ", s).strip()[:limit]


# ---------- WhatsApp API helpers ----------
def api_call(method, path, **kw):
    """Backoff on 429 / 503(131016). 500s are NOT retried (could duplicate a send)."""
    timeout = kw.pop("timeout", 60)
    for i in range(4):
        try:
            r = HTTP.request(method, BASE + path, headers=HDR, timeout=timeout, **kw)
        except requests.RequestException:
            log.exception("%s %s request failed", method, path)
            raise
        if not (r.status_code == 429 or (r.status_code == 503 and "131016" in r.text)):
            return r
        wait = min(8, 2**i)
        log.warning("%s %s -> %s, retrying in %ss", method, path, r.status_code, wait)
        time.sleep(wait)
    return r


def send(to, type_, payload):
    try:
        r = api_call("POST", "/messages", json={
            "messaging_product": "whatsapp", "to": to, "type": type_, type_: payload})
    except requests.RequestException:
        return False
    if not r.ok:
        log.error("send failed %s: %s", r.status_code, r.text)
    return r.ok


def send_text(to, text):
    text = text or "(empty)"
    for i in range(0, len(text), 4000):   # limit is 4096
        send(to, "text", {"body": text[i:i + 4000]})


def send_audio(to, mp3_bytes):
    try:
        r = api_call("POST", "/media",
                     data={"messaging_product": "whatsapp", "type": "audio/mpeg"},
                     files={"file": ("reply.mp3", mp3_bytes, "audio/mpeg")})
    except requests.RequestException:
        return
    if not r.ok:
        log.error("media upload failed %s: %s", r.status_code, r.text)
        return
    try:
        media_id = r.json()["id"]
    except (ValueError, KeyError, TypeError) as exc:
        log.error("media upload response was invalid: %s", exc)
        return
    send(to, "audio", {"id": media_id})


def mark_read_typing(msg_id):
    try:
        api_call("POST", "/statuses", json={
            "messaging_product": "whatsapp", "status": "read",
            "message_id": msg_id, "typing_indicator": {"type": "text"}})
    except requests.RequestException as exc:
        log.warning("status request failed: %s", exc)


def download(media_id):
    meta = api_call("GET", f"/media/{media_id}")
    meta.raise_for_status()
    meta = meta.json()
    media_url = meta.get("url")
    if not isinstance(media_url, str) or not media_url:
        raise ValueError("WhatsApp media metadata did not contain a download URL.")
    r = HTTP.get(media_url, headers=HDR, timeout=30, stream=True)
    try:
        r.raise_for_status()
        content_length = r.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_MEDIA_BYTES:
            raise ValueError("WhatsApp media exceeds the 20 MB processing limit.")
        payload = bytearray()
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            if len(payload) + len(chunk) > MAX_MEDIA_BYTES:
                raise ValueError("WhatsApp media exceeds the 20 MB processing limit.")
            payload.extend(chunk)
    finally:
        r.close()
    mime = meta.get("mime_type")
    return bytes(payload), mime.split(";", 1)[0].strip().lower() if isinstance(mime, str) else ""


# ---------- Gemini: ears and eyes ----------
def cfg_model():
    try:
        with (BASE_DIR / "config.json").open(encoding="utf-8") as config_file:
            config = json.load(config_file)
        return config.get("model") if isinstance(config, dict) else None
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not load Gemini model configuration: %s", exc)
        return None


def gem(parts, budget=15):
    text, _ = gemini._call_gemini(gemini._pick_model(cfg_model()), "",
                                  [{"role": "user", "parts": parts}], 0.1, budget)
    return text.strip()


_GEMINI_AUDIO_MIMES = {"audio/ogg", "audio/mpeg", "audio/aac", "audio/wav", "audio/flac", "audio/opus"}


def _normalise_audio_mime(mime: str) -> str:
    """Map WhatsApp audio MIME variants to a type Gemini accepts.
    WhatsApp sends 'audio/ogg; codecs=opus' or bare 'audio/opus';
    Gemini's inline_data accepts 'audio/ogg' for OGG/Opus streams."""
    m = mime.split(";")[0].strip().lower()
    if m in ("audio/opus", "audio/ogg"):
        return "audio/ogg"
    if m in _GEMINI_AUDIO_MIMES:
        return m
    return "audio/ogg"  # safe fallback for unknown WhatsApp audio types


def transcribe(data: bytes, mime: str) -> str:
    """Send audio bytes to Gemini for transcription. Always uses normalised MIME."""
    norm_mime = _normalise_audio_mime(mime)
    return gem([gemini.media_part(data, norm_mime),
                {"text": "Transcribe this voice note exactly as spoken, in the original language and "
                         "script. Output only the transcript, nothing else."}], MEDIA_BUDGET)


def digest(kind: str, data: bytes | str | os.PathLike | None = None,
           mime: str | None = None, text: str | None = None) -> str:
    try:
        if kind == "image":
            if data is None:
                raise ValueError("Image payload is missing.")
            mime = (mime or "image/jpeg").split(";", 1)[0].strip().lower()
            if not mime.lower().startswith("image/"):
                raise ValueError("Image payload has an invalid MIME type.")
            if not isinstance(data, (bytes, bytearray, memoryview)):
                raise ValueError("Image payload must be downloaded image bytes.")
            ask = (text.strip() if isinstance(text, str) and text.strip()
                   else "Analyze this image in detail.")
            with Image.open(io.BytesIO(bytes(data))) as source:
                source.load()
                image = source.copy()
            image_part = gemini.media_part(image, mime)
            return gem([{"text": ask}, image_part], MEDIA_BUDGET)
        else:
            ask = ("Summarise this document: topic, key points, important numbers, names and dates. "
                   "Plain text only, no markdown, at most 1000 characters.")
        media_mime = mime or ("image/jpeg" if kind == "image" else "application/octet-stream")
        first = gemini.media_part(data, media_mime) if data is not None else {"text": text}
        return gem([first, {"text": ask}], MEDIA_BUDGET)
    except Exception as exc:
        if kind == "image":
            log.exception("Gemini image processing failed (mime=%s)", mime)
            raise RuntimeError("Image payload could not be prepared or sent to Gemini Vision.") from exc
        raise


def docx_text(data):
    import docx
    return "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)


def pick_voice(text):
    if re.search(r"[\u0600-\u06FF]", text):
        return "ur-PK-AsadNeural"
    if re.search(r"[\u0900-\u097F]", text):
        return "hi-IN-MadhurNeural"
    return "en-US-AriaNeural"


def tts(text):
    text = text[:1000]
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        asyncio.run(edge_tts.Communicate(text, pick_voice(text)).save(path))
        with open(path, "rb") as audio_file:
            return audio_file.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------- handler ----------
def handle(msg):
    to, t = msg["from"], msg["type"]        # 'from' goes back unchanged as 'to'
    t0 = time.monotonic()
    mark_read_typing(msg["id"])
    heard, voice = None, False

    if t == "text":
        txt = msg["text"]["body"].strip()
        if not txt:
            return
        if txt.lower().split()[0] in BLOCKED_CMDS:
            send_text(to, "Yeh command WhatsApp par disable hai (terminal mein chalao).")
            return
        # commands go through untouched; normal text gets '/' and '..' softened so the chatbot's
        # path-traversal filter does not reject harmless words like and/or
        line = txt if txt.startswith("/") else safe(txt)

    elif t == "audio":
        try:
            data, mime = download(msg["audio"]["id"])
            heard = safe(transcribe(data, mime))
        except Exception as e:
            log.error("audio processing failed: %s", e)
            send_text(to, "Awaaz process nahi ho saki. Dobara try karo.")
            return
        log.info("transcript: %s", heard)
        if not heard:
            send_text(to, "Awaaz samajh nahi aayi, dobara bolo?")
            return
        line, voice = heard, True

    elif t == "image":
        try:
            data, mime = download(msg["image"]["id"])
            cap = safe(msg["image"].get("caption") or "Is image ki summary do", 300)
            desc = safe(digest("image", data, mime or "image/jpeg", text=cap), 1500)
            line = safe(f"{cap} [Attached image: {desc}]")
        except Exception as e:
            log.exception("image processing failed for media_id=%s: %s", msg["image"].get("id"), e)
            send_text(to, "Image process nahi ho saki. Dobara bhejo ya text mein batao.")
            return

    elif t == "document":
        try:
            d = msg["document"]
            data, mime = download(d["id"])
            name = d.get("filename", "document")
            cap = safe(d.get("caption") or "Is document ki summary do: key points aur action items", 300)
            if mime == "application/pdf":
                dg = digest("doc", data, mime)
            elif mime == "text/plain":
                dg = digest("doc", text=data.decode("utf-8", "ignore")[:60_000])
            elif name.lower().endswith(".docx"):
                dg = digest("doc", text=docx_text(data)[:60_000])
            else:
                send_text(to, f"'{name}' ka format summarise nahi kar sakta. PDF, TXT ya DOCX bhejo.")
                return
            line = safe(f"{cap} [Attached document {safe(name, 60)}: {safe(dg, 1500)}]")
        except Exception as e:
            log.error("document processing failed: %s", e)
            send_text(to, "Document process nahi ho saka. Dobara bhejo ya text mein batao.")
            return

    else:
        return   # stickers, video, reactions: ignored

    if bot is None:
        log.error("Ignoring message because the C++ chatbot is not initialized.")
        send_text(to, "Chatbot abhi available nahi hai. Thori der baad dobara try karo.")
        return

    try:
        out = bot.ask(line)
    except ChatbotDied as e:
        log.error("chatbot problem: %s", e)
        send_text(to, f"Chatbot chal nahi saka: {e}")
        return

    reply, is_chat = split_reply(out)
    log.info("turn done in %.1fs (%s)", time.monotonic() - t0, t)
    message_text = f"\U0001F399 {heard}\n\n{reply}" if heard else reply
    send_text(to, clean_whatsapp_text(message_text))   # sequential sends only
    if voice and is_chat and not reply.startswith("Error:"):
        try:
            send_audio(to, tts(reply))
        except Exception as e:
            log.error("voice reply failed: %s", e)


# ---------- state + poll loop ----------
def load_state():
    try:
        with STATE_FILE.open(encoding="utf-8") as state_file:
            state = json.load(state_file)
        if (isinstance(state, dict) and isinstance(state.get("offset"), int)
                and isinstance(state.get("since"), int)
                and isinstance(state.get("handled"), list)
                and all(isinstance(item, str) for item in state["handled"])):
            return state
        raise ValueError("WhatsApp state file has an invalid schema.")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        log.warning("Could not load WhatsApp state; initializing a fresh offset: %s", exc)
        return {"offset": 0, "since": int(time.time()), "handled": []}   # first run: ignore old backlog


def save_state(st):
    st["handled"] = st["handled"][-500:]
    try:
        atomic_write_json(STATE_FILE, st)
    except (OSError, TypeError, ValueError):
        log.exception("Could not persist WhatsApp polling state.")


def main():
    global bot
    try:
        bot = ChatbotProc(find_exe(), find_user())
        bot.ask("/help")  # Starts agent mode, which provisions a missing user non-interactively.
    except ChatbotDied as exc:
        log.critical("Chatbot startup failed: %s", exc)
        if bot is not None:
            bot.close()
        return
    st = load_state()
    handled = set(st["handled"])
    log.info("WhatsApp agent started (user=%s, exe=%s, offset=%s)", bot.user, bot.exe.name, st["offset"])

    try:
        while True:
            try:
                r = HTTP.get(f"{BASE}/updates", headers=HDR, timeout=35,
                             params={"offset": st["offset"], "limit": 50, "timeout": 25})
            except requests.RequestException as e:
                log.warning("poll error: %s", e)
                time.sleep(5)
                continue

            if r.status_code == 204:
                continue
            if r.status_code == 409:
                log.error("Another poller is running for this agent. Stop the other one.")
                time.sleep(10)
                continue
            if r.status_code in (429, 500):
                time.sleep(20)
                continue
            if not r.ok:
                log.error("poll failed %s: %s", r.status_code, r.text)
                time.sleep(10)
                continue

            try:
                data = r.json()
                if not isinstance(data, dict) or not isinstance(data.get("entry", []), list):
                    raise ValueError("WhatsApp updates response has an invalid schema.")
                next_offset = data.get("next_offset")
                if not isinstance(next_offset, int):
                    raise ValueError("WhatsApp updates response has no valid next_offset.")
            except (ValueError, requests.JSONDecodeError) as exc:
                log.error("Invalid WhatsApp updates response: %s", exc)
                time.sleep(5)
                continue

            for entry in data.get("entry", []):
                if not isinstance(entry, dict):
                    continue
                changes = entry.get("changes", [])
                if not isinstance(changes, list):
                    continue
                for ch in changes:
                    if not isinstance(ch, dict) or not isinstance(ch.get("value"), dict):
                        continue
                    messages = ch["value"].get("messages", [])
                    if not isinstance(messages, list):
                        continue
                    for m in messages:
                        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
                            log.warning("Skipping malformed WhatsApp message event.")
                            continue
                        try:
                            timestamp = int(m["timestamp"])
                        except (KeyError, TypeError, ValueError):
                            log.warning("Skipping WhatsApp message with invalid timestamp: %s", m["id"])
                            continue
                        if m["id"] in handled or timestamp < st["since"]:
                            continue
                        try:
                            handle(m)
                        except Exception:
                            log.exception("handler crashed on %s", m["id"])
                        handled.add(m["id"])
                        st["handled"].append(m["id"])

            st["offset"] = next_offset
            save_state(st)
    finally:
        bot.close()


if __name__ == "__main__":
    main()
