FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Piper voice, pinned to a piper-voices revision and checked against known hashes.
ARG VOICE_BASE=https://huggingface.co/rhasspy/piper-voices/resolve/c10ece1aade47bb51c153c893d14e5bf8e5b7117/en/en_US/lessac/medium
RUN mkdir -p /app/voices && cd /app/voices \
 && curl -fsSL -o en_US-lessac-medium.onnx "$VOICE_BASE/en_US-lessac-medium.onnx" \
 && curl -fsSL -o en_US-lessac-medium.onnx.json "$VOICE_BASE/en_US-lessac-medium.onnx.json" \
 && printf '%s  %s\n' \
      5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f en_US-lessac-medium.onnx \
      efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0 en_US-lessac-medium.onnx.json \
    | sha256sum -c -

COPY app ./app

RUN useradd --uid 1000 --create-home horn && mkdir -p /data && chown horn:horn /data
USER 1000
ENV DATA_DIR=/data PIPER_VOICE=/app/voices/en_US-lessac-medium.onnx PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
