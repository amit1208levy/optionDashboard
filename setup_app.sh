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

# 4. Build the launcher .app on the Desktop.
#    AppleScript app runs python3 in the background, no Terminal window.
echo "→ Creating Options Dashboard launcher on Desktop..."
LAUNCHER_SRC="$(mktemp /tmp/launcher_XXXXXX.applescript)"
cat > "$LAUNCHER_SRC" << 'APPLESCRIPT'
on run
    set repoPath to (POSIX path of (path to home folder)) & "Applications/OptionsDashboard"
    try
        do shell script "test -d " & quoted form of repoPath
    on error
        display dialog "Options Dashboard isn't installed." & return & ¬
            "Run the setup command again." buttons {"OK"} default button "OK" with icon stop
        return
    end try
    do shell script "cd " & quoted form of repoPath & ¬
        " && /usr/bin/env python3 app.py > /dev/null 2>&1 &"
end run
APPLESCRIPT

rm -rf "$LAUNCHER"
osacompile -o "$LAUNCHER" "$LAUNCHER_SRC"
rm "$LAUNCHER_SRC"
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
