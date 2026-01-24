# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for MatchBox
Builds standalone executables for Windows, macOS, and Linux
"""
import os
import sys
import platform
import urllib.request
import zipfile
import tarfile
import shutil
from pathlib import Path
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules
from setuptools_scm import get_version, _cli

_cli.main(["--force-write-version-files"])

datas, binaries, hiddenimports = collect_all('MatchBox')
datas += [('./us.brainstormz.MatchBox.png', '.'),
         ('./us.brainstormz.MatchBox.Devel.png', '.')]
datas += collect_data_files('sv_ttk')
hiddenimports += collect_submodules('zeroconf') # https://github.com/pyinstaller/pyinstaller-hooks-contrib/issues/840

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


def download_rsync():
    """Download rsync binaries for the current platform"""
    system = platform.system()
    output_dir = Path('build') / 'rsync-binaries' / system.lower()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Check if already downloaded
    rsync_name = 'rsync.exe' if system == 'Windows' else 'rsync'
    if (output_dir / rsync_name).exists():
        print(f"[OK] rsync binaries already present for {system}")
        return output_dir

    print(f"Downloading rsync binaries for {system}...")

    if system == 'Windows':
        # Use rsync from MSYS2 - download required packages
        msys2_base = 'https://repo.msys2.org/msys/x86_64/'

        # Core packages needed for rsync (versions may need updating periodically)
        packages = [
            'rsync-3.3.0-1-x86_64.pkg.tar.zst',
            'msys2-runtime-3.5.4-2-x86_64.pkg.tar.zst',
            'libzstd-1.5.6-1-x86_64.pkg.tar.zst',
            'libxxhash-0.8.2-1-x86_64.pkg.tar.zst',
            'liblz4-1.10.0-1-x86_64.pkg.tar.zst',
            'libiconv-1.17-1-x86_64.pkg.tar.zst',
            'libintl-0.22.4-1-x86_64.pkg.tar.zst',
            'libopenssl-3.4.0-1-x86_64.pkg.tar.zst',
        ]

        temp_dir = output_dir / 'temp'
        temp_dir.mkdir(exist_ok=True)

        # zstandard is in requirements.txt for .tar.zst decompression
        import zstandard

        for pkg in packages:
            url = msys2_base + pkg
            archive = temp_dir / pkg
            print(f"  Fetching {pkg}")
            try:
                urllib.request.urlretrieve(url, archive)

                # Decompress .tar.zst using streaming decompression
                print(f"  Fetched {pkg}, decompressing")
                with open(archive, 'rb') as compressed:
                    dctx = zstandard.ZstdDecompressor()
                    with dctx.stream_reader(compressed) as reader:
                        with tarfile.open(fileobj=reader, mode='r|') as tar:
                            tar.extractall(temp_dir / 'extracted')

                os.remove(archive)
            except Exception as e:
                print(f"  Warning: Could not fetch {pkg}: {e}")

        # Copy binaries to output
        extracted = temp_dir / 'extracted' / 'usr' / 'bin'
        if extracted.exists():
            for f in extracted.iterdir():
                if f.suffix in ['.exe', '.dll'] or f.name.startswith('msys-'):
                    shutil.copy2(f, output_dir / f.name)
                    print(f"  Copied {f.name}")

        if (temp_dir / 'extracted').exists():
            shutil.rmtree(temp_dir / 'extracted')
        if temp_dir.exists():
            shutil.rmtree(temp_dir)

    elif system == 'Darwin':
        # macOS: rsync is pre-installed, but we can bundle a newer version
        # Use Homebrew bottle or static build
        # For now, we'll use the system rsync by not bundling (it's always available on macOS)
        # If needed, could use: brew fetch rsync and extract the bottle
        print("  macOS: Using system rsync (pre-installed on all macOS versions)")
        # Create a shell script wrapper that uses system rsync
        wrapper = output_dir / 'rsync'
        wrapper.write_text('#!/bin/sh\nexec /usr/bin/rsync "$@"\n')
        os.chmod(wrapper, 0o755)

    else:  # Linux
        # Linux: rsync is typically available, but we can bundle a static build
        # Use static rsync from official sources or build
        # For now, create wrapper to use system rsync
        print("  Linux: Using system rsync (available on virtually all Linux systems)")
        # Create a shell script wrapper that uses system rsync
        wrapper = output_dir / 'rsync'
        wrapper.write_text('#!/bin/sh\nexec /usr/bin/rsync "$@"\n')
        os.chmod(wrapper, 0o755)

    print(f"[OK] rsync binaries ready at {output_dir}")
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

# Download rsync and add to binaries
rsync_dir = download_rsync()

if system == 'Windows':
    # Windows needs rsync.exe and Cygwin DLLs
    for file in os.listdir(rsync_dir):
        if file.endswith('.exe') or file.endswith('.dll'):
            binaries.append((str(rsync_dir / file), '.'))
else:  # macOS and Linux use wrapper scripts
    binaries.append((str(rsync_dir / 'rsync'), '.'))

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

if system != 'Darwin':
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
else: # Use onedir on macOS, since we are creating a .app bundle anyways, and onefile is not necessary and causes much slower startup times
    exe = EXE(  # pyright: ignore[reportUndefinedVariable]
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='MatchBox',
        icon='us.brainstormz.MatchBox.ico',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        runtime_tmpdir=None,
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
        a.zipfiles,
        a.datas,
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
    system == 'Darwin' and coll or exe,
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
