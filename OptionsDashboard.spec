# -*- mode: python ; coding: utf-8 -*-
import os, subprocess

# Stamp the current git SHA into the bundle so runtime update-checks can
# know which commit this .app was built from.
_sha_file = os.path.join(os.path.abspath(os.path.dirname(SPEC)), "_build_sha.txt")
try:
    sha = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], text=True
    ).strip()
    with open(_sha_file, "w") as _f:
        _f.write(sha)
except Exception:
    sha = "unknown"


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('_build_sha.txt', '.')],
    hiddenimports=[
        'websockets',
        'ib_insync',
        'ib_insync.ib',
        'ib_insync.contract',
        'ib_insync.ticker',
        'ib_insync.objects',
        'ib_insync.util',
        'eventkit',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='OptionsDashboard',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='OptionsDashboard',
)
app = BUNDLE(
    coll,
    name='OptionsDashboard.app',
    icon=None,
    bundle_identifier='com.amitlevy.optionsdashboard',
)
