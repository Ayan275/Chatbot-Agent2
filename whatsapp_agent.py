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
import os, io, re, sys, json, time, queue, asyncio, logging, tempfile, threading, subprocess
from pathlib import Path
import requests, edge_tts

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
OK_AUDIO = ("audio/ogg", "audio/mpeg", "audio/aac", "audio/wav", "audio/flac")
BLOCKED_CMDS = ("/exit", "exit", "/changepassword")
MAX_LINE = 1900            # chatbot hard limit is 2000 characters per message
TURN_TIMEOUT = 40          # C++ API bridge has a 25 s hard timeout

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
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.q = queue.Queue()
        self.proc = subprocess.Popen([str(self.exe), "--agent", self.user], cwd=str(BASE_DIR),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=env, bufsize=0)
        threading.Thread(target=self._pump, args=(self.proc, self.q), daemon=True).start()
        banner = self._read_ready(60)
        log.info("chatbot started: %s", (banner.strip().splitlines() or ["?"])[-1])

    def _kill(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass
        self.proc = None

    def _read_ready(self, timeout):
        buf, deadline = b"", time.monotonic() + timeout
        while True:
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
                raise ChatbotDied(buf.decode("utf-8", "replace").strip()[-300:] or "chatbot exited")
            buf += chunk
            i = buf.find(self.MARK)
            if i >= 0:
                return buf[:i].decode("utf-8", "replace")

    def ask(self, line: str) -> str:
        """Type one line into the chatbot; return everything it printed before the next prompt."""
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            while not self.q.empty():          # drop stray leftovers
                self.q.get_nowait()
            self.proc.stdin.write((line.replace("\r", " ").replace("\n", " ") + "\n").encode("utf-8"))
            self.proc.stdin.flush()
            out = self._read_ready(TURN_TIMEOUT)
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
    sys.exit("chatbot executable not found. Build it first with CMake or set CHATBOT_EXE.")


def find_user() -> str:
    u = os.getenv("CHATBOT_USER", "").strip()
    if u:
        return u
    try:
        names = [x["username"] for x in json.load(open(BASE_DIR / "users.json", encoding="utf-8"))]
    except Exception:
        names = []
    if len(names) == 1:
        return names[0]
    sys.exit("Set CHATBOT_USER to one of your chatbot users: " + (", ".join(names) or "(none found in users.json)")
             + '\n  PowerShell:  $env:CHATBOT_USER="Ayan"')


bot: ChatbotProc  # created in main()


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
        r = HTTP.request(method, BASE + path, headers=HDR, timeout=timeout, **kw)
        if not (r.status_code == 429 or (r.status_code == 503 and "131016" in r.text)):
            return r
        wait = 10 * 2**i
        log.warning("%s %s -> %s, retrying in %ss", method, path, r.status_code, wait)
        time.sleep(wait)
    return r


def send(to, type_, payload):
    r = api_call("POST", "/messages", json={
        "messaging_product": "whatsapp", "to": to, "type": type_, type_: payload})
    if not r.ok:
        log.error("send failed %s: %s", r.status_code, r.text)
    return r.ok


def send_text(to, text):
    text = text or "(empty)"
    for i in range(0, len(text), 4000):   # limit is 4096
        send(to, "text", {"body": text[i:i + 4000]})


def send_audio(to, mp3_bytes):
    r = api_call("POST", "/media",
                 data={"messaging_product": "whatsapp", "type": "audio/mpeg"},
                 files={"file": ("reply.mp3", mp3_bytes, "audio/mpeg")})
    if not r.ok:
        log.error("media upload failed %s: %s", r.status_code, r.text)
        return
    send(to, "audio", {"id": r.json()["id"]})


def mark_read_typing(msg_id):
    try:
        api_call("POST", "/statuses", json={
            "messaging_product": "whatsapp", "status": "read",
            "message_id": msg_id, "typing_indicator": {"type": "text"}})
    except Exception as e:
        log.warning("status failed: %s", e)


def download(media_id):
    meta = api_call("GET", f"/media/{media_id}")
    meta.raise_for_status()
    meta = meta.json()
    r = HTTP.get(meta["url"], headers=HDR, timeout=30)
    r.raise_for_status()
    return r.content, (meta.get("mime_type") or "").split(";")[0]


# ---------- Gemini: ears and eyes ----------
def cfg_model():
    try:
        return json.load(open(BASE_DIR / "config.json", encoding="utf-8")).get("model")
    except Exception:
        return None


def gem(parts, budget=15):
    text, _ = gemini._call_gemini(gemini._pick_model(cfg_model()), "",
                                  [{"role": "user", "parts": parts}], 0.1, budget)
    return text.strip()


def transcribe(data, mime):
    mime = mime if mime in OK_AUDIO else "audio/ogg"
    return gem([gemini.media_part(data, mime),
                {"text": "Transcribe this voice note exactly as spoken, in the original language and "
                         "script. Output only the transcript."}], 40)


def digest(kind, data=None, mime=None, text=None):
    if kind == "image":
        ask = ("Describe this image in detail and transcribe any visible text. "
               "Plain text only, no markdown, at most 1200 characters.")
    else:
        ask = ("Summarise this document faithfully: topic, key points, important numbers, names and dates. "
               "Plain text only, no markdown, at most 1500 characters.")
    first = gemini.media_part(data, mime) if data is not None else {"text": text}
    return gem([first, {"text": ask}])


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
    path = tempfile.mktemp(suffix=".mp3")
    asyncio.run(edge_tts.Communicate(text, pick_voice(text)).save(path))
    try:
        return open(path, "rb").read()
    finally:
        os.remove(path)


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
        data, mime = download(msg["audio"]["id"])
        heard = safe(transcribe(data, mime))
        log.info("transcript: %s", heard)
        if not heard:
            send_text(to, "Awaaz samajh nahi aayi, dobara bolo?")
            return
        line, voice = heard, True

    elif t == "image":
        data, mime = download(msg["image"]["id"])
        cap = safe(msg["image"].get("caption") or "Is image ki summary do", 300)
        line = safe(f"{cap} [Attached image: {safe(digest('image', data, mime or 'image/jpeg'), 1500)}]")

    elif t == "document":
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

    else:
        return   # stickers, video, reactions: ignored

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
        return json.load(open(STATE_FILE))
    except Exception:
        return {"offset": 0, "since": int(time.time()), "handled": []}   # first run: ignore old backlog


def save_state(st):
    st["handled"] = st["handled"][-500:]
    json.dump(st, open(STATE_FILE, "w"))


def main():
    global bot
    bot = ChatbotProc(find_exe(), find_user())
    bot.ask("/help")      # start the chatbot now so the first real message is not slow; also proves login works
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

            data = r.json()
            for entry in data.get("entry", []):
                for ch in entry.get("changes", []):
                    for m in ch["value"].get("messages", []):
                        if m["id"] in handled or int(m["timestamp"]) < st["since"]:
                            continue
                        try:
                            handle(m)
                        except Exception:
                            log.exception("handler crashed on %s", m["id"])
                        handled.add(m["id"])
                        st["handled"].append(m["id"])

            st["offset"] = data["next_offset"]   # pass back unchanged
            save_state(st)
    finally:
        bot.close()


if __name__ == "__main__":
    main()
