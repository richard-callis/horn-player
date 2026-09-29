#!/bin/sh
# Run the app locally against the UNVR, for development. Credentials come from horn.env.
cd "$(dirname "$0")"
set -a; . ./horn.env; set +a   # UNIFI_HOST, UNIFI_USER, UNIFI_PASS
export DATA_DIR=${DATA_DIR:-./data} \
       PIPER_VOICE=./voices/en_US-lessac-medium.onnx PIPER_BIN=./.venv/bin/piper
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port ${PORT:-8765}
