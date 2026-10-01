#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$TASK_ROOT"
test -x venv/bin/python || { echo 'Run bash setup.sh first.' >&2; exit 1; }
test -f frontend/dist/index.html || { echo 'Build the panel with npm ci --prefix frontend && npm run build --prefix frontend.' >&2; exit 1; }
export OLLAMA_MODELS="${OLLAMA_MODELS:-$TASK_ROOT/ollama/models}"
TASK_OLLAMA_PID=''
TASK_APP_PID=''
cleanup() {
  if [ -n "$TASK_APP_PID" ]; then kill -TERM "$TASK_APP_PID" 2>/dev/null || true; wait "$TASK_APP_PID" 2>/dev/null || true; fi
  if [ -n "$TASK_OLLAMA_PID" ]; then kill "$TASK_OLLAMA_PID" 2>/dev/null || true; wait "$TASK_OLLAMA_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
TASK_URL="$(venv/bin/python -c 'from config.api_config import config; print(config["ollama_base_url"].rstrip("/"))')"
if [[ "$TASK_URL" = http://localhost:11434 || "$TASK_URL" = http://127.0.0.1:11434 ]]; then
  if ! curl --fail --silent --max-time 2 "$TASK_URL/api/tags" >/dev/null && [ -x ollama/bin/ollama ]; then
    mkdir -p ollama
    ollama/bin/ollama serve >ollama/service.log 2>&1 &
    TASK_OLLAMA_PID=$!
    for _ in $(seq 1 30); do
      if curl --fail --silent --max-time 2 "$TASK_URL/api/tags" >/dev/null; then break; fi
      if ! kill -0 "$TASK_OLLAMA_PID" 2>/dev/null; then break; fi
      sleep 1
    done
  fi
fi
# Management remains accessible when model dependencies are unavailable.
venv/bin/python main.py "$@" &
TASK_APP_PID=$!
wait "$TASK_APP_PID"
TASK_APP_PID=''
