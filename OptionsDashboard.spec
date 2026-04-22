# -*- mode: python ; coding: utf-8 -*-


from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# tastytrade SDK + its market-calendars dependency need explicit collection
# because they use lazy imports / data files PyInstaller doesn't auto-detect.
_hidden = (
    collect_submodules('tastytrade')
    + collect_submodules('pandas_market_calendars')
    + ['websockets', 'httpx', 'pydantic']
)
_datas = (
    collect_data_files('tastytrade')
    + collect_data_files('pandas_market_calendars')
)

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=_datas,
    hiddenimports=_hidden,
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
