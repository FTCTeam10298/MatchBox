# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MatchBox is a Python desktop application that automates OBS scene switching and match video clipping for FIRST Tech Challenge (FTC) robotics events. It connects to the FTC scoring system via WebSocket, controls OBS via WebSocket, and serves clipped match videos via HTTP with mDNS discovery.

## Commands

**Install dependencies:**
```bash
pip install -r requirements.txt
```

**Run the GUI application:**
```bash
python matchbox.py
```

**Run in CLI mode:**
```bash
python matchbox-cli.py --ftc-host <host> --event-code <code>
```

**Build standalone executables:**
```bash
python build.py
```

Note: There is no test suite.

**Type checking:**
```bash
basedpyright matchbox.py matchbox-cli.py matchbox-sync.py build.py web_api/ local_video_processor.py
```

The project uses **basedpyright** in strict mode. When writing or modifying code:
- Avoid `Any` types — use `object`, `dict[str, object]`, or `cast()` instead
- Annotate class attributes, function parameters, and return types
- Use `_ =` for intentionally unused call results
- Prefix unused variables with `_` (e.g., `_dirs`)
- Use `from __future__ import annotations` with `TYPE_CHECKING` imports to avoid circular imports at runtime

## Architecture

### Core Components

**matchbox.py** - Main application containing:
- `MatchBoxConfig` - Configuration dataclass for all settings
- `MatchBoxCore` - Application controller handling WebSocket connections, OBS control, and video processing orchestration
- `MatchBoxGUI` - Tkinter GUI with sv-ttk theming

**matchbox-cli.py** - Command-line interface alternative to the GUI

**local_video_processor.py** - Video clipping engine that uses ffmpeg subprocess calls to extract match segments from OBS recordings

### Data Flow

```
FTC Scoring WebSocket → MatchBoxCore → OBS Scene Switch
                              ↓
                     Match Start Event
                              ↓
                  LocalVideoProcessor (delayed)
                              ↓
                     ffmpeg Clip Extraction
                              ↓
                   match_clips/<event_code>/
                              ↓
                   HTTP Server (mDNS: ftcvideo.local)
```

### Threading Model

- Main thread: Tkinter GUI event loop
- Background thread: asyncio event loop for WebSocket monitoring
- Web server: ThreadingHTTPServer with multi-threaded request handling
- mDNS: Separate daemon thread for Zeroconf registration

### Key Technical Details

- Uses `obs-websocket-py` for OBS control with fallback methods for older API versions
- Custom HTTP handler implements Range requests for progressive video playback
- Configuration stored as JSON (`matchbox_config.json`) with CLI args taking priority
- Version derived from git tags via setuptools-scm

### Build System

PyInstaller builds configured in `matchbox.spec`:
- Downloads platform-specific ffmpeg binaries automatically
- Produces standalone executables for Windows, macOS, and Linux
- CI/CD via GitHub Actions (`.github/workflows/pyinstaller.yml`)
