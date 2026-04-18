#!/usr/bin/env bash
# Build macOS .app + .dmg using PyInstaller.
# Usage: ./build.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

APP_NAME="OptionsDashboard"
VERSION="$(python3 -c 'import version; print(version.VERSION)')"

echo "→ Building $APP_NAME v$VERSION"

# Clean previous builds
rm -rf build dist

# Ensure pyinstaller is on PATH (user-install fallback)
PYI="$(command -v pyinstaller || true)"
if [ -z "$PYI" ]; then
  PYI="$HOME/Library/Python/3.9/bin/pyinstaller"
fi

"$PYI" \
  --noconfirm \
  --windowed \
  --name "$APP_NAME" \
  --osx-bundle-identifier "com.amitlevy.optionsdashboard" \
  app.py

APP_PATH="dist/$APP_NAME.app"
DMG_PATH="dist/$APP_NAME-$VERSION.dmg"

if [ ! -d "$APP_PATH" ]; then
  echo "✗ Build failed — $APP_PATH not found"
  exit 1
fi

echo "→ Creating DMG: $DMG_PATH"
rm -f "$DMG_PATH"
hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$APP_PATH" \
  -ov -format UDZO \
  "$DMG_PATH"

echo "✓ Done"
echo "  App: $APP_PATH"
echo "  DMG: $DMG_PATH"
echo
echo "Next: upload DMG to a GitHub release:"
echo "  gh release create v$VERSION \"$DMG_PATH\" --title \"v$VERSION\" --notes \"...\""
