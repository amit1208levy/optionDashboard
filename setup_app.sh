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
#    Runs python3 in the foreground so any error is captured and shown
#    in a dialog (instead of silently failing). Output is also written to
#    ~/Library/Logs/OptionsDashboard.log for after-the-fact debugging.
echo "→ Creating Options Dashboard launcher on Desktop..."
LAUNCHER_SRC="$(mktemp /tmp/launcher_XXXXXX.applescript)"
cat > "$LAUNCHER_SRC" << 'APPLESCRIPT'
on run
    set homePath to POSIX path of (path to home folder)
    set repoPath to homePath & "Applications/OptionsDashboard"
    set logFile  to homePath & "Library/Logs/OptionsDashboard.log"

    -- Make sure the repo is there
    try
        do shell script "test -d " & quoted form of repoPath
    on error
        display dialog "Options Dashboard isn't installed at:" & return & ¬
            repoPath & return & return & ¬
            "Re-run the setup command in Terminal:" & return & ¬
            "curl -fsSL https://raw.githubusercontent.com/amit1208levy/optionDashboard/main/setup_app.sh | bash" ¬
            buttons {"OK"} default button "OK" with icon stop
        return
    end try

    -- Locate a working python3
    set pyCmd to ""
    repeat with candidate in {"/usr/bin/python3", "/usr/local/bin/python3", "/opt/homebrew/bin/python3"}
        try
            do shell script "test -x " & quoted form of (contents of candidate)
            set pyCmd to contents of candidate
            exit repeat
        end try
    end repeat
    if pyCmd is "" then
        display dialog "Couldn't find python3 on this Mac." & return & return & ¬
            "Open Terminal and run:  xcode-select --install" ¬
            buttons {"OK"} default button "OK" with icon stop
        return
    end if

    -- Launch python3 in the BACKGROUND, redirecting all output to the log.
    -- Then wait 3 seconds and check whether the process is still alive.
    -- If it died, surface the tail of the log to the user instead of silently
    -- doing nothing.
    set bashCmd to "cd " & quoted form of repoPath & ¬
        " && nohup " & quoted form of pyCmd & " app.py >>" & ¬
        quoted form of logFile & " 2>&1 & echo $! ; disown"
    set pyPid to do shell script bashCmd

    -- Give the GUI a moment to start
    delay 3

    try
        do shell script "kill -0 " & pyPid
        -- still running → success, exit silently
        return
    on error
        -- python exited within 3s → show the last bit of the log
        set tailLog to ""
        try
            set tailLog to do shell script "tail -n 40 " & quoted form of logFile
        end try
        display dialog "Options Dashboard quit during startup." & return & return & ¬
            "Last log lines:" & return & tailLog ¬
            buttons {"OK"} default button "OK" with icon stop
    end try
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
