#!/usr/bin/env python3
"""
FIRST® MatchBox™
Combines OBS scene switching and match video autosplitting functionality

Based on the design document and existing ftc-obs-autoswitcher and match-video-autosplitter code.
"""

import json
import time
import asyncio
import signal
import threading
import websockets
import obswebsocket
from obswebsocket import requests as obsrequests
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import argparse
import sys
import logging
from pathlib import Path
from local_video_processor import LocalVideoProcessor
import concurrent.futures
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# mDNS/Zeroconf support
try:
    from zeroconf import ServiceInfo, Zeroconf
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger("matchbox")

class MatchBoxCore:
    """Core MatchBox functionality combining OBS switching and video autosplitting"""

    def __init__(self, config=None):
        """Initialize MatchBox with configuration"""
        self.config = config or {}

        # FTC/OBS Settings
        self.event_code = self.config.get('event_code', '')
        self.scoring_host = self.config.get('scoring_host', 'localhost')
        self.scoring_port = self.config.get('scoring_port', 80)
        self.obs_host = self.config.get('obs_host', 'localhost')
        self.obs_port = self.config.get('obs_port', 4455)
        self.obs_password = self.config.get('obs_password', '')
        self.num_fields = self.config.get('num_fields', 2)

        # Video processing settings
        self.frame_increment = self.config.get('frame_increment', 5.0)
        self.max_attempts = self.config.get('max_attempts', 30)
        self.output_dir = Path(self.config.get('output_dir', './match_clips')).absolute()

        # Web server settings
        self.web_port = self.config.get('web_port', 8000)
        self.mdns_name = self.config.get('mdns_name', 'ftcvideo.local')

        # Field to scene mapping
        self.field_scene_mapping = self.config.get('field_scene_mapping', {
            1: "Field 1",
            2: "Field 2"
        })

        # Initialize connection objects
        self.obs_ws = None
        self.ftc_websocket = None
        self.current_field = None
        self.running = False

        # Video processing state
        self.video_splitter = None
        self.current_match_clips = []

        # Web server
        self.web_server = None
        self.web_thread = None

        # mDNS/Zeroconf service
        self.zeroconf = None
        self.service_info = None

        # Local video processing
        self.local_video_processor = None
        self.obs_recording_path = None

        # Callbacks
        self.log_callback = None

        # Create output directory with event code subfolder
        self.clips_dir = self.output_dir / self.event_code
        self.clips_dir.mkdir(exist_ok=True, parents=True)

    def set_log_callback(self, callback):
        """Set callback for logging messages"""
        self.log_callback = callback

    def log(self, message):
        """Log message to console and callback"""
        logger.info(message)
        if self.log_callback:
            self.log_callback(message)

    def connect_to_obs(self):
        """Connect to OBS WebSocket server"""
        try:
            self.obs_ws = obswebsocket.obsws(self.obs_host, self.obs_port, self.obs_password)
            self.obs_ws.connect()
            self.log("Connected to OBS WebSocket server")
            return True
        except Exception as e:
            self.log(f"Error connecting to OBS: {e}")
            return False

    def disconnect_from_obs(self):
        """Disconnect from OBS WebSocket server"""
        if self.obs_ws and hasattr(self.obs_ws, 'ws') and self.obs_ws.ws.connected:
            try:
                self.obs_ws.disconnect()
                self.log("Disconnected from OBS WebSocket server")
            except Exception as e:
                self.log(f"Error disconnecting from OBS: {e}")

    def configure_obs_scenes(self):
        """Auto-configure OBS scenes and sources"""
        if not self.obs_ws:
            if not self.connect_to_obs():
                return False

        try:
            self.log("Starting OBS scene configuration...")

            # Step 1: Get current scenes and sources
            self.log("Getting current scenes...")
            scenes_response = self.obs_ws.call(obsrequests.GetSceneList())
            existing_scenes = [scene['sceneName'] for scene in scenes_response.datain['scenes']]
            self.log(f"Found {len(existing_scenes)} existing scenes")

            # Step 2: Create field scenes FIRST
            self.log("Creating field scenes...")
            for field_num in range(1, self.num_fields + 1):
                scene_name = f"Field {field_num}"
                if scene_name not in existing_scenes:
                    try:
                        self.obs_ws.call(obsrequests.CreateScene(sceneName=scene_name))
                        self.log(f"✓ Created scene: {scene_name}")
                    except Exception as e:
                        self.log(f"✗ Failed to create scene {scene_name}: {e}")
                else:
                    self.log(f"✓ Scene already exists: {scene_name}")

            # Step 3: Get existing sources to avoid duplicates
            self.log("Checking existing sources...")
            try:
                sources_response = self.obs_ws.call(obsrequests.GetInputList())
                existing_sources = [source['inputName'] for source in sources_response.datain['inputs']]
                self.log(f"Found {len(existing_sources)} existing sources")
            except Exception as e:
                self.log(f"Could not get input list: {e}")
                existing_sources = []

            # Step 4: Create shared overlay source
            shared_overlay_name = "FTC Scoring System Overlay"
            if shared_overlay_name not in existing_sources:
                self.log("Creating shared overlay source...")
                overlay_url = (f"http://{self.scoring_host}:{self.scoring_port}/event/{self.event_code}/display/"
                              f"?type=audience&bindToField=all&scoringBarLocation=bottom&allianceOrientation=standard"
                              f"&liveScores=true&mute=false&muteRandomizationResults=false&fieldStyleTimer=false"
                              f"&overlay=true&overlayColor=%23ff00ff&allianceSelectionStyle=classic&awardsStyle=overlay"
                              f"&dualDivisionRankingStyle=sideBySide&rankingsFontSize=larger&showMeetRankings=false"
                              f"&rankingsAllTeams=true")
                self.log(f"Overlay URL: {overlay_url}")

                # Browser source settings
                browser_settings = {
                    "url": overlay_url,
                    "width": 1920,
                    "height": 1080,
                    "shutdown": False,
                    "restart_when_active": False,
                    "reroute_audio": True,  # Enable audio output
                    "monitor_audio": True   # Monitor audio (for live mixing)
                }

                try:
                    # Create the browser source - need to specify a scene for newer API
                    try:
                        # Use the first field scene as the target for creation
                        first_scene = f"Field 1"
                        self.obs_ws.call(obsrequests.CreateInput(
                            sceneName=first_scene,
                            inputName=shared_overlay_name,
                            inputKind="browser_source",
                            inputSettings=browser_settings
                        ))
                        self.log("✓ Used CreateInput API with scene")
                    except Exception as e1:
                        self.log(f"CreateInput failed ({e1}), trying CreateSource...")
                        # Fallback to older API method
                        self.obs_ws.call(obsrequests.CreateSource(
                            sourceName=shared_overlay_name,
                            sourceKind="browser_source",
                            sourceSettings=browser_settings
                        ))
                        self.log("✓ Used CreateSource API")

                    self.log(f"✓ Created shared overlay source: {shared_overlay_name}")

                    # Wait for source to be fully created
                    time.sleep(1.0)

                    # Step 5: Add chroma key filter
                    self.log("Adding chroma key filter...")
                    chroma_settings = {
                        "key_color_type": "magenta",
                        "key_color": 16711935,  # Magenta color value (0xFF00FF)
                        "similarity": 110,
                        "smoothness": 80,
                        "key_color_spill_reduction": 100,
                        "opacity": 1.0,
                        "contrast": 0.0,
                        "brightness": 0.0,
                        "gamma": 0.0
                    }

                    try:
                        # Try newer filter API first
                        try:
                            self.obs_ws.call(obsrequests.CreateSourceFilter(
                                sourceName=shared_overlay_name,
                                filterName="Chroma Key",
                                filterKind="chroma_key_filter_v2",
                                filterSettings=chroma_settings
                            ))
                            self.log("✓ Added chroma key filter (v2)")
                        except Exception as e1:
                            self.log(f"v2 filter failed ({e1}), trying v1...")
                            # Fallback to older filter name
                            self.obs_ws.call(obsrequests.CreateSourceFilter(
                                sourceName=shared_overlay_name,
                                filterName="Chroma Key",
                                filterKind="chroma_key_filter",
                                filterSettings=chroma_settings
                            ))
                            self.log("✓ Added chroma key filter (v1)")

                    except Exception as e:
                        self.log(f"✗ Could not add chroma key filter: {e}")

                except Exception as e:
                    self.log(f"✗ Error creating shared overlay source: {e}")
                    # Don't return False here, continue with scene setup
            else:
                self.log(f"✓ Shared overlay source already exists: {shared_overlay_name}")

            # Step 6: Add the shared overlay to each field scene
            self.log("Adding overlay to scenes...")
            for field_num in range(1, self.num_fields + 1):
                scene_name = f"Field {field_num}"

                try:
                    # Check if the source is already in the scene
                    try:
                        scene_items_response = self.obs_ws.call(obsrequests.GetSceneItemList(sceneName=scene_name))
                        existing_items = [item['sourceName'] for item in scene_items_response.datain['sceneItems']]
                    except Exception:
                        # Fallback for older API
                        existing_items = []

                    if shared_overlay_name not in existing_items:
                        # Skip Field 1 if we created the source there already
                        if scene_name == "Field 1" and shared_overlay_name not in existing_sources:
                            self.log(f"✓ Overlay already in {scene_name} (created there)")
                        else:
                            try:
                                # Try newer API first
                                self.obs_ws.call(obsrequests.CreateSceneItem(
                                    sceneName=scene_name,
                                    sourceName=shared_overlay_name
                                ))
                                self.log(f"✓ Added overlay to {scene_name} (CreateSceneItem)")
                            except Exception as e1:
                                self.log(f"CreateSceneItem failed ({e1}), trying AddSceneItem...")
                                # Fallback to older API method
                                self.obs_ws.call(obsrequests.AddSceneItem(
                                    sceneName=scene_name,
                                    sourceName=shared_overlay_name
                                ))
                                self.log(f"✓ Added overlay to {scene_name} (AddSceneItem)")

                    else:
                        self.log(f"✓ Overlay already exists in {scene_name}")

                except Exception as e:
                    self.log(f"✗ Could not add overlay to {scene_name}: {e}")

            self.log("✅ OBS scene configuration completed successfully!")
            return True

        except Exception as e:
            self.log(f"✗ Error configuring OBS scenes: {e}")
            return False

    def get_obs_recording_path(self):
        """Get current OBS recording file path via WebSocket"""
        if not self.obs_ws:
            return None

        try:
            # Check if recording is active
            record_status = self.obs_ws.call(obsrequests.GetRecordStatus())
            if not record_status.datain.get('outputActive', False):
                self.log("OBS is not currently recording")
                return None

            # Try to get recording output settings
            try:
                # Try advanced file output first
                output_settings = self.obs_ws.call(obsrequests.GetOutputSettings(outputName="adv_file_output"))
                recording_path = output_settings.datain['outputSettings'].get('path')
            except Exception:
                # Fallback: try simple file output
                try:
                    output_settings = self.obs_ws.call(obsrequests.GetOutputSettings(outputName="simple_file_output"))
                    recording_path = output_settings.datain['outputSettings'].get('path')
                except Exception:
                    # Final fallback: use record status filename if available
                    recording_path = record_status.datain.get('outputPath')

            if recording_path:
                self.log(f"Found OBS recording path: {recording_path}")
                return recording_path
            else:
                self.log("Could not determine OBS recording path")
                return None

        except Exception as e:
            self.log(f"Error getting OBS recording path: {e}")
            return None

    def setup_local_video_processor(self):
        """Initialize local video processor with OBS recording path"""
        try:
            self.log("🔍 Setting up local video processor...")

            # Check if LocalVideoProcessor was imported successfully
            try:
                import tempfile
                with tempfile.TemporaryDirectory() as temp_dir:
                    test_processor = LocalVideoProcessor({'output_dir': temp_dir})
                self.log("✅ LocalVideoProcessor import successful")
            except Exception as import_error:
                self.log(f"❌ LocalVideoProcessor import failed: {import_error}")
                return False

            # Get current OBS recording path
            recording_path = self.get_obs_recording_path()

            if recording_path:
                # Create local video processor
                config = {
                    'output_dir': str(self.clips_dir),  # clips_dir is already absolute
                    'pre_match_buffer_seconds': self.config.get('pre_match_buffer_seconds', 10),
                    'post_match_buffer_seconds': self.config.get('post_match_buffer_seconds', 10),
                    'match_duration_seconds': self.config.get('match_duration_seconds', 158)
                }

                self.local_video_processor = LocalVideoProcessor(config)
                self.local_video_processor.set_recording_path(recording_path)
                self.local_video_processor.start_monitoring()

                self.obs_recording_path = recording_path
                self.log(f"✅ Local video processor ready: {recording_path}")
                return True
            else:
                self.log("❌ Could not setup local video processor - no recording path")
                self.log("   Make sure OBS is recording before starting MatchBox")
                return False

        except Exception as e:
            self.log(f"❌ Error setting up local video processor: {e}")
            import traceback
            self.log(f"❌ Full error traceback: {traceback.format_exc()}")
            return False

    def switch_scene(self, field_number):
        """Switch OBS scene based on field number"""
        if field_number not in self.field_scene_mapping:
            self.log(f"No scene mapping found for Field {field_number}")
            return False

        scene_name = self.field_scene_mapping[field_number]
        try:
            response = self.obs_ws.call(obsrequests.SetCurrentProgramScene(sceneName=scene_name))
            if response.status:
                self.log(f"Switched to scene: {scene_name} for Field {field_number}")
                return True
            else:
                self.log(f"Failed to switch scene: {response.error}")
                return False
        except Exception as e:
            self.log(f"Error switching scene: {e}")
            return False

    def start_web_server(self):
        """Start local web server for match clips"""
        try:
            # Create clips directory if it doesn't exist
            self.clips_dir.mkdir(exist_ok=True, parents=True)
            clips_dir_str = str(self.clips_dir.absolute())

            # Create initial index.html with existing files scan
            try:
                self.create_initial_web_interface()
                self.log(f"Created index.html with existing files scan")
            except Exception as e:
                self.log(f"Error creating initial index.html: {e}")

            # Custom handler that serves from a specific directory without changing working directory
            def make_handler(directory):
                class MatchClipHandler(SimpleHTTPRequestHandler):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, directory=directory, **kwargs)

                    def log_message(self, format, *args):
                        # Enable logging for debugging (but suppress routine errors)
                        message = format % args
                        if "Broken pipe" not in message and "Connection reset" not in message:
                            print(f"HTTP: {message}")

                    def address_string(self):
                        """Override to avoid slow reverse DNS lookups"""
                        return str(self.client_address[0])

                    def end_headers(self):
                        # Add CORS headers
                        self.send_header('Access-Control-Allow-Origin', '*')
                        self.send_header('Cache-Control', 'no-cache')
                        super().end_headers()

                    def handle_one_request(self):
                        """Handle a single HTTP request with better error handling"""
                        import time
                        start_time = time.time()
                        try:
                            super().handle_one_request()
                            duration = time.time() - start_time
                            if duration > 1.0:  # Log slow requests
                                print(f"⚠️ Slow HTTP request took {duration:.2f}s")
                        except (BrokenPipeError, ConnectionResetError):
                            # Client disconnected - this is normal, don't spam logs
                            pass
                        except Exception as e:
                            # Log other unexpected errors
                            self.log_error(f"Request handling error: {e}")

                    def copyfile(self, source, outputfile):
                        """Copy file data with proper error handling for broken pipes"""
                        try:
                            super().copyfile(source, outputfile)
                        except (BrokenPipeError, ConnectionResetError):
                            # Client disconnected while downloading - normal behavior
                            pass
                return MatchClipHandler

            def run_server():
                try:
                    self.log(f"Starting web server on port {self.web_port}")
                    self.log(f"Serving directory: {clips_dir_str}")
                    self.log(f"Access match clips at http://localhost:{self.web_port}")

                    # Create handler class with the specific directory
                    HandlerClass = make_handler(clips_dir_str)
                    # Use ThreadingHTTPServer for better performance and bind to all interfaces
                    self.web_server = ThreadingHTTPServer(('0.0.0.0', self.web_port), HandlerClass)
                    # Prevent the server from hanging on to connections
                    self.web_server.allow_reuse_address = True
                    self.web_server.timeout = 30  # 30 second timeout for requests
                    self.web_server.serve_forever()
                except OSError as e:
                    if "Address already in use" in str(e):
                        self.log(f"Web server port {self.web_port} is already in use")
                    else:
                        self.log(f"Web server OS error: {e}")
                except Exception as e:
                    self.log(f"Web server error: {e}")

            self.web_thread = threading.Thread(target=run_server, daemon=True)
            self.web_thread.start()

            # Give server a moment to start
            import time
            time.sleep(1.0)

            # Test the server
            try:
                import urllib.request
                test_url = f"http://localhost:{self.web_port}/index.html"
                with urllib.request.urlopen(test_url, timeout=2) as response:
                    if response.getcode() == 200:
                        self.log("✅ Web server test successful")
                    else:
                        self.log(f"❌ Web server test failed: HTTP {response.getcode()}")
            except Exception as e:
                self.log(f"❌ Web server test failed: {e}")

            # Register mDNS service for local network discovery
            self.register_mdns_service()

            return True

        except Exception as e:
            self.log(f"Error starting web server: {e}")
            return False

    def stop_web_server(self):
        """Stop local web server"""
        if self.web_server:
            try:
                self.web_server.shutdown()
                self.web_server.server_close()
                self.log("Web server stopped")
            except Exception as e:
                self.log(f"Error stopping web server: {e}")

    def register_mdns_service(self):
        """Register mDNS service for local network discovery"""
        if not ZEROCONF_AVAILABLE:
            self.log("⚠️ Zeroconf not available - install with: pip install zeroconf")
            return False

        # Run mDNS registration in a separate thread to avoid event loop conflicts
        def _register_in_thread():
            try:
                # Get local IP address
                import socket
                hostname = socket.gethostname()
                local_ip = socket.gethostbyname(hostname)

                # Use actual network IP instead of localhost
                # Get the IP that would be used to connect to external hosts
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                        s.connect(("8.8.8.8", 80))  # Connect to external IP to find local IP
                        local_ip = s.getsockname()[0]
                except:
                    pass  # Fallback to hostname resolution

                self.log(f"📡 mDNS: Using IP {local_ip}")

                # Create Zeroconf instance in this thread
                self.zeroconf = Zeroconf()

                # Parse mDNS name to get hostname
                mdns_name = self.mdns_name
                if mdns_name.endswith('.local'):
                    hostname_part = mdns_name[:-6]  # Remove '.local'
                else:
                    hostname_part = mdns_name

                # Register HTTP service
                service_name = f"{hostname_part}._http._tcp.local."

                self.service_info = ServiceInfo(
                    "_http._tcp.local.",
                    service_name,
                    addresses=[socket.inet_aton(local_ip)],
                    port=self.web_port,
                    properties={
                        'path': '/',
                        'description': f'FIRST MatchBox - {self.event_code}',
                        'event': self.event_code,
                        'service': 'matchbox'
                    },
                    server=f"{hostname_part}.local."
                )

                self.zeroconf.register_service(self.service_info)
                self.log(f"✅ mDNS service registered: http://{mdns_name}:{self.web_port}")
                self.log(f"📡 Access from network: {local_ip}:{self.web_port}")

            except Exception as e:
                import traceback
                self.log(f"❌ Failed to register mDNS service: {type(e).__name__}: {e}")
                self.log(f"❌ Full traceback: {traceback.format_exc()}")

        # Start registration in background thread
        mdns_thread = threading.Thread(target=_register_in_thread, daemon=True)
        mdns_thread.start()
        return True

    def unregister_mdns_service(self):
        """Unregister mDNS service"""
        try:
            if self.service_info and self.zeroconf:
                self.zeroconf.unregister_service(self.service_info)
                self.log("mDNS service unregistered")

            if self.zeroconf:
                self.zeroconf.close()
                self.zeroconf = None
                self.service_info = None

        except Exception as e:
            self.log(f"Error unregistering mDNS service: {e}")

    async def monitor_ftc_websocket(self):
        """Monitor FTC scoring system WebSocket for match events"""
        if not self.connect_to_obs():
            self.log("Failed to connect to OBS. Exiting.")
            return

        # Start web server
        self.start_web_server()

        # Setup local video processing if OBS is recording
        self.setup_local_video_processor()

        ftc_ws_url = f"ws://{self.scoring_host}:{self.scoring_port}/stream/display/command/?code={self.event_code}"
        self.log(f"Connecting to FTC WebSocket: {ftc_ws_url}")
        self.log(f"Field-scene mapping: {json.dumps(self.field_scene_mapping, indent=2)}")

        self.running = True
        try:
            async with websockets.connect(ftc_ws_url) as websocket:
                self.ftc_websocket = websocket
                self.log("Connected to FTC scoring system WebSocket")

                # Drain initial backlog of old events for 5 seconds
                self.log("⏳ Draining initial backlog of old events...")
                backlog_end_time = time.time() + 5.0
                backlog_count = 0

                while time.time() < backlog_end_time:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=0.5)
                        backlog_count += 1
                        # Just discard these messages without processing
                    except asyncio.TimeoutError:
                        # No more messages in backlog, continue waiting
                        continue

                if backlog_count > 0:
                    self.log(f"🗑️ Discarded {backlog_count} old events from backlog")
                self.log("✅ Ready to process new FTC events")

                while self.running:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                        data = json.loads(message)

                        if data.get("type") == "SHOW_PREVIEW" or data.get("type") == "SHOW_MATCH":
                            # Extract field number
                            field_number = data.get("field")
                            if field_number is None and "params" in data:
                                field_number = data["params"].get("field")

                            if field_number is not None and field_number != self.current_field:
                                self.log(f"Field change detected: {self.current_field} -> {field_number}")
                                if self.switch_scene(field_number):
                                    self.current_field = field_number

                        elif data.get("type") == "START_MATCH":
                            # Match started - schedule delayed clip generation
                            match_info = data.get("params", {})
                            self.log(f"🎬 Match started: {match_info}")

                            # Add timestamp for accurate clip timing
                            match_info['start_timestamp'] = time.time()

                            # Schedule clip generation to start after full match duration
                            if self.local_video_processor:
                                self.log("🎬 Scheduling delayed clip generation...")
                                asyncio.create_task(self.generate_match_clip_delayed(match_info))
                            else:
                                self.log("❌ Local video processor not available for clipping")

                        elif data.get("type") == "END_MATCH":
                            # Match ended - log event (clip generation scheduled from START_MATCH)
                            match_info = data.get("params", {})
                            self.log(f"🏁 Match ended: {match_info}")

                    except asyncio.TimeoutError:
                        continue
                    except json.JSONDecodeError as e:
                        if message != "pong":
                            self.log(f"Error decoding message: {e}")
                    except websockets.exceptions.ConnectionClosed:
                        if self.running:
                            raise
                    except Exception as e:
                        if self.running:
                            self.log(f"Error processing message: {e}")

        except asyncio.CancelledError:
            self.log("WebSocket monitoring cancelled")
        except websockets.exceptions.ConnectionClosed:
            self.log("Connection to FTC scoring system closed. Check server and event code.")
        except Exception as e:
            if self.running:
                self.log(f"WebSocket error: {e}")
        finally:
            await self.shutdown()

    async def generate_match_clip_delayed(self, match_info):
        """Generate a match clip after waiting for the full match duration"""
        # Calculate total time to wait: match duration + post-match buffer + extra safety margin
        match_duration = self.config.get('match_duration_seconds', 158)  # FTC match duration
        post_match_buffer = self.config.get('post_match_buffer_seconds', 10)
        safety_margin = 8  # Extra time for transitions and safety

        total_wait_time = match_duration + post_match_buffer + safety_margin

        self.log(f"🎬 Waiting {total_wait_time} seconds for match to complete before generating clip...")
        await asyncio.sleep(total_wait_time)

        self.log("🎬 Match duration complete - starting clip generation...")
        await self.generate_match_clip(match_info)

    async def generate_match_clip(self, match_info):
        """Generate a match clip using the local video processor"""
        try:
            self.log(f"🎬 Generating clip for match: {match_info}")

            # Double-check processor is available
            if not self.local_video_processor:
                self.log("❌ Local video processor is None!")
                return

            # Extract clip using local video processor
            self.log("🎬 Calling local_video_processor.extract_clip()...")
            clip_path = await self.local_video_processor.extract_clip(match_info)
            self.log(f"🎬 extract_clip() returned: {clip_path}")

            if clip_path:
                self.log(f"✅ Match clip created: {clip_path}")
                self.current_match_clips.append(str(clip_path))

                # Update web interface by refreshing index.html with latest clips
                await self.update_web_interface_clips()

            else:
                self.log(f"❌ Failed to create match clip - extract_clip returned None")

        except Exception as e:
            self.log(f"❌ Error generating match clip: {e}")
            import traceback
            self.log(f"❌ Full traceback: {traceback.format_exc()}")

    def _scan_video_files(self):
        """Scan for video files in clips directory"""
        video_extensions = ('.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm')
        video_files = []

        try:
            for file_path in self.clips_dir.iterdir():
                if file_path.is_file() and file_path.suffix.lower() in video_extensions:
                    video_files.append(file_path)
        except Exception as e:
            print(f"Error scanning for video files: {e}")

        # Sort files by modification time (newest first)
        video_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return video_files

    def _generate_html_content(self, video_files):
        """Generate HTML content for the web interface"""
        clips_dir_str = str(self.clips_dir.absolute())

        # Generate file list HTML
        if video_files:
            file_list_html = "<ul>"
            for video_file in video_files:
                file_size = video_file.stat().st_size
                size_mb = file_size / (1024 * 1024)
                file_list_html += f'<li><a href="{video_file.name}">{video_file.name}</a> <small>({size_mb:.1f} MB)</small></li>'
            file_list_html += "</ul>"
        else:
            file_list_html = "<p><em>No match clips available yet...</em></p>"

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FIRST&reg; MatchBox&trade; - Match Clips</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; }}
        h1 {{ color: #0066cc; }}
        .status {{ padding: 10px; background: #f0f8ff; border-radius: 5px; margin: 20px 0; }}
        .footer {{ margin-top: 40px; color: #666; font-size: 0.9em; }}
        li {{ margin: 5px 0; }}
        small {{ color: #666; margin-left: 10px; }}
    </style>
    <meta http-equiv="refresh" content="30">
</head>
<body>
    <h1>&#x1F3A5; FIRST&reg; MatchBox&trade;</h1>
    <div class="status">
        <h3>Match Clips Server</h3>
        <p><strong>Status:</strong> Running on port {self.web_port}</p>
        <p><strong>Event Code:</strong> {self.event_code}</p>
        <p><strong>Output Directory:</strong> {clips_dir_str}</p>
        <p><strong>Total Clips:</strong> {len(video_files)}</p>
    </div>

    <h3>&#x1F4C1; Available Match Clips</h3>
    {file_list_html}

    <div class="footer">
        <p><em>This web interface provides local access to match clips for referees and field staff.</em></p>
        <p>This page automatically refreshes every 30 seconds to show new clips.</p>
    </div>
</body>
</html>"""

    def create_initial_web_interface(self):
        """Create initial web interface with existing files (sync version)"""
        video_files = self._scan_video_files()
        html_content = self._generate_html_content(video_files)

        index_path = self.clips_dir / "index.html"
        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(html_content)

    async def update_web_interface_clips(self):
        """Update web interface to show available clips"""
        try:
            video_files = self._scan_video_files()
            html_content = self._generate_html_content(video_files)

            index_path = self.clips_dir / "index.html"
            with open(index_path, 'w', encoding='utf-8') as f:
                f.write(html_content)

        except Exception as e:
            self.log(f"Error updating web interface: {e}")

    async def shutdown(self):
        """Gracefully shutdown MatchBox"""
        self.log("Shutting down MatchBox...")
        self.running = False

        # Close FTC WebSocket
        if self.ftc_websocket and not self.ftc_websocket.closed:
            await self.ftc_websocket.close()
            self.log("Closed FTC WebSocket connection")

        # Disconnect from OBS
        self.disconnect_from_obs()

        # Stop web server and mDNS service
        self.stop_web_server()
        self.unregister_mdns_service()

        # Stop local video processor
        if self.local_video_processor:
            self.local_video_processor.stop_monitoring()
            self.log("Stopped local video processor")

        self.log("MatchBox shutdown complete")


class MatchBoxGUI:
    """Tkinter GUI for MatchBox"""

    def __init__(self, root):
        self.root = root
        self.root.title("FIRST® MatchBox™")
        self.root.geometry("900x700")
        self.root.resizable(True, True)

        self.matchbox = None
        self.async_loop = None
        self.monitor_task = None
        self.thread = None

        # Default configuration
        self.default_config = {
            'event_code': '',
            'scoring_host': 'localhost',
            'scoring_port': 80,
            'obs_host': 'localhost',
            'obs_port': 4455,
            'obs_password': '',
            'num_fields': 2,
            'output_dir': './match_clips',
            'web_port': 8000,
            'field_scene_mapping': {1: "Field 1", 2: "Field 2"}
        }

        self.create_widgets()
        self.load_config()

    def create_widgets(self):
        """Create GUI widgets"""
        # Main frame
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Title
        title_label = ttk.Label(main_frame, text="FIRST® MatchBox™", font=("", 16, "bold"))
        title_label.pack(pady=(0, 10))

        # Create notebook with tabs
        notebook = ttk.Notebook(main_frame)
        notebook.pack(fill=tk.BOTH, expand=True, pady=5)

        # Connection settings tab
        self.create_connection_tab(notebook)

        # Scene mapping tab
        self.create_scene_mapping_tab(notebook)

        # Video settings tab
        self.create_video_settings_tab(notebook)

        # Control & Log Frame
        control_frame = ttk.Frame(main_frame, padding="10")
        control_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        # Control buttons
        button_frame = ttk.Frame(control_frame)
        button_frame.pack(fill=tk.X)

        self.configure_obs_button = ttk.Button(button_frame, text="Configure OBS Scenes",
                                             command=self.configure_obs_scenes)
        self.configure_obs_button.pack(side=tk.LEFT, padx=5)

        self.start_button = ttk.Button(button_frame, text="Start MatchBox",
                                     command=self.start_matchbox)
        self.start_button.pack(side=tk.LEFT, padx=5)

        self.stop_button = ttk.Button(button_frame, text="Stop MatchBox",
                                    command=self.stop_matchbox, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=5)

        ttk.Button(button_frame, text="Save Config", command=self.save_config).pack(side=tk.LEFT, padx=5)

        # Status indicator
        self.status_var = tk.StringVar(value="Status: Not Running 🔴")
        status_label = ttk.Label(button_frame, textvariable=self.status_var)
        status_label.pack(side=tk.RIGHT, padx=5)

        # Log area
        ttk.Label(control_frame, text="Log", font=("", 10, "bold")).pack(anchor=tk.W, pady=(10, 5))

        self.log_text = scrolledtext.ScrolledText(control_frame, height=12)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.config(state=tk.DISABLED)

    def create_connection_tab(self, notebook):
        """Create connection settings tab"""
        conn_frame = ttk.Frame(notebook, padding="10")
        notebook.add(conn_frame, text="Connection Settings")

        # FTC Settings
        ttk.Label(conn_frame, text="FTC Scoring System", font=("", 12, "bold")).grid(
            row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 5))

        ttk.Label(conn_frame, text="Event Code:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.event_code_var = tk.StringVar()
        ttk.Entry(conn_frame, textvariable=self.event_code_var, width=30).grid(
            row=1, column=1, sticky=tk.W, pady=2)

        ttk.Label(conn_frame, text="Scoring System Host:").grid(row=2, column=0, sticky=tk.W, pady=2)
        self.scoring_host_var = tk.StringVar(value="localhost")
        ttk.Entry(conn_frame, textvariable=self.scoring_host_var, width=30).grid(
            row=2, column=1, sticky=tk.W, pady=2)

        ttk.Label(conn_frame, text="Port:").grid(row=2, column=2, sticky=tk.W, pady=2)
        self.scoring_port_var = tk.StringVar(value="80")
        ttk.Entry(conn_frame, textvariable=self.scoring_port_var, width=6).grid(
            row=2, column=3, sticky=tk.W, pady=2)

        ttk.Label(conn_frame, text="Number of Fields:").grid(row=3, column=0, sticky=tk.W, pady=2)
        self.num_fields_var = tk.StringVar(value="2")
        ttk.Entry(conn_frame, textvariable=self.num_fields_var, width=6).grid(
            row=3, column=1, sticky=tk.W, pady=2)

        # OBS Settings
        ttk.Label(conn_frame, text="OBS Settings", font=("", 12, "bold")).grid(
            row=4, column=0, columnspan=3, sticky=tk.W, pady=(10, 5))

        ttk.Label(conn_frame, text="OBS WebSocket Host:").grid(row=5, column=0, sticky=tk.W, pady=2)
        self.obs_host_var = tk.StringVar(value="localhost")
        ttk.Entry(conn_frame, textvariable=self.obs_host_var, width=30).grid(
            row=5, column=1, sticky=tk.W, pady=2)

        ttk.Label(conn_frame, text="Port:").grid(row=5, column=2, sticky=tk.W, pady=2)
        self.obs_port_var = tk.StringVar(value="4455")
        ttk.Entry(conn_frame, textvariable=self.obs_port_var, width=6).grid(
            row=5, column=3, sticky=tk.W, pady=2)

        ttk.Label(conn_frame, text="Password:").grid(row=6, column=0, sticky=tk.W, pady=2)
        self.obs_password_var = tk.StringVar()
        ttk.Entry(conn_frame, textvariable=self.obs_password_var, width=30, show="*").grid(
            row=6, column=1, sticky=tk.W, pady=2)

    def create_scene_mapping_tab(self, notebook):
        """Create scene mapping tab"""
        mapping_frame = ttk.Frame(notebook, padding="10")
        notebook.add(mapping_frame, text="Scene Mapping")

        ttk.Label(mapping_frame, text="Field to Scene Mapping", font=("", 12, "bold")).grid(
            row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 10))

        # Scene mapping entries
        self.scene_mappings = {}
        for i in range(1, 3):
            ttk.Label(mapping_frame, text=f"Field {i} Scene:").grid(
                row=i, column=0, sticky=tk.W, pady=2)
            scene_var = tk.StringVar(value=f"Field {i}")
            ttk.Entry(mapping_frame, textvariable=scene_var, width=30).grid(
                row=i, column=1, sticky=tk.W, pady=2)
            self.scene_mappings[i] = scene_var

    def create_video_settings_tab(self, notebook):
        """Create video settings tab"""
        video_frame = ttk.Frame(notebook, padding="10")
        notebook.add(video_frame, text="Video & Web Settings")

        # Output settings
        ttk.Label(video_frame, text="Video Output", font=("", 12, "bold")).grid(
            row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 5))

        ttk.Label(video_frame, text="Output Directory:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.output_dir_var = tk.StringVar(value="./match_clips")
        ttk.Entry(video_frame, textvariable=self.output_dir_var, width=40).grid(
            row=1, column=1, sticky=tk.W, pady=2)
        ttk.Button(video_frame, text="Browse...", command=self.browse_output_dir).grid(
            row=1, column=2, sticky=tk.W, pady=2)

        # Web server settings
        ttk.Label(video_frame, text="Local Web Server", font=("", 12, "bold")).grid(
            row=2, column=0, columnspan=3, sticky=tk.W, pady=(10, 5))

        ttk.Label(video_frame, text="Web Server Port:").grid(row=3, column=0, sticky=tk.W, pady=2)
        self.web_port_var = tk.StringVar(value="8000")
        ttk.Entry(video_frame, textvariable=self.web_port_var, width=6).grid(
            row=3, column=1, sticky=tk.W, pady=2)

        # Info label
        # Video processing settings
        ttk.Label(video_frame, text="Video Processing", font=("", 12, "bold")).grid(
            row=4, column=0, columnspan=3, sticky=tk.W, pady=(10, 5))

        ttk.Label(video_frame, text="Pre-match buffer (seconds):").grid(row=5, column=0, sticky=tk.W, pady=2)
        self.pre_match_buffer_var = tk.StringVar(value="10")
        ttk.Entry(video_frame, textvariable=self.pre_match_buffer_var, width=6).grid(
            row=5, column=1, sticky=tk.W, pady=2)

        ttk.Label(video_frame, text="Post-match buffer (seconds):").grid(row=6, column=0, sticky=tk.W, pady=2)
        self.post_match_buffer_var = tk.StringVar(value="10")
        ttk.Entry(video_frame, textvariable=self.post_match_buffer_var, width=6).grid(
            row=6, column=1, sticky=tk.W, pady=2)

        ttk.Label(video_frame, text="Match duration (seconds):").grid(row=7, column=0, sticky=tk.W, pady=2)
        self.match_duration_var = tk.StringVar(value="158")
        ttk.Entry(video_frame, textvariable=self.match_duration_var, width=6).grid(
            row=7, column=1, sticky=tk.W, pady=2)

        info_text = ("Match clips will be available at http://localhost:PORT\n"
                    "Refs on the scoring network can access video clips locally\n"
                    "MatchBox will automatically detect OBS recording and create clips")
        ttk.Label(video_frame, text=info_text, foreground="gray").grid(
            row=8, column=0, columnspan=3, sticky=tk.W, pady=5)

    def browse_output_dir(self):
        """Browse for output directory"""
        directory = filedialog.askdirectory(
            initialdir=self.output_dir_var.get(),
            title="Select Output Directory for Match Clips"
        )
        if directory:
            self.output_dir_var.set(directory)

    def get_config(self):
        """Get current configuration from GUI"""
        config = {
            'event_code': self.event_code_var.get(),
            'scoring_host': self.scoring_host_var.get(),
            'scoring_port': int(self.scoring_port_var.get()) if self.scoring_port_var.get().isdigit() else 80,
            'obs_host': self.obs_host_var.get(),
            'obs_port': int(self.obs_port_var.get()) if self.obs_port_var.get().isdigit() else 4455,
            'obs_password': self.obs_password_var.get(),
            'num_fields': int(self.num_fields_var.get()) if self.num_fields_var.get().isdigit() else 2,
            'output_dir': self.output_dir_var.get(),
            'web_port': int(self.web_port_var.get()) if self.web_port_var.get().isdigit() else 8000,
            'pre_match_buffer_seconds': int(self.pre_match_buffer_var.get()) if self.pre_match_buffer_var.get().isdigit() else 10,
            'post_match_buffer_seconds': int(self.post_match_buffer_var.get()) if self.post_match_buffer_var.get().isdigit() else 10,
            'match_duration_seconds': int(self.match_duration_var.get()) if self.match_duration_var.get().isdigit() else 158,
            'field_scene_mapping': {int(k): v.get() for k, v in self.scene_mappings.items()}
        }
        return config

    def load_config_to_gui(self, config):
        """Load configuration into GUI"""
        self.event_code_var.set(config.get('event_code', ''))
        self.scoring_host_var.set(config.get('scoring_host', 'localhost'))
        self.scoring_port_var.set(str(config.get('scoring_port', 80)))
        self.obs_host_var.set(config.get('obs_host', 'localhost'))
        self.obs_port_var.set(str(config.get('obs_port', 4455)))
        self.obs_password_var.set(config.get('obs_password', ''))
        self.num_fields_var.set(str(config.get('num_fields', 2)))
        self.output_dir_var.set(config.get('output_dir', './match_clips'))
        self.web_port_var.set(str(config.get('web_port', 8000)))
        self.pre_match_buffer_var.set(str(config.get('pre_match_buffer_seconds', 10)))
        self.post_match_buffer_var.set(str(config.get('post_match_buffer_seconds', 10)))
        self.match_duration_var.set(str(config.get('match_duration_seconds', 158)))

        # Load scene mappings
        field_scene_mapping = config.get('field_scene_mapping', {})
        for field_num, scene_var in self.scene_mappings.items():
            scene_var.set(field_scene_mapping.get(field_num, f"Field {field_num}"))

    def save_config(self):
        """Save configuration to file"""
        config = self.get_config()
        try:
            with open("matchbox_config.json", "w") as f:
                json.dump(config, f, indent=2)
            self.log("Configuration saved to matchbox_config.json")
        except Exception as e:
            self.log(f"Error saving configuration: {e}")
            messagebox.showerror("Error", f"Failed to save configuration: {e}")

    def load_config(self):
        """Load configuration from file"""
        try:
            with open("matchbox_config.json", "r") as f:
                config = json.load(f)
            self.load_config_to_gui(config)
            self.log("Configuration loaded from matchbox_config.json")
        except FileNotFoundError:
            self.load_config_to_gui(self.default_config)
            self.log("No configuration file found, using defaults")
        except Exception as e:
            self.load_config_to_gui(self.default_config)
            self.log(f"Error loading configuration: {e}")

    def configure_obs_scenes(self):
        """Configure OBS scenes"""
        config = self.get_config()
        if not config['event_code']:
            messagebox.showerror("Error", "Event code is required")
            return

        # Create temporary MatchBox instance just for OBS configuration
        temp_matchbox = MatchBoxCore(config)
        temp_matchbox.set_log_callback(self.log)

        if temp_matchbox.configure_obs_scenes():
            self.log("OBS scenes configured successfully!")
        else:
            self.log("Failed to configure OBS scenes")

        temp_matchbox.disconnect_from_obs()

    def start_matchbox(self):
        """Start MatchBox operation"""
        config = self.get_config()

        if not config['event_code']:
            messagebox.showerror("Error", "Event code is required")
            return

        # Create MatchBox instance
        self.matchbox = MatchBoxCore(config)
        self.matchbox.set_log_callback(self.log)

        # Create new event loop
        self.async_loop = asyncio.new_event_loop()

        # Start monitoring in separate thread
        self.thread = threading.Thread(target=self.run_async_monitoring, daemon=True)
        self.thread.start()

        # Update UI
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.configure_obs_button.config(state=tk.DISABLED)
        self.status_var.set("Status: Running 🟢")

        self.log("MatchBox started!")
        self.log(f"Match clips will be available at http://localhost:{config['web_port']}")
        if ZEROCONF_AVAILABLE:
            mdns_name = config.get('mdns_name', 'ftcvideo.local')
            self.log(f"Network access: http://{mdns_name}:{config['web_port']}")

    def run_async_monitoring(self):
        """Run async monitoring in separate thread"""
        asyncio.set_event_loop(self.async_loop)
        self.monitor_task = self.async_loop.create_task(self.matchbox.monitor_ftc_websocket())

        try:
            self.async_loop.run_until_complete(self.monitor_task)
        except asyncio.CancelledError:
            pass
        finally:
            self.root.after(0, self.update_ui_after_stop)

    def stop_matchbox(self):
        """Stop MatchBox operation"""
        if self.matchbox and self.matchbox.running:
            self.log("Stopping MatchBox...")

            # Cancel monitoring task
            if self.monitor_task and not self.monitor_task.done():
                self.async_loop.call_soon_threadsafe(self.monitor_task.cancel)

            # Schedule shutdown
            shutdown_task = asyncio.run_coroutine_threadsafe(
                self.matchbox.shutdown(), self.async_loop)

            try:
                shutdown_task.result(timeout=5)
            except concurrent.futures.TimeoutError:
                self.log("Shutdown timed out")
            except Exception as e:
                self.log(f"Error during shutdown: {e}")

            self.matchbox.running = False

    def update_ui_after_stop(self):
        """Update UI after MatchBox stops"""
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.configure_obs_button.config(state=tk.NORMAL)
        self.status_var.set("Status: Not Running 🔴")
        self.log("MatchBox stopped")

    def log(self, message):
        """Log message to GUI"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"{time.strftime('%H:%M:%S')} - {message}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def on_closing(self):
        """Handle window close"""
        if self.matchbox and self.matchbox.running:
            self.stop_matchbox()
            time.sleep(1)
        self.root.destroy()


def main():
    """Main function"""
    parser = argparse.ArgumentParser(description="FIRST® MatchBox™ - FRC Webcast Unit @ home")
    parser.add_argument("--config", "-c", help="Configuration file path")
    parser.add_argument("--cli", action="store_true", help="Run in CLI mode (no GUI)")
    parser.add_argument("--event-code", help="FTC Event Code")
    parser.add_argument("--scoring-host", default="localhost", help="Scoring system host")
    parser.add_argument("--scoring-port", type=int, default=80, help="Scoring system port")
    parser.add_argument("--obs-host", default="localhost", help="OBS WebSocket host")
    parser.add_argument("--obs-port", type=int, default=4455, help="OBS WebSocket port")
    parser.add_argument("--obs-password", default="", help="OBS WebSocket password")

    args = parser.parse_args()

    # Load configuration
    config = {}
    if args.config:
        try:
            with open(args.config, 'r') as f:
                config = json.load(f)
        except Exception as e:
            print(f"Error loading config file: {e}")
            sys.exit(1)

    # Override config with command line arguments
    if args.event_code:
        config['event_code'] = args.event_code
    if args.scoring_host != "localhost":
        config['scoring_host'] = args.scoring_host
    if args.scoring_port != 80:
        config['scoring_port'] = args.scoring_port
    if args.obs_host != "localhost":
        config['obs_host'] = args.obs_host
    if args.obs_port != 4455:
        config['obs_port'] = args.obs_port
    if args.obs_password:
        config['obs_password'] = args.obs_password

    if args.cli:
        # CLI mode
        if not config.get('event_code'):
            print("Event code is required")
            sys.exit(1)

        print("Starting MatchBox in CLI mode...")
        matchbox = MatchBoxCore(config)

        def signal_handler(sig, frame):
            print("\nShutting down...")
            asyncio.create_task(matchbox.shutdown())
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)

        try:
            asyncio.run(matchbox.monitor_ftc_websocket())
        except KeyboardInterrupt:
            print("\nShutting down...")
    else:
        # GUI mode
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except:
            pass

        root = tk.Tk()

        # Apply Sun Valley theme on Linux
        import platform
        if platform.system() == "Linux":
            try:
                import sv_ttk
                sv_ttk.set_theme("dark")
                logger.info("Applied Sun Valley theme")
            except ImportError:
                logger.info("Sun Valley theme not available - install with: pip install sv-ttk")
            except Exception as e:
                logger.info(f"Could not apply Sun Valley theme: {e}")

        app = MatchBoxGUI(root)
        root.protocol("WM_DELETE_WINDOW", app.on_closing)

        # Load config if provided
        if config:
            app.load_config_to_gui(config)

        root.mainloop()


if __name__ == "__main__":
    main()