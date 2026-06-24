#!/usr/bin/env bash
# Build, sign, notarize, and staple Semester.app for distribution outside the App Store.
#
# One-time setup (creates a Keychain profile so your password isn't typed each run):
#   xcrun notarytool store-credentials semester-notary \
#     --apple-id "YOUR_APPLE_ID_EMAIL" --team-id 7MX978TCBY --password "APP_SPECIFIC_PASSWORD"
#   (make an app-specific password at https://appleid.apple.com → Sign-In and Security)
#
# Then just run:  bash sign_mac.sh
set -e
cd "$(dirname "$0")"

IDENTITY="Developer ID Application: Ramon Ledesma (7MX978TCBY)"
BUNDLE_ID="com.ramonledesma.semester"
PROFILE="semester-notary"
APP="dist/Semester.app"

echo "▸ Building (onedir, notarization-friendly)…"
python3 -m pip install --quiet --upgrade pyinstaller requests pywebview pillow
python3 make_icon.py || true
rm -rf build dist ./*.spec
pyinstaller --windowed --onedir --name "Semester" --icon icon.icns \
  --osx-bundle-identifier "$BUNDLE_ID" --collect-all webview app.py

echo "▸ Signing with hardened runtime…"
codesign --deep --force --timestamp --options runtime \
  --entitlements entitlements.plist --sign "$IDENTITY" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
echo "✓ Signed."

if [ "${1:-}" = "--sign-only" ]; then
  echo "Skipping notarization (--sign-only)."; exit 0
fi

echo "▸ Notarizing (uploads to Apple, waits for result)…"
ditto -c -k --keepParent "$APP" "Semester-macOS.zip"
xcrun notarytool submit "Semester-macOS.zip" --keychain-profile "$PROFILE" --wait
echo "▸ Stapling ticket…"
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
ditto -c -k --keepParent "$APP" "Semester-macOS.zip"
echo "✓ Done — Semester-macOS.zip is signed + notarized and runs with no Gatekeeper warning."
