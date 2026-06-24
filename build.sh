#!/usr/bin/env bash
# Build a double-click Semester app for THIS computer's OS with PyInstaller.
# (For all three OSes at once, push a GitHub Release — see .github/workflows/build.yml)
set -e
cd "$(dirname "$0")"

python3 -m pip install --quiet --upgrade pyinstaller requests pywebview pillow
python3 make_icon.py || true   # (re)generate the S icon

COMMON="--onefile --noconfirm --collect-all webview"
case "$(uname -s)" in
  Darwin)
    pyinstaller --windowed --name "Semester" --icon icon.icns $COMMON app.py
    echo "✓ Built: dist/Semester.app" ;;
  Linux)
    pyinstaller --name semester $COMMON app.py
    echo "✓ Built: dist/semester" ;;
  *)
    pyinstaller --windowed --name Semester --icon icon.ico $COMMON app.py
    echo "✓ Built: dist/Semester.exe" ;;
esac
