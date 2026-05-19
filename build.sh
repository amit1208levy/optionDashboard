#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# build.sh  —  Build OptionsDashboard.app + zip for distribution
#
# Usage:
#   cd /path/to/dashboard
#   ./build.sh
#
# Output:
#   dist/OptionsDashboard.app   — the macOS app bundle (double-click to open)
#   dist/OptionsDashboard.zip   — shareable zip (AirDrop, email, iCloud, etc.)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

APP_NAME="OptionsDashboard"
VERSION="$(python3 -c 'from version import VERSION; print(VERSION)')"
APP_PATH="dist/$APP_NAME.app"
ZIP_PATH="dist/$APP_NAME.zip"

echo "═══════════════════════════════════════════════"
echo "  Building  $APP_NAME  v$VERSION"
echo "═══════════════════════════════════════════════"
echo ""

# ── 1. Find PyInstaller ───────────────────────────────────────────────────────
# Prefer `python3 -m PyInstaller` over the standalone script, because the
# script's #! shebang sometimes points to an Xcode-shipped Python that gets
# wiped by Xcode updates ("No such file or directory" on a present file).
PYTHON=""
for candidate in \
    "$(command -v python3 2>/dev/null)" \
    "/opt/homebrew/bin/python3" \
    "/usr/local/bin/python3" \
    "/usr/bin/python3"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ] \
       && "$candidate" -c "import PyInstaller" 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "✗ PyInstaller not found in any python3 on PATH. Install it first:"
    echo "     python3 -m pip install --user pyinstaller"
    exit 1
fi

PYI_VERSION="$("$PYTHON" -m PyInstaller --version 2>/dev/null || echo unknown)"
echo "→ Using PyInstaller: $PYTHON -m PyInstaller ($PYI_VERSION)"
echo ""

# ── 2. Clean previous builds ──────────────────────────────────────────────────
echo "→ Cleaning previous build..."
rm -rf build dist

# ── 3. Run PyInstaller ────────────────────────────────────────────────────────
echo "→ Running PyInstaller..."
"$PYTHON" -m PyInstaller OptionsDashboard.spec --noconfirm

# ── 4. Verify .app was created ────────────────────────────────────────────────
if [ ! -d "$APP_PATH" ]; then
    echo ""
    echo "✗ Build failed — $APP_PATH was not created."
    echo "  Check the output above for errors."
    exit 1
fi
echo ""
echo "✓ App built:  $APP_PATH"
APP_SIZE="$(du -sh "$APP_PATH" | cut -f1)"
echo "  Size: $APP_SIZE"

# ── 4b. Code-sign + notarize (if Developer ID is configured) ─────────────────
# Notarization eliminates the "damaged" / "unidentified developer" warnings
# entirely. If the certificate / notary credentials aren't set up, we fall
# back to ad-hoc signing + the workaround installer.
NOTARY_PROFILE="${NOTARY_PROFILE:-optdash-notary}"
NOTARIZED=false

# Find the Developer ID Application certificate (NOT "Apple Development",
# which is for testing only and can't be used for distribution).
DEVELOPER_ID="$(security find-identity -v -p codesigning 2>/dev/null \
    | grep "Developer ID Application" \
    | head -1 \
    | sed -E 's/.*"(.+)".*/\1/')"

# Extract the Team ID from the cert name, e.g. "...Application: Name (TEAMID)"
TEAM_ID="$(echo "$DEVELOPER_ID" | sed -nE 's/.*\(([A-Z0-9]+)\).*/\1/p')"

if [ -n "$DEVELOPER_ID" ]; then
    # macOS 14+ Sequoia adds the kernel-protected `com.apple.provenance`
    # xattr to every executable created on APFS. codesign rejects this for
    # Mach-O *executables* (but not dylibs or directories) with
    #     "resource fork, Finder information, or similar detritus not allowed"
    # The xattr is unremovable from userspace.
    #
    # Workaround: do all signing on a temporary HFS+ disk image. codesign
    # on HFS+ doesn't reject provenance. The signed bundle is then copied
    # back to dist/ — bit-for-bit, so the embedded signature survives.
    echo ""
    echo "→ Creating clean HFS+ disk image for signing..."
    DMG="/tmp/optdash_sign_$$.dmg"
    DMG_VOL="OptDashSign$$"
    DMG_MNT="/Volumes/$DMG_VOL"
    rm -f "$DMG"
    hdiutil create -size 500m -fs HFS+ -volname "$DMG_VOL" "$DMG" -quiet -ov
    hdiutil attach "$DMG" -quiet -mountpoint "$DMG_MNT"
    DMG_APP="$DMG_MNT/OptionsDashboard.app"

    echo "→ Copying app onto disk image..."
    ditto "$APP_PATH" "$DMG_APP"
    xattr -cr "$DMG_APP" 2>/dev/null || true

    echo ""
    echo "→ Signing with: $DEVELOPER_ID"

    # 1. Sign every dylib / .so. Sequential (not parallel) avoids races when
    #    codesign serially rewrites files on the disk image.
    find "$DMG_APP" \( -name "*.dylib" -o -name "*.so" \) -print0 \
        | xargs -0 -n 1 codesign --force --options=runtime --timestamp \
            --entitlements entitlements.plist \
            --sign "$DEVELOPER_ID" >/dev/null 2>&1 || true

    # 2. Sign all executables in Contents/MacOS.
    find "$DMG_APP/Contents/MacOS" -type f -perm +111 -print0 \
        | xargs -0 -n 1 codesign --force --options=runtime --timestamp \
            --entitlements entitlements.plist \
            --sign "$DEVELOPER_ID" >/dev/null 2>&1 || true

    # 3. Sign the .app bundle itself (this seals the resources).
    codesign --force --options=runtime --timestamp \
        --entitlements entitlements.plist \
        --sign "$DEVELOPER_ID" "$DMG_APP" 2>&1 | grep -v "replacing existing signature" || true

    # 4. Verify signature is valid + complete. Check codesign's exit code
    #    directly — piping to `grep -q` would race (grep exits on first
    #    match, sending SIGPIPE to codesign, which pipefail reports as fail).
    if codesign --verify --strict "$DMG_APP" >/dev/null 2>&1; then
        echo "✓ Signature verified  (Team ID: ${TEAM_ID})"
    else
        echo "✗ Signature verification failed:"
        codesign --verify --strict --verbose=2 "$DMG_APP"
        hdiutil detach "$DMG_MNT" -quiet 2>/dev/null
        rm -f "$DMG"
        exit 1
    fi

    # 5. Notarize (if keychain profile is configured)
    if xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" &>/dev/null; then
        echo ""
        echo "→ Submitting to Apple notary service (~1-5 min wait)..."

        NOTARY_ZIP="$DMG_MNT/notary_submission.zip"
        ditto -c -k --keepParent "$DMG_APP" "$NOTARY_ZIP"

        if xcrun notarytool submit "$NOTARY_ZIP" \
                --keychain-profile "$NOTARY_PROFILE" \
                --wait 2>&1 | tee /tmp/notary.log | grep -q "status: Accepted"; then
            rm -f "$NOTARY_ZIP"
            echo "✓ Notarization accepted"

            # Staple the ticket so the app works offline (no callback to Apple
            # required at launch).
            xcrun stapler staple "$DMG_APP"
            echo "✓ Notarization ticket stapled"
            NOTARIZED=true
        else
            rm -f "$NOTARY_ZIP"
            echo "✗ Notarization failed. Apple's response:"
            cat /tmp/notary.log
            echo ""
            SUBMIT_ID="$(grep -oE 'id: [a-f0-9-]+' /tmp/notary.log | head -1 | awk '{print $2}')"
            if [ -n "$SUBMIT_ID" ]; then
                echo "  Detailed logs:"
                echo "    xcrun notarytool log $SUBMIT_ID --keychain-profile $NOTARY_PROFILE"
            fi
        fi
    else
        echo ""
        echo "! Notary credentials not stored in keychain — skipping notarization."
        echo "  Run this once to set them up:"
        echo "    xcrun notarytool store-credentials $NOTARY_PROFILE \\"
        echo "        --apple-id YOUR_APPLE_ID \\"
        echo "        --team-id ${TEAM_ID:-YOUR_TEAM_ID} \\"
        echo "        --password YOUR_APP_SPECIFIC_PASSWORD"
    fi

    # 6. Replace the on-disk dist/ copy with the signed (and possibly
    #    stapled) bundle from the disk image. ditto preserves the
    #    embedded signature byte-for-byte.
    echo ""
    echo "→ Copying signed bundle back to dist/..."
    rm -rf "$APP_PATH"
    ditto "$DMG_APP" "$APP_PATH"

    # 7. Detach and remove the disk image.
    hdiutil detach "$DMG_MNT" -quiet 2>/dev/null || true
    rm -f "$DMG"
else
    echo ""
    echo "! No 'Developer ID Application' certificate found in keychain."
    echo "  The app will still build, but recipients will need the workaround installer."
    echo "  To fix: open Xcode → Settings → Accounts → select your Apple ID →"
    echo "  Manage Certificates → click '+' → 'Developer ID Application'."
fi

# ── 5. Compile one-click installer ───────────────────────────────────────────
# Only needed when the app ISN'T notarized. A notarized app opens normally.
if [ "$NOTARIZED" = "true" ]; then
    echo ""
    echo "→ Skipping installer (app is notarized — opens normally on any Mac)."
    INSTALLER_APP=""
else

echo ""
echo "→ Compiling one-click installer..."

INSTALLER_SCRIPT="$(mktemp /tmp/installer_XXXXXX.applescript)"
cat > "$INSTALLER_SCRIPT" << 'APPLESCRIPT'
on run
    -- Find OptionsDashboard.app sitting next to this installer
    set myDir to do shell script "dirname " & quoted form of POSIX path of (path to me)
    set appSrc  to myDir & "/OptionsDashboard.app"
    set appDest to "/Applications/OptionsDashboard.app"

    -- Verify the app is there
    try
        do shell script "test -d " & quoted form of appSrc
    on error
        display dialog "Could not find OptionsDashboard.app." & return & return & ¬
            "Make sure this installer and OptionsDashboard.app are in the same folder." ¬
            buttons {"OK"} default button "OK" with icon stop
        return
    end try

    -- Welcome prompt
    set ans to button returned of (display dialog ¬
        "This will install Options Dashboard on your Mac and open it." & return & return & ¬
        "Click Install to continue." ¬
        buttons {"Cancel", "Install"} default button "Install" with icon 1)
    if ans is "Cancel" then return

    -- Strip quarantine so macOS lets the app run (no password needed)
    try
        do shell script "xattr -dr com.apple.quarantine " & quoted form of appSrc
    end try

    -- Copy to /Applications (ask for password only if needed)
    try
        do shell script "cp -Rf " & quoted form of appSrc & " " & quoted form of appDest
    on error
        try
            do shell script "cp -Rf " & quoted form of appSrc & " " & ¬
                quoted form of appDest with administrator privileges
        on error errMsg
            display dialog "Installation failed." & return & errMsg ¬
                buttons {"OK"} default button "OK" with icon stop
            return
        end try
    end try

    -- Launch
    do shell script "open " & quoted form of appDest

    display dialog "Options Dashboard is installed and open!" & return & return & ¬
        "You can move this installer and the zip to the Trash." ¬
        buttons {"Done"} default button "Done" with icon 1
end run
APPLESCRIPT

INSTALLER_APP="dist/Install Options Dashboard.app"
rm -rf "$INSTALLER_APP"
osacompile -o "$INSTALLER_APP" "$INSTALLER_SCRIPT"
rm -f "$INSTALLER_SCRIPT"
echo "✓ Installer compiled"

fi   # end !NOTARIZED branch

# ── 6. Create distributable zip ───────────────────────────────────────────────
echo ""
echo "→ Creating zip for distribution..."
rm -f "$ZIP_PATH"

if [ "$NOTARIZED" = "true" ]; then
    # Notarized — clean zip with just the .app. Recipients double-click the
    # zip, drag the .app to Applications, and it opens normally with no
    # right-click trick or installer step.
    ditto -c -k --sequesterRsrc --keepParent "$APP_PATH" "$ZIP_PATH"
else
    # Not notarized — include the workaround installer that strips the
    # quarantine flag at install time. Both apps go at the root of the zip.
    STAGING="$(mktemp -d)"
    cp -R "$APP_PATH"      "$STAGING/OptionsDashboard.app"
    cp -R "$INSTALLER_APP" "$STAGING/Install Options Dashboard.app"
    ditto -c -k --sequesterRsrc "$STAGING" "$ZIP_PATH"
    rm -rf "$STAGING"
fi

ZIP_SIZE="$(du -sh "$ZIP_PATH" | cut -f1)"
echo "✓ Zip created: $ZIP_PATH  ($ZIP_SIZE)"

echo ""
echo "═══════════════════════════════════════════════"
echo "  ✓  Done!  v$VERSION"
echo ""
echo "  File to share:"
echo "    $(pwd)/$ZIP_PATH  ($ZIP_SIZE)"
echo ""
echo "  Permanent download link:"
echo "    https://github.com/amit1208levy/optionDashboard/releases/latest/download/OptionsDashboard.zip"
echo ""

if [ "$NOTARIZED" = "true" ]; then
    echo "  ✨ Notarized by Apple — opens cleanly on any Mac."
    echo ""
    echo "  Recipient instructions:"
    echo "    1. Click the link → zip downloads"
    echo "    2. Double-click OptionsDashboard.zip to unzip it"
    echo "    3. Drag OptionsDashboard.app into Applications"
    echo "    4. Double-click — it just opens. No warnings, no right-click trick."
else
    echo "  Recipient instructions (one-time setup):"
    echo "    1. Click the link → zip downloads"
    echo "    2. Double-click OptionsDashboard.zip to unzip it"
    echo "    3. Double-click 'Install Options Dashboard'"
    echo "       macOS asks once: right-click → Open → Open"
    echo "    4. Click Install — app installs itself and opens. Done!"
fi
echo "═══════════════════════════════════════════════"

# ── 7. Publish to GitHub Releases (enables in-app auto-update) ───────────────
echo ""
echo "→ Publishing v$VERSION to GitHub Releases..."

if command -v gh &>/dev/null; then
    # Check if this release tag already exists
    if gh release view "v$VERSION" &>/dev/null 2>&1; then
        echo ""
        echo "! Release v$VERSION already exists on GitHub."
        echo "  If you want to replace it, run:"
        echo "     gh release delete v$VERSION --yes"
        echo "  Then re-run ./build.sh"
        echo ""
    else
        gh release create "v$VERSION" \
            "$ZIP_PATH" \
            --title "Options Dashboard v$VERSION" \
            --generate-notes \
            --repo amit1208levy/optionDashboard
        echo ""
        echo "✓ Release v$VERSION published to GitHub."
        echo "  Users running v<$VERSION will be notified on their next launch"
        echo "  and can install the update with one click."
    fi
else
    echo ""
    echo "! GitHub CLI (gh) not found — skipping automatic GitHub release."
    echo "  The zip was built successfully but users won't see an update prompt"
    echo "  until it's published."
    echo ""
    echo "  Option A — install gh and re-run (recommended):"
    echo "     brew install gh"
    echo "     gh auth login"
    echo "     ./build.sh"
    echo ""
    echo "  Option B — publish this build manually now:"
    echo "     gh release create \"v$VERSION\" \\"
    echo "         \"$(pwd)/$ZIP_PATH\" \\"
    echo "         --title \"Options Dashboard v$VERSION\" \\"
    echo "         --generate-notes \\"
    echo "         --repo amit1208levy/optionDashboard"
    echo ""
    echo "  Option C — via GitHub web UI:"
    echo "     https://github.com/amit1208levy/optionDashboard/releases/new"
    echo "     Tag:   v$VERSION"
    echo "     Asset: $(pwd)/$ZIP_PATH"
fi
