#!/usr/bin/env python3
"""
Build script for MatchBox
Creates standalone executables for Windows, macOS, and Linux using PyInstaller
"""

import sys
import os
import platform
import shutil
import subprocess
from pathlib import Path


def clean_build():
    """Remove previous build artifacts"""
    dirs_to_remove = ['build', 'dist', '__pycache__']
    for dir_name in dirs_to_remove:
        if os.path.exists(dir_name):
            print(f"Removing {dir_name}/")
            shutil.rmtree(dir_name)

    # Remove .pyc files
    for root, _dirs, files in os.walk('.'):
        for file in files:
            if file.endswith('.pyc'):
                os.remove(os.path.join(root, file))


def build_executable():
    """Build the executable using PyInstaller"""
    print(f"Building MatchBox for {platform.system()}...")

    # Run PyInstaller
    cmd = ['pyinstaller', '--clean', 'matchbox.spec']

    result = subprocess.run(cmd, check=False)

    if result.returncode != 0:
        print("Build failed!")
        sys.exit(1)

    print("\nBuild completed successfully!")
    print(f"Executable location: dist/")

    # Show what was created
    if os.path.exists('dist'):
        print("\nCreated files:")
        for item in os.listdir('dist'):
            item_path = os.path.join('dist', item)
            if os.path.isfile(item_path):
                size = os.path.getsize(item_path)
                print(f"  {item} ({size / (1024*1024):.2f} MB)")
            else:
                print(f"  {item}/ (directory)")


def create_dist_package():
    """Create a distributable package with the executable and necessary files"""
    system = platform.system()
    dist_name = f"MatchBox-{system}-{platform.machine()}"
    dist_path = Path('dist') / dist_name

    if dist_path.exists():
        shutil.rmtree(dist_path)

    dist_path.mkdir(parents=True, exist_ok=True)

    # Copy executable
    if system == 'Darwin':
        # macOS app bundle
        app_path = Path('dist') / 'MatchBox.app'
        if app_path.exists():
            _ = shutil.copytree(app_path, dist_path / 'MatchBox.app')
    elif system == 'Windows':
        exe_path = Path('dist') / 'MatchBox.exe'
        if exe_path.exists():
            _ = shutil.copy(exe_path, dist_path)
    else:
        # Linux
        exe_path = Path('dist') / 'MatchBox'
        if exe_path.exists():
            _ = shutil.copy(exe_path, dist_path)
            # Make executable
            os.chmod(dist_path / 'MatchBox', 0o755)

    # Copy README and other documentation
    if os.path.exists('README.md'):
        _ = shutil.copy('README.md', dist_path)

    # Copy example config if it exists
    if os.path.exists('matchbox_config.json'):
        _ = shutil.copy('matchbox_config.json', dist_path / 'matchbox_config.example.json')

    print(f"\nDistribution package created: {dist_path}")

    # Create archive
    archive_name = f"{dist_name}"
    if system == 'Windows':
        archive_format = 'zip'
    else:
        archive_format = 'gztar'

    print(f"Creating archive: {archive_name}.{archive_format.replace('tar', 'tar.gz')}")
    _ = shutil.make_archive(
        str(Path('dist') / archive_name),
        archive_format,
        'dist',
        dist_name
    )


def main():
    """Main build process"""
    print("=" * 60)
    print("MatchBox™ for FIRST® Tech Challenge - Build Script")
    print("=" * 60)
    print(f"Platform: {platform.system()} {platform.machine()}")
    print(f"Python: {sys.version}")
    print("=" * 60)

    # Check if PyInstaller is installed
    try:
        import PyInstaller
        print(f"PyInstaller version: {PyInstaller.__version__}")
    except ImportError:
        print("ERROR: PyInstaller not found!")
        print("Install it with: pip install -r requirements.txt")
        sys.exit(1)

    # Clean previous builds
    print("\nCleaning previous builds...")
    clean_build()

    # Build executable
    print("\nBuilding executable...")
    build_executable()

    # Create distribution package
    print("\nCreating distribution package...")
    create_dist_package()

    print("\n" + "=" * 60)
    print("Build process complete!")
    print("=" * 60)


if __name__ == '__main__':
    main()
