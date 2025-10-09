# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for MatchBox
Builds standalone executables for Windows, macOS, and Linux
"""
import os
import platform
import urllib.request
import zipfile
import tarfile
import shutil
from pathlib import Path
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import collect_data_files
from setuptools_scm import get_version, _cli

_cli.main(["--force-write-version-files"])

datas, binaries, hiddenimports = collect_all('MatchBox')
datas += [('./us.brainstormz.MatchBox.png', '.'),
         ('./us.brainstormz.MatchBox.Devel.png', '.')]
datas += collect_data_files('sv_ttk')

# Download and bundle ffmpeg binaries
def download_ffmpeg():
    """Download ffmpeg binaries for the current platform"""
    system = platform.system()
    output_dir = Path('build') / 'ffmpeg-binaries' / system.lower()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Check if already downloaded
    ffmpeg_name = 'ffmpeg.exe' if system == 'Windows' else 'ffmpeg'
    if (output_dir / ffmpeg_name).exists():
        print(f"[OK] ffmpeg binaries already present for {system}")
        return output_dir

    print(f"Downloading ffmpeg binaries for {system}...")

    if system == 'Windows':
        url = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'
        archive = output_dir / 'ffmpeg.zip'
        print(f"  Fetching {url}")
        urllib.request.urlretrieve(url, archive)

        with zipfile.ZipFile(archive, 'r') as zip_ref:
            zip_ref.extractall(output_dir / 'temp')

        # Find and copy binaries
        for root, dirs, files in os.walk(output_dir / 'temp'):
            for file in ['ffmpeg.exe', 'ffprobe.exe']:
                if file in files:
                    shutil.copy2(Path(root) / file, output_dir / file)

        shutil.rmtree(output_dir / 'temp')
        os.remove(archive)

    elif system == 'Darwin':
        # macOS: Use evermeet.cx (official macOS builds)
        ffmpeg_url = 'https://evermeet.cx/ffmpeg/getrelease/zip'
        ffmpeg_zip = output_dir / 'ffmpeg.zip'
        print(f"  Fetching {ffmpeg_url}")
        urllib.request.urlretrieve(ffmpeg_url, ffmpeg_zip)

        with zipfile.ZipFile(ffmpeg_zip, 'r') as zip_ref:
            zip_ref.extractall(output_dir)
        os.remove(ffmpeg_zip)

        ffprobe_url = 'https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip'
        ffprobe_zip = output_dir / 'ffprobe.zip'
        print(f"  Fetching {ffprobe_url}")
        urllib.request.urlretrieve(ffprobe_url, ffprobe_zip)

        with zipfile.ZipFile(ffprobe_zip, 'r') as zip_ref:
            zip_ref.extractall(output_dir)
        os.remove(ffprobe_zip)

        # Make executable
        os.chmod(output_dir / 'ffmpeg', 0o755)
        os.chmod(output_dir / 'ffprobe', 0o755)

    else:  # Linux
        url = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz'
        archive = output_dir / 'ffmpeg.tar.xz'
        print(f"  Fetching {url}")
        urllib.request.urlretrieve(url, archive)

        with tarfile.open(archive, 'r:xz') as tar_ref:
            tar_ref.extractall(output_dir / 'temp')

        # Find and copy binaries
        for root, dirs, files in os.walk(output_dir / 'temp'):
            for file in ['ffmpeg', 'ffprobe']:
                if file in files:
                    shutil.copy2(Path(root) / file, output_dir / file)
                    os.chmod(output_dir / file, 0o755)

        shutil.rmtree(output_dir / 'temp')
        os.remove(archive)

    print(f"[OK] Downloaded ffmpeg binaries to {output_dir}")
    return output_dir

# Download ffmpeg and add to binaries
ffmpeg_dir = download_ffmpeg()
system = platform.system()

if system == 'Windows':
    binaries.append((str(ffmpeg_dir / 'ffmpeg.exe'), '.'))
    binaries.append((str(ffmpeg_dir / 'ffprobe.exe'), '.'))
elif system == 'Darwin':
    binaries.append((str(ffmpeg_dir / 'ffmpeg'), '.'))
    binaries.append((str(ffmpeg_dir / 'ffprobe'), '.'))
else:  # Linux
    binaries.append((str(ffmpeg_dir / 'ffmpeg'), '.'))
    binaries.append((str(ffmpeg_dir / 'ffprobe'), '.'))

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

# Splash screen is not supported on macOS
splash_args = []
if system != 'Darwin':
    splash = Splash(  # pyright: ignore[reportUndefinedVariable]
        'us.brainstormz.MatchBox.Splash.png',
        binaries=a.binaries,
        datas=a.datas,
        text_pos=None,
        text_size=12,
        minify_script=True,
    )
    splash_args = [splash, splash.binaries]

exe = EXE(  # pyright: ignore[reportUndefinedVariable]
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    *splash_args,
    [],
    name='MatchBox',
    icon='us.brainstormz.MatchBox.ico',
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
    icon='us.brainstormz.MatchBox.icns',
    bundle_identifier=None,
    version=get_version(),
    info_plist={
        'NSPrincipalClass': 'NSApplication',
        'NSAppleScriptEnabled': False,
        'CFBundleIdentifier': 'us.brainstormz.MatchBox',
        'CFBundleDisplayName': 'MatchBox™ for FIRST® Tech Challenge',
        'NSHumanReadableCopyright': 'Copyright © the MatchBox™ development team and contributors.\nThis software is released under the MIT license.',
    },
)
