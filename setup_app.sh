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
# Pin pip install to the SAME python the launcher will use. We pick it by
# the same priority order as the launcher and persist that path in the
# repo so the launcher reads the *exact* same interpreter at runtime.
PY="/usr/bin/python3"
[ -x "$PY" ] || PY="/usr/local/bin/python3"
[ -x "$PY" ] || PY="/opt/homebrew/bin/python3"
[ -x "$PY" ] || { echo "✗ python3 not found anywhere"; exit 1; }
echo "→ Using python: $PY"
"$PY" --version
"$PY" -m pip --version || {
    echo "✗ pip not available for $PY"
    echo "  Try: $PY -m ensurepip --user"
    exit 1
}

echo "→ Installing Python libraries (output below — watch for errors)..."
# No --quiet so the user sees real failures. The first install attempts
# the standard --user path; if that fails (typically PEP 668 / externally-
# managed-environment), retry with --break-system-packages.
INSTALL_OK=0
if "$PY" -m pip install --user \
        PyQt6 requests websockets matplotlib ib_insync; then
    INSTALL_OK=1
elif "$PY" -m pip install --user --break-system-packages \
        PyQt6 requests websockets matplotlib ib_insync; then
    INSTALL_OK=1
fi
if [ "$INSTALL_OK" -ne 1 ]; then
    echo "✗ pip install failed. Scroll up for the real error."
    exit 1
fi

# Hard verify: actually import PyQt6 with the SAME python. Surfaces any
# remaining mismatch (mixed archs, wrong site-packages, etc.) right here
# instead of as an opaque ModuleNotFoundError on first launch.
if ! "$PY" -c "import PyQt6.QtWidgets" 2>&1; then
    echo "✗ PyQt6 install reported success but $PY can't import it."
    echo "  $PY -m pip show PyQt6"
    "$PY" -m pip show PyQt6 2>&1 | head -10
    exit 1
fi
echo "✓ Libraries installed and verified (for $PY)"

# Persist the chosen python path so the launcher reads exactly the same
# interpreter every time, regardless of $PATH at run time.
echo "$PY" > "$INSTALL_DIR/.python_path"

# 4. Build a real macOS .app bundle on the Desktop.
#    No AppleScript wrapper — the bundle's executable is a shell script that
#    `exec`s python3, so the .app and the python process share a PID. macOS
#    treats it as a normal foreground app: dock icon while running, gone when
#    the user quits, GUI errors visible if python crashes.
echo "→ Creating Options Dashboard launcher on Desktop..."
rm -rf "$LAUNCHER"
mkdir -p "$LAUNCHER/Contents/MacOS"
mkdir -p "$LAUNCHER/Contents/Resources"

# Copy the app icon from the cloned repo into the launcher bundle
if [ -f "$INSTALL_DIR/AppIcon.icns" ]; then
    cp "$INSTALL_DIR/AppIcon.icns" "$LAUNCHER/Contents/Resources/AppIcon.icns"
fi

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
    <key>CFBundleIconFile</key>        <string>AppIcon</string>
    <key>CFBundleIconName</key>        <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>  <string>10.15</string>
    <key>NSHighResolutionCapable</key> <true/>
    <key>NSRequiresAquaSystemAppearance</key> <false/>
    <!-- Prefer native arch so PyQt6 (installed as arm64 on Apple Silicon)
         loads correctly. Without this macOS sometimes runs the launcher
         under Rosetta (x86_64), which can't dlopen arm64 dylibs. -->
    <key>LSArchitecturePriority</key>
    <array>
        <string>arm64</string>
        <string>x86_64</string>
    </array>
    <key>LSRequiresNativeExecution</key> <true/>
</dict>
</plist>
PLIST

cat > "$LAUNCHER/Contents/MacOS/OptionsDashboard" << 'LAUNCHER_SH'
#!/bin/bash
# Launcher for Options Dashboard. Runs the cloned Python app from
# ~/Applications/OptionsDashboard. Every step is logged so we can debug
# if something doesn't work.
LOG="$HOME/Library/Logs/OptionsDashboard.log"
mkdir -p "$(dirname "$LOG")"

{
    echo ""
    echo "═══ launcher started at $(date) ═══"
    echo "PATH=$PATH"
} >> "$LOG"

REPO="$HOME/Applications/OptionsDashboard"
if [ ! -d "$REPO" ]; then
    echo "ERROR: repo missing at $REPO" >> "$LOG"
    osascript -e "display dialog \"Options Dashboard not installed at $REPO. Re-run the setup command.\" buttons {\"OK\"} default button \"OK\" with icon stop" >>"$LOG" 2>&1
    exit 1
fi

PY=""
# Prefer the python recorded by setup_app.sh — guarantees the launcher
# uses the same interpreter pip installed against.
PINFILE="$REPO/.python_path"
if [ -r "$PINFILE" ]; then
    p="$(head -1 "$PINFILE")"
    [ -x "$p" ] && PY="$p"
fi
if [ -z "$PY" ]; then
    for cand in /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
        if [ -x "$cand" ]; then PY="$cand"; break; fi
    done
fi
if [ -z "$PY" ]; then
    echo "ERROR: no python3 found" >> "$LOG"
    osascript -e "display dialog \"python3 not found. In Terminal: xcode-select --install\" buttons {\"OK\"} default button \"OK\" with icon stop" >>"$LOG" 2>&1
    exit 1
fi
echo "Using python: $PY" >> "$LOG"

cd "$REPO" || { echo "ERROR: cd failed" >> "$LOG"; exit 1; }
echo "CWD: $(pwd)" >> "$LOG"

# Force python to run under the kernel's native arch. Otherwise the .app
# can launch under Rosetta (x86_64), and PyQt6 — installed as arm64 by pip
# — will fail to dlopen with "incompatible architecture".
ARCH="$(uname -m)"
echo "Launching python3 app.py under arch=$ARCH..." >> "$LOG"
arch -"$ARCH" "$PY" app.py >>"$LOG" 2>&1
EC=$?
echo "python3 exited with code $EC at $(date)" >> "$LOG"

# Show the tail of the log on a real crash (any non-zero except SIGINT/Cmd+Q).
if [ $EC -ne 0 ] && [ $EC -ne 130 ] && [ $EC -ne 143 ]; then
    TAIL=$(tail -n 25 "$LOG" | sed 's/"/\\"/g; s/\\$/\\\\/g')
    osascript -e "display dialog \"Options Dashboard crashed (exit $EC).\n\n$TAIL\" buttons {\"OK\"} default button \"OK\" with icon stop" >>"$LOG" 2>&1
fi

exit $EC
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
