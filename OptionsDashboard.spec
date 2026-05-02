# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for OptionsDashboard.
Produces a self-contained OptionsDashboard.app for macOS.
"""

import sys
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Pull the version from source so info_plist stays in sync automatically.
sys.path.insert(0, SPECPATH)
from version import VERSION as _APP_VERSION

# ── Data files to bundle ──────────────────────────────────────────────────────
datas = [
    ("templates", "templates"),          # HTML templates (if used)
    ("tickers.json", "."),               # Watchlist ticker list
]
# App icon — only included if it's been generated (build_icon.py)
if os.path.exists("AppIcon.png"):
    datas.append(("AppIcon.png", "."))
# Add static/ only if it exists
if os.path.isdir("static"):
    datas.append(("static", "static"))

# Matplotlib needs its mpl-data directory (fonts, style sheets, etc.)
datas += collect_data_files("matplotlib")

# ── Hidden imports ────────────────────────────────────────────────────────────
# PyInstaller misses these because they're imported dynamically or via strings.
hiddenimports = [
    # Qt / PyQt6 internals
    "PyQt6.sip",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "PyQt6.QtNetwork",
    # Matplotlib Qt backend (matplotlib.use("QtAgg") picks this at runtime)
    "matplotlib.backends.backend_qtagg",
    "matplotlib.backends.backend_qt",
    "matplotlib.backends._backend_tk",   # fallback, avoids import warnings
    "matplotlib.figure",
    "matplotlib.ticker",
    "matplotlib.dates",
    # Networking
    "requests",
    "requests.adapters",
    "requests.auth",
    "requests.packages",
    "urllib3",
    "urllib3.util",
    "certifi",
    "charset_normalizer",
    "idna",
    # WebSockets (async streamer)
    "websockets",
    "websockets.legacy",
    "websockets.legacy.client",
    "websockets.legacy.server",
    # IBKR (optional — ib_insync may not be installed; PyInstaller won't error
    # because we gate these imports with try/except at runtime)
    "ib_insync",
    # pkg_resources used internally by several libraries
    "pkg_resources",
    "pkg_resources.extern",
]

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Exclude heavy packages that are definitely not needed
    excludes=["tkinter", "scipy", "pandas", "numpy.distutils",
              "IPython", "jupyter", "PIL"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OptionsDashboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX can corrupt Qt dylibs — leave off
    console=False,       # no Terminal window
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
    upx=False,
    upx_exclude=[],
    name="OptionsDashboard",
)

app = BUNDLE(
    coll,
    name="OptionsDashboard.app",
    icon="AppIcon.icns" if os.path.exists("AppIcon.icns") else None,
    bundle_identifier="com.amitlevy.optionsdashboard",
    info_plist={
        "CFBundleName":             "Options Dashboard",
        "CFBundleDisplayName":      "Options Dashboard",
        "CFBundleVersion":          _APP_VERSION,
        "CFBundleShortVersionString": _APP_VERSION,
        "NSHighResolutionCapable":  True,
        "LSMinimumSystemVersion":   "10.15",
        "NSRequiresAquaSystemAppearance": False,  # supports dark mode
    },
)
