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

FROM python:3.10-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHATBOT_USER=Ayan \
    CHATBOT_EXE=/app/chatbot_linux

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app \
    && mkdir -p /app/temp /app/audit_logs

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=cpp-builder /tmp/chatbot-build/chatbot_linux ./chatbot_linux
COPY --chown=app:app . .
RUN chown -R app:app /app

USER app
CMD ["python", "whatsapp_agent.py"]
