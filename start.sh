#!/usr/bin/env bash
# Mooring Twin - one-click start (Linux / macOS / a cloud VM).
# Builds the web UI when it is missing or out of date (needs Node.js once), then starts the backend, which serves the
# UI. Ctrl+C stops it.   ./start.sh --rebuild   forces a fresh UI build;  --no-browser  for a server without a desktop.
set -e
cd "$(dirname "$0")"
[ -f .venv/bin/activate ] && . .venv/bin/activate
PY=$(command -v python3 || command -v python) || { echo "Python 3.10+ not found"; exit 1; }
"$PY" -c "import fastapi, uvicorn, sqlalchemy, cv2, reportlab" 2>/dev/null || {
  echo "Some Python packages are missing. Run: pip install -r backend/requirements.txt"; exit 1; }
if [ "$1" = "--rebuild" ] || ! "$PY" tools/ui_check.py >/dev/null; then
  if command -v npm >/dev/null; then
    echo "[UI] Building the web UI..."
    (cd frontend && { [ -d node_modules ] || npm install; } && npm run build)
  elif [ -f frontend/dist/index.html ]; then
    echo "[UI] Node.js not found - using the existing UI build, which may be out of date."
  else
    echo "[UI] The web UI is not built and Node.js is not installed (https://nodejs.org)."; exit 1
  fi
fi
OPEN="--open"; for a in "$@"; do [ "$a" = "--no-browser" ] && OPEN=""; done
exec "$PY" backend/main.py $OPEN
