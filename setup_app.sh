#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_app.sh  —  One-time installer for OptionsDashboard
#
# What this does:
#   1. Installs Apple Command Line Tools (git + python3) if missing
#   2. Clones the repo into ~/Applications/OptionsDashboard
#   3. Installs the Python libraries the app needs
#   4. Creates an "Options Dashboard.app" launcher on the Desktop
#   5. Opens the app
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/amit1208levy/optionDashboard/main/setup_app.sh | bash
# ─────────────────────────────────────────────────────────────────────────────
set -e

REPO_URL="https://github.com/amit1208levy/optionDashboard.git"
INSTALL_DIR="$HOME/Applications/OptionsDashboard"
APP_NAME="Options Dashboard.app"
LAUNCHER="$HOME/Desktop/$APP_NAME"

echo ""
echo "────────────────────────────────────────────────"
echo "  Installing Options Dashboard"
echo "────────────────────────────────────────────────"
echo ""

# 1. Make sure git + python3 are available (Command Line Tools)
if ! xcode-select -p &>/dev/null; then
    echo "→ Installing Apple Command Line Tools (this includes git and python3)..."
    echo "  A popup will appear — click 'Install' and wait until it finishes."
    echo "  Then re-run this command."
    xcode-select --install 2>/dev/null || true
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    echo "✗ python3 not found. Open the App Store and install Xcode, or run:"
    echo "    xcode-select --install"
    exit 1
fi

echo "✓ Command Line Tools present"

# 2. Clone or update the repo
mkdir -p "$HOME/Applications"
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "→ Updating existing install at $INSTALL_DIR..."
    git -C "$INSTALL_DIR" pull --ff-only --quiet
else
    echo "→ Cloning into $INSTALL_DIR..."
    rm -rf "$INSTALL_DIR"
    git clone --quiet "$REPO_URL" "$INSTALL_DIR"
fi
echo "✓ Repo ready"

# 3. Install Python dependencies
echo "→ Installing Python libraries (this can take a minute)..."
python3 -m pip install --user --quiet --upgrade pip 2>/dev/null || true
python3 -m pip install --user --quiet \
    PyQt6 requests websockets matplotlib ib_insync \
    || python3 -m pip install --user --break-system-packages --quiet \
        PyQt6 requests websockets matplotlib ib_insync
echo "✓ Libraries installed"

# 4. Build a real macOS .app bundle on the Desktop.
#    No AppleScript wrapper — the bundle's executable is a shell script that
#    `exec`s python3, so the .app and the python process share a PID. macOS
#    treats it as a normal foreground app: dock icon while running, gone when
#    the user quits, GUI errors visible if python crashes.
echo "→ Creating Options Dashboard launcher on Desktop..."
rm -rf "$LAUNCHER"
mkdir -p "$LAUNCHER/Contents/MacOS"

cat > "$LAUNCHER/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTD/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>Options Dashboard</string>
    <key>CFBundleDisplayName</key>     <string>Options Dashboard</string>
    <key>CFBundleExecutable</key>      <string>OptionsDashboard</string>
    <key>CFBundleIdentifier</key>      <string>com.amitlevy.optionsdashboard.launcher</string>
    <key>CFBundleInfoDictionaryVersion</key> <string>6.0</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleShortVersionString</key> <string>1.0</string>
    <key>CFBundleVersion</key>         <string>1</string>
    <key>LSMinimumSystemVersion</key>  <string>10.15</string>
    <key>NSHighResolutionCapable</key> <true/>
    <key>NSRequiresAquaSystemAppearance</key> <false/>
</dict>
</plist>
PLIST

cat > "$LAUNCHER/Contents/MacOS/OptionsDashboard" << 'LAUNCHER_SH'
#!/bin/bash
# Launcher for Options Dashboard. Runs the cloned Python app from
# ~/Applications/OptionsDashboard. If anything goes wrong, surface a
# dialog instead of failing silently.
set -o pipefail

REPO="$HOME/Applications/OptionsDashboard"
LOG="$HOME/Library/Logs/OptionsDashboard.log"

if [ ! -d "$REPO" ]; then
    osascript -e "display dialog \"Options Dashboard isn't installed.\nExpected: $REPO\n\nRe-run the setup command in Terminal.\" buttons {\"OK\"} default button \"OK\" with icon stop"
    exit 1
fi

# Find a working python3
PY=""
for cand in /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
    if [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
    osascript -e 'display dialog "python3 not found.\n\nIn Terminal, run:\n  xcode-select --install" buttons {"OK"} default button "OK" with icon stop'
    exit 1
fi

cd "$REPO" || exit 1
mkdir -p "$(dirname "$LOG")"

# Run python in the foreground so the .app stays in the dock as long as the
# GUI is up. Tee output to the log AND keep stderr for the trap below.
exec "$PY" app.py 2>&1 | tee -a "$LOG"
LAUNCHER_SH

chmod +x "$LAUNCHER/Contents/MacOS/OptionsDashboard"
echo "✓ Launcher placed at: $LAUNCHER"

# 5. Open the app
echo ""
echo "→ Launching Options Dashboard..."
open "$LAUNCHER"

echo ""
echo "────────────────────────────────────────────────"
echo "  ✓  Installed!"
echo ""
echo "  From now on:"
echo "    Double-click 'Options Dashboard' on your Desktop"
echo ""
echo "  Updates are automatic from inside the app."
echo "────────────────────────────────────────────────"
