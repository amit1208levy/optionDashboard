#!/usr/bin/env bash
# Launches the Options Dashboard. Installs deps on first run.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

PY="${PYTHON:-python3}"

# First-run dep check: if PyQt6 missing, install requirements.
if ! "$PY" -c "import PyQt6" >/dev/null 2>&1; then
  echo "First run — installing dependencies…"
  "$PY" -m pip install --user -r requirements.txt
fi

exec "$PY" app.py
