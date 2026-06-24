#!/usr/bin/env bash
# Semester — one-command setup + launch.
# Works on macOS and Linux. Re-run anytime to refresh your dashboard.
set -e
cd "$(dirname "$0")"

echo "🎓 Semester setup"

# 1. Find Python 3
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else
  echo "❌ Python 3 is required but not found. Install it from https://python.org and re-run."
  exit 1
fi
echo "✓ Using $($PY --version)"

# 2. Install dependencies (user-level, no admin needed)
echo "📦 Installing dependencies…"
$PY -m pip install --quiet --user -r requirements.txt 2>/dev/null \
  || $PY -m pip install --quiet -r requirements.txt

# 3. Launch the app — opens the friendly setup screen in your browser on first run
echo "🚀 Launching…"
exec $PY app.py
