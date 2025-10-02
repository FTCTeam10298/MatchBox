# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for FIRST® MatchBox™
Builds standalone executables for Windows, macOS, and Linux
"""
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import collect_data_files
from setuptools_scm import get_version, _cli
_cli.main(["--force-write-version-files"])

datas, binaries, hiddenimports = collect_all('MatchBox')
#datas += [('./us.brainstormz.MatchBox.png', '.'),
#         ('./us.brainstormz.MatchBox.Devel.png', '.')]
datas += collect_data_files('sv_ttk')

print("datas", datas)
print("binaries", binaries)
print("hiddenimports", hiddenimports)

block_cipher = None


a = Analysis(  # pyright: ignore[reportUndefinedVariable]
    ['matchbox.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # pyright: ignore[reportUndefinedVariable]

exe = EXE(  # pyright: ignore[reportUndefinedVariable]
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='MatchBox',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

app = BUNDLE(  # pyright: ignore[reportUndefinedVariable]
    exe,
    name='MatchBox.app',
    icon=None, # 'us.brainstormz.MatchBox.icns',
    bundle_identifier=None,
    version=get_version(),
    info_plist={
        'NSPrincipalClass': 'NSApplication',
        'NSAppleScriptEnabled': False,
        'CFBundleIdentifier': 'us.brainstormz.MatchBox',
        'CFBundleDisplayName': 'FIRST® MatchBox™',
        'NSHumanReadableCopyright': 'Copyright © the MatchBox™ development team and contributors.\nThis software is released under the MIT license.',
    },
)
