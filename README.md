# Hybrid C++/Python Chatbot

A production-ready hybrid chatbot with a **C++ terminal interface** and a **Python/Gemini API backend**.
The C++ core manages all application state, IPC, and user interaction; Python handles network communication with the Gemini API.

---

## Key Features

- **Hybrid architecture**: C++ manages the CLI, conversation state, session memory, and subprocess orchestration; Python handles Gemini API communication.
- **Novelty: Self-Accountable AI**: The system monitors its own reliability and logs every interaction.
  - **Decision Audit Trail**: Every AI response is permanently logged with its query, model, generation time, and confidence score.
  - **Trust Degradation Protocol**: Users can rate responses. Wrong answers reduce the AI's internal trust score, prompting transparency advisories on future responses if trust drops too low.
- **Gemini backend**: Uses the v1beta `generateContent` endpoint and supports text and inline image/audio/document parts.
- **Dynamic configuration**: Load `config.json` at startup to override the model, turn limits, and history size without recompiling.
- **Unified logger**: Thread-safe `Logger` utility writes ISO-8601 timestamped entries to `app.log` from all components.
- **500-word response limit**: Replies are word-capped and Markdown-stripped for clean terminal output.
- **Smart context management**: Retains only the last 10 exchanges (20 messages) to prevent API payload bloat and latency.
- **Crash-proof IPC**: UUID v4 filenames for request/response JSON files prevent cross-session collisions; a bounded subprocess timeout prevents the system from hanging.
- **Memory safety**: All heap objects managed via `std::unique_ptr` — zero raw owning pointers.
- **Input sanitisation**: Two-layer filtering (character-level + jailbreak pattern detection) before any message reaches the API.
- **Session persistence**: Chat history saved to a per-user file with automatic rotation when size exceeds 1 MB.
- **Session export**: `/export` writes the current conversation to a timestamped Markdown file.

---

## Essential Files

| Path | Purpose |
|---|---|
| `src/` | C++ source files (main, chatbot, api bridge, memory, user, AuditLogger, TrustTracker) |
| `include/` | C++ headers |
| `api.py` | Python/Gemini API bridge |
| `CMakeLists.txt` | Build configuration with FetchContent for `nlohmann/json` |
| `requirements.txt` | Python dependencies (`requests>=2.31.0`) |
| `config.json` | Optional runtime configuration |
| `.gitignore` | Shields build artifacts, secrets, logs, and IPC files |

---

## Build and Run

```powershell
cd C:\AI_Chatbot_Project
cmake -S . -B build -G Ninja
cmake --build build
.\build\chatbot.exe
```

> `nlohmann/json` is fetched automatically by CMake — no manual header installation required.

## Docker (Linux)

Build and run the container with WhatsApp and Gemini credentials supplied at
runtime rather than embedded in the image:

```sh
docker build -t whatsapp-ai-agent .
docker run --name whatsapp-bot \
  -e WA_AGENT_TOKEN \
  -e GEMINI_API_KEY \
  -e CHATBOT_USER=Ayan \
  whatsapp-ai-agent
```

The image builds the C++ chatbot with CMake/GCC and launches it through
`whatsapp_agent.py`. Set `CHATBOT_EXE` to override the default
`/app/chatbot_linux`.

Railway can deploy this worker from the repository Dockerfile. Configure
`WA_AGENT_TOKEN` and `GEMINI_API_KEY` as service variables; `CHATBOT_USER`
defaults to `Ayan`. The worker does not expose an HTTP port. Runtime user,
correction, audit, and WhatsApp offset state are stored under `/app`; they are
not durable across container replacement. Persistent-volume path support is
not wired yet, so do not mount a volume over `/app` (it would hide the app and
binary).

---

## Python Setup

```powershell
python -m pip install -r requirements.txt
```

---

## API Key Setup

This project uses the [Gemini API](https://ai.google.dev/gemini-api/docs). Set your key as an environment variable before running:

```powershell
# PowerShell (current session)
$env:GEMINI_API_KEY = "your-gemini-api-key"

# PowerShell (permanent, user-level)
[System.Environment]::SetEnvironmentVariable("GEMINI_API_KEY", "your-key", "User")
```

The default model is `gemini-3.5-flash-lite`. `GEMINI_MODEL` can select
`gemini-3.1-pro-preview`; unsupported model names use the default.

---

## Configuration (`config.json`)

Create `config.json` in the project root to override defaults:

```json
{
    "model": "openai/gpt-4o-mini",
    "maxTurns": 1000,
    "maxHistoryMessages": 10
}
```

Any key omitted falls back to its default value. A malformed file is caught gracefully and defaults are used.

---

## Commands

| Command | Action |
|---|---|
| `/help` | Show command list |
| `/clear` | Clear conversation history |
| `/history` | Display saved session history |
| `/export` | Export session as a Markdown file |
| `/exit` | Quit the chatbot |

### Self-Accountable AI Commands

| Command | Action |
|---|---|
| `/trust` | Show AI reliability score |
| `/feedback correct` | Confirm last answer was correct (+2.5% trust) |
| `/feedback wrong` | Report last answer was wrong (-7.0% trust) |
| `/wrong` | Alias: mark last answer as wrong |
| `/audit last` | Show the latest audit record |
| `/audit <id>` | Show specific audit record by ID (e.g., `msg_0001`) |

---

## Novelty: Self-Accountable AI

### The Problem
Traditional conversational AI systems operate as "black boxes." When they make mistakes or hallucinate, they often do so with unwavering confidence. Users have no visibility into the AI's internal uncertainty, the specific model version used, or the history of previous corrections. This lack of transparency erodes user trust, especially in professional or critical environments.

### The Proposed Solution
The **Self-Accountable AI** feature set transforms the chatbot from a static oracle into a transparent, self-monitoring system. It introduces two core pillars:

1. **Decision Audit Trail (`AuditLogger`)**: A permanent, structured log of every interaction.
2. **Trust Degradation Protocol (`TrustTracker`)**: A dynamic reliability score that degrades when the AI makes mistakes and recovers when it succeeds.

### System Architecture & Data Flow

```text
User Input 
   │
   ▼
[Chatbot Engine] ──(1. Check Trust)──► [TrustTracker]
   │                                     ▲
   │ (2. API Call)                       │ (4. User Feedback)
   ▼                                     │
[Gemini API]                             │
   │                                     │
   ▼                                     │
[Chatbot Engine] ──(3. Log Record)───► [AuditLogger]
   │                                     │
   ▼                                     ▼
 Terminal Output                 audit_logs/user_audit.jsonl
 (with Transparency              audit_logs/<user>_trust.json
  Report Footer)
```

### Algorithms

#### Confidence Heuristic
The AI computes a pseudo-confidence score for every response before displaying it:
- **Base Score**: Starts at 0.87 (87%).
- **Length Penalty**: Subtracts 0.15 if the response is suspiciously short (< 20 chars).
- **Error Penalty**: Subtracts 0.30 if the response indicates an API or system error.
- **Systemic Trust Penalty**: Subtracts 0.05 if the global trust score is < 70, and a further 0.05 if < 50.
- **Clamping**: The final score is bound between 5% and 99%.

#### Trust Decay Formula
The system maintains a user-specific trust score (initial: 100.0).
- **`/feedback wrong`**: Subtracts 7.0 from the score (clamped at 0.0).
- **`/feedback correct`**: Adds 2.5 to the score (clamped at 100.0).

If the score drops below 70, the AI begins prepending a warning to its responses (*"My confidence in this topic is reduced..."*). If it drops below 40, the warning becomes critical.

### Benefits & Future Scope
- **Benefits**: Enforces accountability, provides immediate visibility into system state, and empowers the user to actively shape the AI's reliability profile.
- **Future Scope**: 
  - Dynamic model switching (e.g., falling back to a smarter, more expensive model if trust drops below 50).
  - Web dashboard for parsing and visualising the `.jsonl` audit trails.
  - Context-aware trust (maintaining separate scores for different conversation topics).

---

## Notes

- Do **not** commit `.env`, API keys, or `config.json` containing secrets — `.gitignore` excludes all of these.
- IPC files (`request_*.json`, `response_*.json`) are ephemeral and also excluded from version control.
