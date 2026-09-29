FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ARG VOICE_BASE=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium
RUN mkdir -p /app/voices \
 && curl -fsSL -o /app/voices/en_US-lessac-medium.onnx "$VOICE_BASE/en_US-lessac-medium.onnx" \
 && curl -fsSL -o /app/voices/en_US-lessac-medium.onnx.json "$VOICE_BASE/en_US-lessac-medium.onnx.json"

COPY app ./app

RUN useradd --uid 1000 --create-home horn && mkdir -p /data && chown horn:horn /data
USER 1000
ENV DATA_DIR=/data PIPER_VOICE=/app/voices/en_US-lessac-medium.onnx PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
