# FIRST® MatchBox™

MatchBox is a standalone application that integrates with OBS and the FTC scoring system to provide:
- Automatic scene switching based on field events
- Match video autosplitting and clipping
- Local web interface for easy access to match clips
- Integration with clipfarm for video upload

_Disclaimer: MatchBox and Clipfarm are (currently) unofficial, community-ran projects.  While we hope to someday have it become an official part of FTC, similar to FRC's Webcast Unit, currently MatchBox and Clipfarm are entirely community-supported._

## Features

✅ **Automatic Scene Switching**
- Connects to FTC scoring system WebSocket
- Automatically switches OBS scenes based on active field
- Configurable field-to-scene mapping

✅ **OBS Integration**
- Auto-configures OBS scenes with scoring system overlays
- WebSocket connection to OBS (localhost:4455 by default)
- Template setup for consistent streaming

✅ **Local Web Interface**
- Serves match clips via HTTP
- Accessible at http://localhost:8080 (configurable)
- mDNS support for easy access (http://ftcvideo.local)
- Perfect for refs to review matches without internet

✅ **Match Video Processing**
- Automatic match detection and splitting
- Clips saved locally for immediate access
- Support for multiple video formats

## Requirements

### Software Dependencies
- Python 3.8+
- OBS Studio with WebSocket plugin enabled
- FTC Scoring System

### Python Packages
Install required packages:
```bash
pip install -r requirements.txt
```

### Required System Tools
- ffmpeg (with ffprobe)
- yt-dlp (for video downloading)

## Quick Start

### GUI Mode (Recommended)
```bash
python matchbox.py
```

### CLI Mode
```bash
python matchbox-cli.py --event-code YOUR_EVENT_CODE --obs-password YOUR_OBS_PASSWORD
```

## Configuration

### GUI Configuration
1. Launch the GUI: `python matchbox.py`
2. Fill in the Connection Settings tab:
   - **Event Code**: Your FTC event code
   - **Scoring System Host**: Usually `localhost` or IP of scoring computer
   - **OBS WebSocket Password**: Set in OBS Tools > WebSocket Server Settings
3. Configure Scene Mapping tab to match your OBS scene names
4. Set output directory in Video & Web Settings
5. Click "Configure OBS Scenes" to auto-setup OBS
6. Click "Start MatchBox" to begin operation

### CLI Configuration
Create a configuration file:
```bash
python matchbox-cli.py --event-code MYEVENT --obs-password mypass --save-config config.json
```

Use the configuration file:
```bash
python matchbox-cli.py --config config.json
```

### Configuration File Format
```json
{
  "event_code": "YOUR_EVENT_CODE",
  "scoring_host": "localhost",
  "scoring_port": 80,
  "obs_host": "localhost",
  "obs_port": 4455,
  "obs_password": "YOUR_OBS_PASSWORD",
  "num_fields": 2,
  "output_dir": "./match_clips",
  "web_port": 8080,
  "field_scene_mapping": {
    "1": "Field 1",
    "2": "Field 2"
  }
}
```

## Usage

### Setting up OBS
1. Enable WebSocket server in OBS:
   - Tools > WebSocket Server Settings
   - Enable WebSocket server
   - Set a password
   - Note the port (usually 4455)

2. Configure MatchBox with your OBS settings

3. Click "Configure OBS Scenes" to auto-create:
   - Field scenes (Field 1, Field 2, etc.)
   - Browser sources with scoring system overlays

### Operation
1. Start your FTC scoring system
2. Start OBS and begin streaming/recording
3. Launch MatchBox (GUI or CLI)
4. MatchBox will:
   - Connect to scoring system WebSocket
   - Switch OBS scenes based on active field
   - Start local web server for match clips
   - Process and split match videos

### Accessing Match Clips
- **Locally**: http://localhost:8080 (or your configured port)
- **Network**: http://[computer-ip]:8080
- **mDNS**: http://ftcvideo.local (if supported)

Perfect for referees and field staff to review matches instantly!

## CLI Reference

### Basic Usage
```bash
# Start with minimal configuration
python matchbox-cli.py --event-code MYEVENT123 --obs-password mypass

# Use configuration file
python matchbox-cli.py --config config.json

# Test connections without starting
python matchbox-cli.py --event-code MYEVENT123 --obs-password mypass --test-connection

# Configure OBS scenes only
python matchbox-cli.py --event-code MYEVENT123 --obs-password mypass --configure-obs-only
```

### Advanced Options
```bash
# Custom scoring system
python matchbox-cli.py --event-code MYEVENT --obs-password mypass \
  --scoring-host 10.0.0.100 --scoring-port 8080

# Custom scene names
python matchbox-cli.py --event-code MYEVENT --obs-password mypass \
  --field1-scene "Red Alliance" --field2-scene "Blue Alliance"

# Custom output and web settings
python matchbox-cli.py --event-code MYEVENT --obs-password mypass \
  --output-dir /path/to/clips --web-port 9000
```

## Troubleshooting

### OBS Connection Issues
- Check OBS WebSocket server is enabled (Tools > WebSocket Server Settings)
- Verify password matches
- Ensure OBS is running before starting MatchBox
- Check Windows firewall isn't blocking connections

### FTC Scoring System Issues
- Verify event code is correct
- Check scoring system is running and accessible
- Test WebSocket URL in browser: `ws://localhost/stream/display/command/?code=YOURCODE`

### Web Interface Issues
- Check if port is already in use
- Try different port number
- Verify output directory permissions
- Check firewall settings for HTTP access

### Scene Switching Not Working
- Verify field-to-scene mapping matches OBS scene names
- Check OBS scenes exist
- Monitor log for WebSocket messages
- Test manual scene switching in OBS

## Architecture

MatchBox combines functionality from:
- **ftc-obs-autoswitcher**: OBS WebSocket integration and scene switching
- **match-video-autosplitter**: Video processing and match detection

### Core Components
- `MatchBoxCore`: Main application logic
- `MatchBoxGUI`: Tkinter-based graphical interface
- `matchbox-cli.py`: Command-line interface
- Web server: Simple HTTP server for match clips

### Network Connections
- **FTC Scoring System**: WebSocket connection for match events
- **OBS Studio**: WebSocket connection for scene control
- **Local Network**: HTTP server for match clip access
