# ============================================================
# Stage 1 — C++ Builder
# ============================================================
FROM ubuntu:22.04 AS cpp-builder
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    cmake \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY . .
RUN cmake -S . -B /tmp/chatbot-build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /tmp/chatbot-build --parallel

# ============================================================
# Stage 2 — Runtime (Python + C++ binary)
# ============================================================
FROM python:3.10-slim AS runtime

# Core Python settings + chatbot user/binary path + dynamic Python exe for C++ subprocess
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHATBOT_USER=Ayan \
    CHATBOT_EXE=/app/chatbot_linux \
    PYTHON_EXECUTABLE=python3

WORKDIR /app

# --- System packages ---
# ffmpeg:          decode audio/ogg, audio/opus sent by WhatsApp before passing to Gemini
# libstdc++6:      C++ runtime required by chatbot_linux
# ca-certificates: TLS for Gemini / WhatsApp Meta API calls
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    ffmpeg \
    libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app \
    && mkdir -p /app/temp /app/audit_logs /tmp/wa_media

# --- Python dependencies ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Application files ---
COPY --from=cpp-builder /tmp/chatbot-build/chatbot_linux ./chatbot_linux
COPY --chown=app:app . .
RUN chown -R app:app /app

USER app
CMD ["python3", "whatsapp_agent.py"]
