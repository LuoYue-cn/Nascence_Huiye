#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$TASK_ROOT"
WITH_OLLAMA=false
PULL_MODEL=false
for arg in "$@"; do
  case "$arg" in
    --with-ollama) WITH_OLLAMA=true ;;
    --pull-model) PULL_MODEL=true ;;
    *) echo "Usage: bash setup.sh [--with-ollama] [--pull-model]" >&2; exit 2 ;;
  esac
done
python3 -m venv venv
"$TASK_ROOT/venv/bin/python" -m pip install -r requirements.txt
command -v npm >/dev/null || { echo 'Install Node.js 20+ and npm before building the management panel.' >&2; exit 1; }
npm ci --prefix frontend
npm run build --prefix frontend
export OLLAMA_MODELS="${OLLAMA_MODELS:-$TASK_ROOT/ollama/models}"
mkdir -p "$OLLAMA_MODELS"
TASK_OLLAMA_PID=''
TASK_TEMP=''
cleanup() {
  if [ -n "$TASK_OLLAMA_PID" ]; then kill "$TASK_OLLAMA_PID" 2>/dev/null || true; wait "$TASK_OLLAMA_PID" 2>/dev/null || true; fi
  if [ -n "$TASK_TEMP" ]; then rm -rf "$TASK_TEMP"; fi
}
trap cleanup EXIT
if $WITH_OLLAMA; then
  case "$(uname -s)" in
    Linux)
      case "$(uname -m)" in x86_64|amd64) TASK_ARCH=amd64 ;; aarch64|arm64) TASK_ARCH=arm64 ;; *) echo 'Unsupported CPU architecture' >&2; exit 1 ;; esac
      command -v zstd >/dev/null || { echo 'Install zstd to extract the official Ollama archive.' >&2; exit 1; }
      TASK_TEMP="$(mktemp -d "$TASK_ROOT/.ollama-install.XXXXXX")"
      TASK_URL="https://ollama.com/download/ollama-linux-${TASK_ARCH}.tar.zst"
      curl --fail --location --retry 3 "$TASK_URL" -o "$TASK_TEMP/ollama.tar.zst"
      mkdir "$TASK_TEMP/runtime"
      tar --zstd -xf "$TASK_TEMP/ollama.tar.zst" -C "$TASK_TEMP/runtime"
      test -x "$TASK_TEMP/runtime/bin/ollama"
      mkdir -p "$TASK_ROOT/ollama"
      # Keep the complete bin/lib runtime; replacing bin/lib removes stale libraries.
      for part in bin lib; do
        if [ -d "$TASK_TEMP/runtime/$part" ]; then
          if [ -d "$TASK_ROOT/ollama/$part" ]; then mv "$TASK_ROOT/ollama/$part" "$TASK_TEMP/previous-$part"; fi
          mv "$TASK_TEMP/runtime/$part" "$TASK_ROOT/ollama/$part"
        fi
      done
      ;;
    Darwin) echo 'Install the official Ollama macOS application from https://ollama.com/download/mac and start it before pulling models.' >&2; exit 1 ;;
    *) echo 'Use setup.ps1 on Windows.' >&2; exit 1 ;;
  esac
fi
if $PULL_MODEL; then
  TASK_BIN="$TASK_ROOT/ollama/bin/ollama"
  if [ ! -x "$TASK_BIN" ]; then TASK_BIN="$(command -v ollama || true)"; fi
  test -n "$TASK_BIN" || { echo 'Ollama executable is unavailable.' >&2; exit 1; }
  if ! curl --fail --silent --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null; then
    "$TASK_BIN" serve > "$TASK_ROOT/ollama/setup.log" 2>&1 &
    TASK_OLLAMA_PID=$!
    for _ in $(seq 1 60); do
      if curl --fail --silent --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null; then break; fi
      kill -0 "$TASK_OLLAMA_PID" || { echo 'Ollama exited during startup.' >&2; exit 1; }
      sleep 1
    done
    curl --fail --silent --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null
  fi
  TASK_MODEL="$("$TASK_ROOT/venv/bin/python" -c 'from config.api_config import config; print(config["ollama_embed_model"])')"
  "$TASK_BIN" pull "$TASK_MODEL"
fi
echo 'Setup complete. Start the QQ bot and management panel with bash start.sh.'
