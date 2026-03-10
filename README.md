# MatchBox™ for *FIRST®* Tech Challenge

MatchBox is a standalone application that integrates with OBS and the *FIRST®* Tech Challenge scoring system to provide:
- Automatic setup of scenes in OBS, including the *FIRST®* Tech Challenge scoring overlay
- Automatic real-time scene switching based on field events
- Fully automatic match video clipping
- Local web interface for easy access to match clips
- Automatic video upload
  - Coming soon: Full integration with https://www.clipfarm.watch/

<img width="941" height="981" alt="image" src="https://github.com/user-attachments/assets/3fbc17b2-c916-47e9-88b6-03cd474c3b4e" />

> [!IMPORTANT]
> **MatchBox is (currently) an unofficial, community-ran project.  While we hope to someday have it become an official part of FTC, similar to FRC's Webcast Unit, currently MatchBox is entirely community-supported, so please do not bother *FIRST®* with any issues you may encounter.**
> 
> *FIRST®, FIRST® Robotics Competition, and FIRST® Tech Challenge, are registered trademarks of FIRST® (www.firstinspires.org) which is not overseeing, involved with, or responsible for this activity, product, or service.*

## Features

- **OBS Integration**: Auto-configures OBS scenes with the FTC scoring system overlays
  - No more messing around with broken chroma key setups, awkward scoring overlay configuration, etc.
- **Automatic Scene Switching**: Connects to FTC scoring system, and automatically switches OBS scenes based on active field
- **Match Video Processing**: Automatic match detection and splitting
  - Clips saved locally for immediate access
- **Local Web Interface**: Locally serves match clips for easy access by both teams and event staff, even when the events's external Wi-Fi may be slow, unreliable, or even non-existant
  - mDNS support for easy access (http://ftcvideo.local)
  - Perfect for anyone on the event network to review matches, even without a full internet connection
- **Full remote administration**: Set an instance password, and you can have your team connect to MatchBox from anywhere, whether across the room or across the whole world, as long as you have an internet connection
  - Optionally, enable the option to let the MatchBox team connect to your instance.  Let the experts keep your stream running smoothly, no matter where you are in the world
- **Clipping continues even if the internet doesn't**: Internet at the event dropped out?  No worries!  MatchBox handles match clipping locally, and will upload the clips whenever internet returns.  No more loosing match footage forever because the internet dropped out for a time; MatchBox only needs the local connection to the scoring system to keep clipping away.

<img width="941" height="997" alt="image" src="https://github.com/user-attachments/assets/4b4467ca-b73b-40d4-bd8b-269f168e7924" />

## Usage

### Setting up OBS
- Enable WebSocket server in OBS:
   - Tools > WebSocket Server Settings
   - Enable WebSocket server
   - Set a password
   - Note the port (usually 4455)

### Configuration
- Fill in the Connection Settings tab:
   - **Event Code**: Your FTC event code
   - **Scoring System Host**: Usually IP of scoring computer
   - **OBS WebSocket Password**: Set in OBS Tools > WebSocket Server Settings
- (Optional) Configure Scene Mapping tab to match your OBS scene names
- Set output directory in Video & Web Settings
- Click "Configure OBS Scenes" to auto-create:
   - Field scenes (Field 1, Field 2, etc.)
   - Shared FTC overlay browser source with chroma key filter
   - Overlay automatically added to all field scenes
- Fill in the fields in the Sync Settings tab to allow match upload
   - Drop me a message to get an access code
   - Match clips will be uploaded to https://jacobspctuneup.tk/FTC/MatchBox/
   - Coming soon: Full integration with <https://www.clipfarm.watch/>

### Operation
1. Start the FTC scoring system
2. Start OBS and begin streaming and recording
3. Launch MatchBox (GUI or CLI)
4. MatchBox will:
   - Connect to scoring system WebSocket
   - Switch OBS scenes based on active field
   - Start local web server for match clips
   - Process and split match videos

### Accessing Match Clips
- **Locally**: http://localhost:80 (or your configured port)
- **Network**: http://[computer-ip]:80
- **mDNS**: http://ftcvideo.local:80 (if supported)

## Troubleshooting

### OBS Connection Issues
- Check OBS WebSocket server is enabled (Tools > WebSocket Server Settings)
- Verify password matches
- Ensure OBS is running before starting MatchBox
- Check Windows firewall isn't blocking connections

### FTC Scoring System Issues
- Verify event code is correct
- Check scoring system is running and accessible
- Test WebSocket URL in browser: `ws://<scoring system host>/stream/display/command/?code=YOURCODE`

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

## Developer Information

### Software Dependencies
- Python 3.8+
- OBS Studio with WebSocket plugin enabled
- FTC Scoring System accessible on the network

### Python Packages
Install required packages:
```bash
pip install -r requirements.txt
```

### Required System Tools
- ffmpeg
- ffprobe
- rsync

### Architecture

#### Core Components
- `MatchBoxCore`: Main application logic
- `MatchBoxGUI`: Tkinter-based graphical interface
- `matchbox-cli.py`: Command-line interface
- Web server: Simple HTTP server for match clips

#### Network Connections
- **FTC Scoring System**: WebSocket connection for match events
- **OBS Studio**: WebSocket connection for scene control
- **Local Network**: HTTP server for match clip access
