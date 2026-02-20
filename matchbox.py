#!/usr/bin/env python3
"""
MatchBox™ for FIRST® Tech Challenge
Combines OBS scene switching and match video autosplitting functionality

Based on the design document and existing ftc-obs-autoswitcher and match-video-autosplitter code.
"""

import json
import time
import asyncio
import signal
import threading
import websockets.client
import websockets.exceptions
from websockets.client import WebSocketClientProtocol
import obswebsocket
from obswebsocket import requests as obsrequests  # pyright: ignore[reportAny]
import tkinter as tk
from tkinter import PhotoImage, TclError, ttk, messagebox, filedialog
import argparse
import sys
import logging
from pathlib import Path
import concurrent.futures
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Callable, cast, override
from zeroconf import ServiceInfo, Zeroconf
import os
import subprocess
from datetime import datetime, timedelta

if TYPE_CHECKING:
    from web_api.ws_tunnel_client import WSTunnelClient

# Configure logging
class GUILogHandler(logging.Handler):
    """Custom logging handler that routes messages to a GUI callback and WebSocket broadcaster (thread-safe)"""
    def __init__(self) -> None:
        super().__init__()
        self.root: tk.Tk | None = None
        self.callback: Callable[[str, str], None] | None = None  # (level, message)
        self.ws_broadcaster: Any = None  # WebSocketBroadcaster, set when WS server starts

    def set_callback(self, root: tk.Tk, callback: Callable[[str, str], None] | None) -> None:
        self.root = root
        self.callback = callback

    @override
    def handle(self, record: logging.LogRecord) -> bool:
        # Skip lock acquisition - we just schedule a callback
        if self.callback and self.root:
            try:
                _ = self.root.after_idle(self.callback, record.levelname, record.getMessage())
            except Exception:
                pass  # GUI might be destroyed
        # Broadcast to WebSocket clients
        if self.ws_broadcaster:
            try:
                self.ws_broadcaster.broadcast_log(record.levelname, record.getMessage())
            except Exception:
                pass
        return True

    @override
    def emit(self, record: logging.LogRecord) -> None:
        pass  # Required override - actual work done in handle()

log_filename = f"matchbox_{datetime.now().strftime('%Y-%m-%d')}.log"
if sys.platform == "darwin" and getattr(sys, 'frozen', False):
    log_filename = str(Path.home() / "Desktop" / log_filename)
gui_handler = GUILogHandler()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(log_filename),
        gui_handler,
    ]
)
logger = logging.getLogger("matchbox")

# Admin password authentication
# To generate a new hash, run: python3 generate_admin_hash.py
ADMIN_SALT = b'\x13\xc7\x90+;<1$;\xdb,\x10\xd1\x16z\xb4'
ADMIN_HASH = 'b2dccbe9889768f2f27b6715701a56a05cede5ca6a7d04494273a5a803d9bbcb'

# Import after logging is configured so it uses our config
from local_video_processor import LocalVideoProcessor


def get_rsync_path() -> str:
    """Get path to bundled rsync binary, or fall back to system PATH"""
    if getattr(sys, 'frozen', False):
        # Running in PyInstaller bundle
        meipass: str | None = getattr(sys, '_MEIPASS', None)
        if meipass:
            base_path = Path(meipass)
            rsync_name = 'rsync.exe' if sys.platform == 'win32' else 'rsync'
            bundled_path = base_path / rsync_name
            if bundled_path.exists():
                logger.debug(f"Using bundled rsync: {bundled_path}")
                return str(bundled_path)

    # Fall back to system PATH
    logger.debug("Using system rsync")
    return 'rsync'


class MatchBoxConfig:
    def __init__(self):
        # Initialize all values to their defaults
        self.event_code: str = ''
        self.scoring_host: str = ''
        self.scoring_port: int = 80
        self.obs_host: str = 'localhost'
        self.obs_port: int = 4455
        self.obs_password: str = ''
        self.output_dir: str = sys.platform == "darwin" and getattr(sys, 'frozen', False) and str(Path.home() / "Desktop" / "match_clips") or sys.platform == "win32" and ".\\match_clips" or './match_clips'
        self.web_port: int = 80
        self.mdns_name: str = 'ftcvideo.local'
        self.field_scene_mapping: dict[int, str] = {1: "Field 1", 2: "Field 2"}
        self.frame_increment: float = 5.0
        self.max_attempts: int = 30
        self.pre_match_buffer_seconds: int = 10
        self.post_match_buffer_seconds: int = 10
        self.match_duration_seconds: int = 158
        # rsync settings
        self.rsync_enabled: bool = False
        self.rsync_host: str = ''
        self.rsync_module: str = ''
        self.rsync_username: str = ''
        self.rsync_password: str = ''
        self.rsync_interval_seconds: int = 60
        # Tunnel settings
        self.tunnel_relay_url: str = ''
        self.tunnel_password: str = ''
        self.tunnel_allow_admin: bool = True

class MatchBoxCore:
    """Core MatchBox functionality combining OBS switching and video autosplitting"""

    def __init__(self, config: MatchBoxConfig):
        """Initialize MatchBox with configuration"""
        self.config: MatchBoxConfig = config
        self._lock: threading.RLock = threading.RLock()
        self._status_callbacks: list[Callable[[dict[str, object]], None]] = []

        # Initialize connection objects
        self.obs_ws: obswebsocket.obsws | None = None
        self.ftc_websocket: WebSocketClientProtocol | None = None
        self.current_field: int | None = None
        self.running: bool = False

        # Video processing state
        self.video_splitter: LocalVideoProcessor | None = None
        self.current_match_clips: list[Path] = []

        # Web server
        self.web_server: ThreadingHTTPServer | None = None
        self.web_thread: threading.Thread | None = None

        # mDNS/Zeroconf service
        self.zeroconf: Zeroconf | None = None
        self.service_info: ServiceInfo | None = None

        # Local video processing
        self.local_video_processor: LocalVideoProcessor | None = None
        self.obs_recording_path: str | None = None

        # WebSocket broadcaster
        self.ws_broadcaster: Any = None  # WebSocketBroadcaster, set in start_web_server

        # Sync state
        self.sync_running: bool = False
        self._sync_thread: threading.Thread | None = None

        # WebSocket tunnel
        self.tunnel_client: WSTunnelClient | None = None

        # Create output directory with event code subfolder
        self.clips_dir: Path = Path(self.config.output_dir).absolute() / self.config.event_code
        self.clips_dir.mkdir(exist_ok=True, parents=True)

    def register_status_callback(self, callback: Callable[[dict[str, object]], None]) -> None:
        """Register a callback to be notified of status changes"""
        self._status_callbacks.append(callback)

    def _notify_status_change(self) -> None:
        """Notify all registered callbacks of a status change"""
        status = self.get_status()
        for cb in self._status_callbacks:
            try:
                cb(status)
            except Exception:
                pass

    def get_status(self) -> dict[str, object]:
        """Get current status as a dict"""
        clips_count = 0
        try:
            clips_count = len(self._scan_video_files())
        except Exception:
            pass

        recording_info = None
        try:
            if self.obs_ws:
                recording_info = self.get_obs_recording_info()
        except Exception:
            pass

        return {
            'running': self.running,
            'obs_connected': self.obs_ws is not None,
            'ftc_connected': self.ftc_websocket is not None and not self.ftc_websocket.closed,
            'current_field': self.current_field,
            'clips_count': clips_count,
            'recording_info': recording_info,
            'event_code': self.config.event_code,
            'sync_running': self.sync_running,
            'tunnel_connected': self.tunnel_client is not None and self.tunnel_client.is_connected(),
        }

    def get_config_dict(self) -> dict[str, object]:
        """Get current config as a dict"""
        d = vars(self.config).copy()
        # Ensure field_scene_mapping keys are strings for JSON
        d['field_scene_mapping'] = {str(k): v for k, v in self.config.field_scene_mapping.items()}
        return d

    def update_config(self, data: dict[str, object]) -> None:
        """Update config from a dict (only known fields)"""
        with self._lock:
            for key, value in data.items():
                if key == 'field_scene_mapping' and isinstance(value, dict):
                    self.config.field_scene_mapping = {int(k): str(v) for k, v in value.items()}
                elif hasattr(self.config, key):
                    setattr(self.config, key, value)

    def connect_to_obs(self) -> bool:
        """Connect to OBS WebSocket server"""
        try:
            self.obs_ws = obswebsocket.obsws(self.config.obs_host, self.config.obs_port, self.config.obs_password)
            self.obs_ws.connect()  # pyright: ignore[reportUnknownMemberType]
            logger.info("Connected to OBS WebSocket server")
            self._notify_status_change()
            return True
        except Exception as e:
            logger.error(f"Error connecting to OBS: {e}")
            return False

    def disconnect_from_obs(self) -> None:
        """Disconnect from OBS WebSocket server"""
        if self.obs_ws:
            try:
                self.obs_ws.disconnect()  # pyright: ignore[reportUnknownMemberType]
                self.obs_ws = None
                logger.info("Disconnected from OBS WebSocket server")
                self._notify_status_change()
            except Exception as e:
                logger.error(f"Error disconnecting from OBS: {e}")

    def configure_obs_scenes(self) -> bool:
        """Auto-configure OBS scenes and sources"""
        if not self.obs_ws:
            if not self.connect_to_obs():
                return False

        assert self.obs_ws is not None

        try:
            logger.info("Starting OBS scene configuration...")

            # Step 1: Get current scenes and sources
            logger.info("Getting current scenes...")
            scenes_response = self.obs_ws.call(obsrequests.GetSceneList())  # pyright: ignore[reportAny, reportUnknownMemberType, reportUnknownVariableType]
            existing_scenes = [scene['sceneName'] for scene in scenes_response.datain['scenes']]  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            logger.info(f"Found {len(existing_scenes)} existing scenes")  # pyright: ignore[reportUnknownArgumentType]

            # Step 2: Create field scenes FIRST
            logger.info("Creating field scenes...")
            for field_num in range(1, 4):
                scene_name = f"Field {field_num}"
                if scene_name not in existing_scenes:
                    try:
                        self.obs_ws.call(obsrequests.CreateScene(sceneName=scene_name))  # pyright: ignore[reportAny, reportUnknownMemberType]
                        logger.info(f"✓ Created scene: {scene_name}")
                    except Exception as e:
                        logger.error(f"✗ Failed to create scene {scene_name}: {e}")
                else:
                    logger.info(f"✓ Scene already exists: {scene_name}")

            # Step 3: Get existing sources to avoid duplicates
            logger.info("Checking existing sources...")
            try:
                sources_response = self.obs_ws.call(obsrequests.GetInputList())  # pyright: ignore[reportAny, reportUnknownMemberType, reportUnknownVariableType]
                existing_sources = [source['inputName'] for source in sources_response.datain['inputs']]  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
                logger.info(f"Found {len(existing_sources)} existing sources")  # pyright: ignore[reportUnknownArgumentType]
            except Exception as e:
                logger.error(f"Could not get input list: {e}")
                existing_sources = []

            # Step 4: Create or update shared overlay source
            shared_overlay_name = "FTC Scoring System Overlay"
            overlay_url = (f"http://{self.config.scoring_host}:{self.config.scoring_port}/event/{self.config.event_code}/display/"
                          f"?type=audience&bindToField=all&scoringBarLocation=bottom&allianceOrientation=standard"
                          f"&liveScores=true&mute=false&muteRandomizationResults=false&fieldStyleTimer=false"
                          f"&overlay=true&overlayColor=transparent&allianceSelectionStyle=classic&awardsStyle=overlay"
                          f"&dualDivisionRankingStyle=sideBySide&rankingsFontSize=larger&showMeetRankings=false"
                          f"&rankingsAllTeams=true")
            logger.info(f"Overlay URL: {overlay_url}")

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

            if shared_overlay_name not in existing_sources:
                logger.info("Creating shared overlay source...")

                try:
                    # Create the browser source - need to specify a scene for newer API
                    try:
                        # Use the first field scene as the target for creation
                        first_scene = f"Field 1"
                        self.obs_ws.call(obsrequests.CreateInput(  # pyright: ignore[reportUnknownMemberType, reportAny]
                            sceneName=first_scene,
                            inputName=shared_overlay_name,
                            inputKind="browser_source",
                            inputSettings=browser_settings
                        ))
                        logger.info("✓ Used CreateInput API with scene")
                    except Exception as e1:
                        logger.error(f"CreateInput failed ({e1}), trying CreateSource...")
                        # Fallback to older API method
                        self.obs_ws.call(obsrequests.CreateSource(  # pyright: ignore[reportUnknownMemberType, reportAny]
                            sourceName=shared_overlay_name,
                            sourceKind="browser_source",
                            sourceSettings=browser_settings
                        ))
                        logger.info("✓ Used CreateSource API")

                    logger.info(f"✓ Created shared overlay source: {shared_overlay_name}")

                    # Wait for source to be fully created
                    time.sleep(1.0)

                except Exception as e:
                    logger.error(f"✗ Error creating shared overlay source: {e}")
                    # Don't return False here, continue with scene setup
            else:
                # Update existing overlay source with new URL
                logger.info(f"Updating existing overlay source: {shared_overlay_name}")
                try:
                    self.obs_ws.call(obsrequests.SetInputSettings(  # pyright: ignore[reportUnknownMemberType, reportAny]
                        inputName=shared_overlay_name,
                        inputSettings={"url": overlay_url},
                        overlay=True  # Overlay mode: only update URL, keep other settings
                    ))
                    logger.info(f"✓ Updated overlay URL for existing source")
                except Exception as e:
                    logger.error(f"✗ Failed to update overlay URL: {e}")

            # Step 6: Add the shared overlay to each field scene
            logger.info("Adding overlay to scenes...")
            for field_num in range(1, 4):
                scene_name = f"Field {field_num}"

                try:
                    # Check if the source is already in the scene
                    try:
                        scene_items_response = self.obs_ws.call(obsrequests.GetSceneItemList(sceneName=scene_name))  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportAny]
                        existing_items = [item['sourceName'] for item in scene_items_response.datain['sceneItems']]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                    except Exception:
                        # Fallback for older API
                        existing_items = []

                    if shared_overlay_name not in existing_items:
                        # Skip Field 1 if we created the source there already
                        if scene_name == "Field 1" and shared_overlay_name not in existing_sources:
                            logger.info(f"✓ Overlay already in {scene_name} (created there)")
                        else:
                            try:
                                # Try newer API first
                                self.obs_ws.call(obsrequests.CreateSceneItem(  # pyright: ignore[reportUnknownMemberType, reportAny]
                                    sceneName=scene_name,
                                    sourceName=shared_overlay_name
                                ))
                                logger.info(f"✓ Added overlay to {scene_name} (CreateSceneItem)")
                            except Exception as e1:
                                logger.error(f"CreateSceneItem failed ({e1}), trying AddSceneItem...")
                                # Fallback to older API method
                                self.obs_ws.call(obsrequests.AddSceneItem(  # pyright: ignore[reportUnknownMemberType, reportAny]
                                    sceneName=scene_name,
                                    sourceName=shared_overlay_name
                                ))
                                logger.info(f"✓ Added overlay to {scene_name} (AddSceneItem)")

                    else:
                        logger.info(f"✓ Overlay already exists in {scene_name}")

                except Exception as e:
                    logger.error(f"✗ Could not add overlay to {scene_name}: {e}")

            logger.info("✅ OBS scene configuration completed successfully!")
            return True

        except Exception as e:
            logger.error(f"✗ Error configuring OBS scenes: {e}")
            return False

    def get_obs_recording_path(self) -> str | None:
        """Get current OBS recording file path via WebSocket"""
        info = self.get_obs_recording_info()
        if info and 'recording_path' in info:
            path = info['recording_path']
            return str(path) if path else None
        return None

    def get_obs_recording_info(self) -> dict[str, object] | None:
        """Get current OBS recording info (path, duration, start time) via WebSocket"""
        if not self.obs_ws:
            return None

        try:
            # Check if recording is active
            record_status = self.obs_ws.call(obsrequests.GetRecordStatus())  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportAny]
            if not record_status.datain.get('outputActive', False):  # pyright: ignore[reportUnknownMemberType]
                logger.error("OBS is not currently recording")
                return None

            # Get recording duration (in milliseconds)
            output_duration_ms: int = int(record_status.datain.get('outputDuration', 0))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            output_timecode: str = str(record_status.datain.get('outputTimecode', '00:00:00.000'))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]

            # Calculate recording start time
            current_time = datetime.now()
            recording_duration_seconds: float = float(output_duration_ms) / 1000.0
            recording_start_time = current_time - timedelta(seconds=recording_duration_seconds)

            # Try to get recording output settings
            recording_path = None
            try:
                # Try advanced file output first
                output_settings = self.obs_ws.call(obsrequests.GetOutputSettings(outputName="adv_file_output"))  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType, reportAny]
                recording_path = output_settings.datain['outputSettings'].get('path')  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            except Exception:
                # Fallback: try simple file output
                try:
                    output_settings = self.obs_ws.call(obsrequests.GetOutputSettings(outputName="simple_file_output"))  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportAny]
                    recording_path = output_settings.datain['outputSettings'].get('path')  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                except Exception:
                    # Final fallback: use record status filename if available
                    recording_path = record_status.datain.get('outputPath')  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

            if recording_path:
                logger.info(f"Found OBS recording: {recording_path} (started at {recording_start_time.strftime('%H:%M:%S')}, duration: {output_timecode})")
                return {
                    'recording_path': recording_path,
                    'recording_start_time': recording_start_time,
                    'recording_duration_ms': output_duration_ms,
                    'recording_timecode': output_timecode
                }
            else:
                logger.error("Could not determine OBS recording path")
                return None

        except Exception as e:
            logger.error(f"Error getting OBS recording info: {e}")
            return None

    def setup_local_video_processor(self) -> bool:
        """Initialize local video processor with OBS recording path"""
        try:
            logger.info("🔍 Setting up local video processor...")

            # Get current OBS recording path
            recording_path = self.get_obs_recording_path()

            if recording_path:
                # Create local video processor
                config = {
                    'output_dir': self.clips_dir,  # clips_dir is already absolute
                    'pre_match_buffer_seconds': self.config.pre_match_buffer_seconds,
                    'post_match_buffer_seconds': self.config.post_match_buffer_seconds,
                    'match_duration_seconds': self.config.match_duration_seconds
                }

                self.local_video_processor = LocalVideoProcessor(config)
                self.local_video_processor.set_recording_path(recording_path)
                self.local_video_processor.start_monitoring()

                self.obs_recording_path = recording_path
                logger.info(f"✅ Local video processor ready: {recording_path}")
                return True
            else:
                logger.error("❌ Could not setup local video processor - no recording path")
                logger.error("   Make sure OBS is recording before starting MatchBox")
                return False

        except Exception as e:
            logger.error(f"❌ Error setting up local video processor: {e}")
            import traceback
            logger.error(f"❌ Full error traceback: {traceback.format_exc()}")
            return False

    def switch_scene(self, field_number: int) -> bool:
        """Switch OBS scene based on field number"""
        if field_number not in self.config.field_scene_mapping:
            logger.error(f"No scene mapping found for Field {field_number}")
            return False

        if not self.obs_ws:
            logger.error("Error switching scene: OBS WebSocket not connected")
            return False

        scene_name = self.config.field_scene_mapping[field_number]
        try:
            response = self.obs_ws.call(obsrequests.SetCurrentProgramScene(sceneName=scene_name))  # pyright: ignore[reportUnknownMemberType, reportAny, reportUnknownVariableType]
            if response.status:  # pyright: ignore[reportUnknownMemberType]
                logger.info(f"Switched to scene: {scene_name} for Field {field_number}")
                self._notify_status_change()
                return True
            else:
                logger.error(f"Failed to switch scene: {response.error}")  # pyright: ignore[reportUnknownMemberType]
                return False
        except Exception as e:
            logger.error(f"Error switching scene: {e}")
            return False

    def start_web_server(self) -> bool:
        """Start local web server for match clips and admin API"""
        try:
            # Create clips directory if it doesn't exist
            self.clips_dir.mkdir(exist_ok=True, parents=True)
            clips_dir_str = str(self.clips_dir.absolute())

            # Create initial index.html with existing files scan
            try:
                self.create_initial_web_interface()
                logger.info(f"Created index.html with existing files scan")
            except Exception as e:
                logger.error(f"Error creating initial index.html: {e}")

            # Start WebSocket server on port+1
            from web_api.websocket_server import WebSocketBroadcaster
            self.ws_broadcaster = WebSocketBroadcaster(self.config.web_port + 1, self)
            self.ws_broadcaster.start()

            # Register status callback to broadcast via WebSocket
            self.register_status_callback(
                lambda status: self.ws_broadcaster.broadcast_status(status)
            )

            # Hook log broadcasting into the global GUILogHandler
            gui_handler.ws_broadcaster = self.ws_broadcaster

            def run_server() -> None:
                try:
                    from web_api.handler import make_admin_handler

                    logger.info(f"Starting web server on port {self.config.web_port}")
                    logger.info(f"Serving directory: {clips_dir_str}")
                    logger.info(f"Access match clips at http://localhost:{self.config.web_port}")
                    logger.info(f"Admin UI at http://localhost:{self.config.web_port}/admin")

                    # Create handler class with API and admin UI support
                    HandlerClass = make_admin_handler(clips_dir_str, self)
                    # Use ThreadingHTTPServer for better performance and bind to all interfaces
                    self.web_server = ThreadingHTTPServer(('0.0.0.0', self.config.web_port), HandlerClass)
                    # Prevent the server from hanging on to connections
                    self.web_server.allow_reuse_address = True
                    self.web_server.timeout = 30  # 30 second timeout for requests
                    self.web_server.serve_forever()
                except OSError as e:
                    if "Address already in use" in str(e):
                        logger.error(f"Web server port {self.config.web_port} is already in use")
                    else:
                        logger.error(f"Web server OS error: {e}")
                except Exception as e:
                    logger.error(f"Web server error: {e}")

            self.web_thread = threading.Thread(target=run_server, daemon=True)
            self.web_thread.start()

            # Register mDNS service for local network discovery
            _ = self.register_mdns_service()

            return True

        except Exception as e:
            logger.error(f"Error starting web server: {e}")
            return False

    def stop_web_server(self) -> None:
        """Stop local web server"""
        if self.web_server:
            try:
                self.web_server.shutdown()
                self.web_server.server_close()
                logger.info("Web server stopped")
            except Exception as e:
                logger.error(f"Error stopping web server: {e}")

    def register_mdns_service(self) -> bool:
        """Register mDNS service for local network discovery"""

        # Run mDNS registration in a separate thread to avoid event loop conflicts
        def _register_in_thread() -> None:
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
                        local_ip: str = cast(str, s.getsockname()[0])
                except:
                    pass  # Fallback to hostname resolution

                logger.info(f"📡 mDNS: Using IP {local_ip}")

                # Create Zeroconf instance in this thread
                self.zeroconf = Zeroconf()

                # Parse mDNS name to get hostname
                mdns_name = self.config.mdns_name
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
                    port=self.config.web_port,
                    properties={
                        'path': '/',
                        'description': f'MatchBox - {self.config.event_code}',
                        'event': self.config.event_code,
                        'service': 'matchbox'
                    },
                    server=f"{hostname_part}.local."
                )

                self.zeroconf.register_service(self.service_info)
                logger.info(f"✅ mDNS service registered: http://{mdns_name}:{self.config.web_port}")
                logger.info(f"📡 Access from network: {local_ip}:{self.config.web_port}")

            except Exception as e:
                import traceback
                logger.error(f"❌ Failed to register mDNS service: {type(e).__name__}: {e}")
                logger.error(f"❌ Full traceback: {traceback.format_exc()}")

        # Start registration in background thread
        mdns_thread = threading.Thread(target=_register_in_thread, daemon=True)
        mdns_thread.start()
        return True

    def unregister_mdns_service(self) -> None:
        """Unregister mDNS service"""
        try:
            if self.service_info and self.zeroconf:
                self.zeroconf.unregister_service(self.service_info)
                logger.info("mDNS service unregistered")

            if self.zeroconf:
                self.zeroconf.close()
                self.zeroconf = None
                self.service_info = None

        except Exception as e:
            logger.error(f"Error unregistering mDNS service: {e}")

    def ensure_web_server(self) -> None:
        """Start the web server if it's not already running"""
        if self.web_server is None:
            _ = self.start_web_server()

    async def monitor_ftc_websocket(self) -> None:
        """Monitor FTC scoring system WebSocket for match events"""
        if not self.connect_to_obs():
            logger.error("Failed to connect to OBS. Exiting.")
            return

        # Ensure web server is running (may already be started)
        self.ensure_web_server()

        # Setup local video processing if OBS is recording
        _ = self.setup_local_video_processor()

        ftc_ws_url = f"ws://{self.config.scoring_host}:{self.config.scoring_port}/stream/display/command/?code={self.config.event_code}"
        logger.info(f"Connecting to FTC WebSocket: {ftc_ws_url}")
        logger.info(f"Field-scene mapping: {json.dumps(self.config.field_scene_mapping, indent=2)}")

        self.running = True
        self._notify_status_change()
        try:
            async with websockets.client.connect(ftc_ws_url) as websocket:
                self.ftc_websocket = websocket
                logger.info("Connected to FTC scoring system WebSocket")
                self._notify_status_change()

                # Drain initial backlog of old events for 5 seconds
                logger.info("⏳ Draining initial backlog of old events...")
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
                    logger.info(f"🗑️ Discarded {backlog_count} old events from backlog")
                logger.info("✅ Ready to process new FTC events")

                while self.running:
                    message = ""
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                        data: dict[str, object] = cast(dict[str, object], json.loads(message))

                        if data.get("type") == "SHOW_PREVIEW" or data.get("type") == "SHOW_MATCH":
                            # Extract field number
                            field_number: int | None = cast(int | None, data.get("field"))
                            if field_number is None and "params" in data:
                                field_number = cast(int | None, cast(dict[str, object], data["params"]).get("field"))

                            # FIXME: this feels unnecessary, just check the actual output structure
                            if field_number is not None and field_number != self.current_field:
                                logger.info(f"Field change detected: {self.current_field} -> {field_number}")
                                if self.switch_scene(field_number):
                                    self.current_field = field_number

                        elif data.get("type") == "START_MATCH":
                            # Match started - schedule delayed clip generation
                            match_info: dict[str, object] = cast(dict[str, object], data.get("params", {}))

                            # Strip whitespace from matchName (scoring system sometimes includes leading space, notably in playoffs matches)
                            if 'matchName' in match_info and isinstance(match_info['matchName'], str):
                                match_info['matchName'] = match_info['matchName'].strip()
                            logger.info(f"🎬 Match started: {match_info}")

                            # Add timestamp for accurate clip timing
                            match_info['start_timestamp'] = time.time()

                            # Schedule clip generation to start after full match duration
                            if self.local_video_processor:
                                logger.info("🎬 Scheduling delayed clip generation...")
                                _ = asyncio.create_task(self.generate_match_clip_delayed(match_info))
                            else:
                                logger.error("❌ Local video processor not available for clipping")

                    except asyncio.TimeoutError:
                        continue
                    except json.JSONDecodeError as e:
                        if message != "pong":
                            logger.error(f"Error decoding message: {e}")
                    except websockets.exceptions.ConnectionClosed:
                        if self.running:
                            raise
                    except Exception as e:
                        if self.running:
                            logger.error(f"Error processing message: {e}")

        except asyncio.CancelledError:
            pass  # Normal cancellation from stop_matchbox
        except websockets.exceptions.ConnectionClosed:
            logger.error("Connection to FTC scoring system closed. Check server and event code.")
        except Exception as e:
            if self.running:
                logger.error(f"WebSocket error: {e}")
        finally:
            await self.stop_monitoring()

    async def generate_match_clip_delayed(self, match_info: dict[str, object]) -> None:
        """Generate a match clip after waiting for the full match duration"""
        # Calculate total time to wait: match duration + post-match buffer + extra safety margin
        match_duration: float = self.config.match_duration_seconds
        post_match_buffer: float = self.config.post_match_buffer_seconds
        safety_margin: float = 8.0  # Extra time for transitions and safety

        total_wait_time: float = match_duration + post_match_buffer + safety_margin

        logger.info(f"🎬 Waiting {total_wait_time} seconds for match to complete before generating clip...")
        await asyncio.sleep(total_wait_time)

        logger.info("🎬 Match duration complete - starting clip generation...")
        await self.generate_match_clip(match_info)

    async def generate_match_clip(self, match_info: dict[str, object]) -> None:
        """Generate a match clip using the local video processor"""
        try:
            logger.info(f"🎬 Generating clip for match: {match_info}")

            # Double-check processor is available
            if not self.local_video_processor:
                logger.error("❌ Local video processor is None!")
                return

            # Fetch fresh recording info from OBS (path + start time)
            # This handles cases where recording was restarted between matches
            logger.info("🎬 Fetching current OBS recording info...")
            obs_info = self.get_obs_recording_info()

            if not obs_info:
                logger.error("❌ Could not get OBS recording info - cannot create clip")
                return

            # Add OBS recording info to match_info for the video processor
            match_info_with_obs = dict(match_info)
            match_info_with_obs['obs_recording_path'] = obs_info['recording_path']
            match_info_with_obs['obs_recording_start_time'] = obs_info['recording_start_time']

            # Extract clip using local video processor
            logger.info("🎬 Calling local_video_processor.extract_clip()...")
            clip_path = await self.local_video_processor.extract_clip(match_info_with_obs)
            logger.info(f"🎬 extract_clip() returned: {clip_path}")

            if clip_path:
                logger.info(f"✅ Match clip created: {clip_path}")
                self.current_match_clips.append(clip_path)

                # Update web interface by refreshing index.html with latest clips
                await self.update_web_interface_clips()

            else:
                logger.error(f"❌ Failed to create match clip - extract_clip returned None")

        except Exception as e:
            logger.error(f"❌ Error generating match clip: {e}")
            import traceback
            logger.error(f"❌ Full traceback: {traceback.format_exc()}")

    def _scan_video_files(self) -> list[Path]:
        """Scan for video files in clips directory"""
        video_extensions = ('.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm')
        video_files: list[Path] = []

        try:
            for file_path in self.clips_dir.iterdir():
                if file_path.is_file() and file_path.suffix.lower() in video_extensions:
                    video_files.append(file_path)
        except Exception as e:
            print(f"Error scanning for video files: {e}")

        # Sort files by modification time (newest first)
        video_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return video_files

    def _generate_html_content(self, video_files: list[Path]) -> str:
        """Generate HTML content for the web interface"""

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

        # Attempt to load version
        try:
            from _version import __version__  # pyright: ignore[reportMissingImports, reportUnknownVariableType]
            version = __version__  # pyright: ignore[reportUnknownVariableType]
        except ModuleNotFoundError:
            version: str = "dev"

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MatchBox&trade; for FIRST&reg; Tech Challenge - Match Clips</title>
    <link rel="icon" href="/favicon.ico">
    <style>
        :root    {{ color-scheme: dark; }}
        body     {{ font-family: sans-serif; margin: 0; background-color: #272727; color: #ddd; }}
        .header  {{ display: inline-flex; gap: 24px; padding: 24px; width: calc(100% - 48px); background-color: #363636; }}
        h1       {{ align-content: end; color: white; margin-top: auto; margin-bottom: auto; }}
        .content {{ margin: 24px; }}
        .logo    {{ height: 1.2em; width: auto; vertical-align: middle; }}
        .status  {{ padding: 1px 20px; background: #1e1e1e; border-radius: 5px; margin: 0 0 20px 0; }}
        .footer  {{ margin-top: 40px; color: #999; font-size: 0.9em; }}
        li       {{ margin: 5px 0; }}
        small    {{ color: #999; margin-left: 10px; }}
        @media (max-width: 520px) {{
            h1       {{ font-size: 18px; }}
            .content {{ margin: 8px; }}
            ul       {{ padding-left: 20px; }}
        }}
    </style>
    <meta http-equiv="refresh" content="30">
</head>
<body>
    <div class="header">
        <svg
            height="80"
            viewBox="0 0 112 80"
            width="112"
            version="1.1"
            id="svg41"
            xmlns:xlink="http://www.w3.org/1999/xlink"
            xmlns="http://www.w3.org/2000/svg"
            xmlns:svg="http://www.w3.org/2000/svg">
            <defs
                id="defs41" />
            <sodipodi:namedview
                id="namedview41"
                pagecolor="#ffffff"
                bordercolor="#000000"
                borderopacity="0.25"
                inkscape:showpageshadow="2"
                inkscape:pageopacity="0.0"
                inkscape:pagecheckerboard="0"
                inkscape:deskcolor="#d1d1d1"
                inkscape:zoom="3.9515251"
                inkscape:cx="65.544314"
                inkscape:cy="62.001378"
                inkscape:window-width="1728"
                inkscape:window-height="1051"
                inkscape:window-x="0"
                inkscape:window-y="38"
                inkscape:window-maximized="1"
                inkscape:current-layer="svg41" />
            <linearGradient
                id="a"
                gradientUnits="userSpaceOnUse">
                <stop
                offset="0"
                stop-color="#77767b"
                id="stop1" />
                <stop
                offset="0.0357143"
                stop-color="#c0bfbc"
                id="stop2" />
                <stop
                offset="0.0713653"
                stop-color="#9a9996"
                id="stop3" />
                <stop
                offset="0.928571"
                stop-color="#9a9996"
                id="stop4" />
                <stop
                offset="0.964286"
                stop-color="#c0bfbc"
                id="stop5" />
                <stop
                offset="1"
                stop-color="#77767b"
                id="stop6" />
            </linearGradient>
            <linearGradient
                id="b"
                x1="8.0000153"
                x2="120.00002"
                xlink:href="#a"
                y1="119.99998"
                y2="119.99998"
                gradientTransform="translate(-8,-36)" />
            <linearGradient
                id="c"
                gradientUnits="userSpaceOnUse">
                <stop
                offset="0"
                stop-color="#dd0000"
                id="stop7" />
                <stop
                offset="0.21275"
                stop-color="#a30000"
                id="stop8" />
                <stop
                offset="0.832119"
                stop-color="#a30000"
                id="stop9" />
                <stop
                offset="1"
                stop-color="#5c0000"
                id="stop10" />
            </linearGradient>
            <linearGradient
                id="d"
                x1="70"
                x2="70"
                xlink:href="#c"
                y1="46.025864"
                y2="53.95137"
                gradientTransform="translate(-8,-36)" />
            <linearGradient
                id="e"
                x1="72"
                x2="72"
                xlink:href="#c"
                y1="56.025864"
                y2="63.95137"
                gradientTransform="translate(-8,-36)" />
            <linearGradient
                id="f"
                x1="68"
                x2="68"
                xlink:href="#c"
                y1="66.025864"
                y2="73.95137"
                gradientTransform="translate(-8,-36)" />
            <linearGradient
                id="g"
                x1="70"
                x2="70"
                xlink:href="#c"
                y1="76.025864"
                y2="83.95137"
                gradientTransform="translate(-8,-36)" />
            <linearGradient
                id="h"
                x1="66"
                x2="66"
                xlink:href="#c"
                y1="86.025864"
                y2="93.95137"
                gradientTransform="translate(-8,-36)" />
            <linearGradient
                id="i"
                x1="72"
                x2="72"
                xlink:href="#c"
                y1="96.025864"
                y2="103.95137"
                gradientTransform="translate(-8,-36)" />
            <linearGradient
                id="j"
                x1="8.0000153"
                x2="100.00001"
                xlink:href="#a"
                y1="115.99998"
                y2="115.99998"
                gradientTransform="translate(-8,-36)" />
            <path
                d="m 8,4 h 96 c 4.41797,0 8,3.582031 8,8 v 56 c 0,4.417969 -3.58203,8 -8,8 H 8 C 3.582031,76 0,72.417969 0,68 V 12 C 0,7.582031 3.582031,4 8,4 Z m 0,0"
                fill="url(#b)"
                id="path10"
                style="fill:url(#b)" />
            <path
                d="m 8,8 h 96 c 4.41797,0 8,3.582031 8,8 v 48 c 0,4.417969 -3.58203,8 -8,8 H 8 C 3.582031,72 0,68.417969 0,64 V 16 C 0,11.582031 3.582031,8 8,8 Z m 0,0"
                fill="#cccccc"
                id="path11" />
            <path
                d="m 20,10 h 84 c 2.21094,0 4,1.789062 4,4 0,2.210938 -1.78906,4 -4,4 H 20 c -2.210938,0 -4,-1.789062 -4,-4 0,-2.210938 1.789062,-4 4,-4 z m 0,0"
                fill="url(#d)"
                id="path12"
                style="fill:url(#d)" />
            <path
                d="M 16.335938,10 H 99.66406 C 99.85156,10 100,11.789062 100,14 c 0,2.210938 -0.14844,4 -0.33594,4 H 16.335938 C 16.148438,18 16,16.210938 16,14 c 0,-2.210938 0.148438,-4 0.335938,-4 z m 0,0"
                fill="#f8f8ab"
                id="path13" />
            <path
                d="m 22,20 h 84 c 2.21094,0 4,1.789062 4,4 0,2.210938 -1.78906,4 -4,4 H 22 c -2.210938,0 -4,-1.789062 -4,-4 0,-2.210938 1.789062,-4 4,-4 z m 0,0"
                fill="url(#e)"
                id="path14"
                style="fill:url(#e)" />
            <path
                d="m 18.335938,20 h 83.328122 c 0.1875,0 0.33594,1.789062 0.33594,4 0,2.210938 -0.14844,4 -0.33594,4 H 18.335938 C 18.148438,28 18,26.210938 18,24 c 0,-2.210938 0.148438,-4 0.335938,-4 z m 0,0"
                fill="#f8f8ab"
                id="path15" />
            <path
                d="m 18,30 h 84 c 2.21094,0 4,1.789062 4,4 0,2.210938 -1.78906,4 -4,4 H 18 c -2.210938,0 -4,-1.789062 -4,-4 0,-2.210938 1.789062,-4 4,-4 z m 0,0"
                fill="url(#f)"
                id="path16"
                style="fill:url(#f)" />
            <path
                d="M 14.335938,30 H 97.66406 C 97.85156,30 98,31.789062 98,34 c 0,2.210938 -0.14844,4 -0.33594,4 H 14.335938 C 14.148438,38 14,36.210938 14,34 c 0,-2.210938 0.148438,-4 0.335938,-4 z m 0,0"
                fill="#f8f8ab"
                id="path17" />
            <path
                d="m 20,40 h 84 c 2.21094,0 4,1.789062 4,4 0,2.210938 -1.78906,4 -4,4 H 20 c -2.210938,0 -4,-1.789062 -4,-4 0,-2.210938 1.789062,-4 4,-4 z m 0,0"
                fill="url(#g)"
                id="path18"
                style="fill:url(#g)" />
            <path
                d="M 16.335938,40 H 99.66406 C 99.85156,40 100,41.789062 100,44 c 0,2.210938 -0.14844,4 -0.33594,4 H 16.335938 C 16.148438,48 16,46.210938 16,44 c 0,-2.210938 0.148438,-4 0.335938,-4 z m 0,0"
                fill="#f8f8ab"
                id="path19" />
            <path
                d="m 16,50 h 84 c 2.21094,0 4,1.789062 4,4 0,2.210938 -1.78906,4 -4,4 H 16 c -2.210938,0 -4,-1.789062 -4,-4 0,-2.210938 1.789062,-4 4,-4 z m 0,0"
                fill="url(#h)"
                id="path20"
                style="fill:url(#h)" />
            <path
                d="M 12.335938,50 H 95.66406 C 95.85156,50 96,51.789062 96,54 c 0,2.210938 -0.14844,4 -0.33594,4 H 12.335938 C 12.148438,58 12,56.210938 12,54 c 0,-2.210938 0.148438,-4 0.335938,-4 z m 0,0"
                fill="#f8f8ab"
                id="path21" />
            <path
                d="m 22,60 h 84 c 2.21094,0 4,1.789062 4,4 0,2.210938 -1.78906,4 -4,4 H 22 c -2.210938,0 -4,-1.789062 -4,-4 0,-2.210938 1.789062,-4 4,-4 z m 0,0"
                fill="url(#i)"
                id="path22"
                style="fill:url(#i)" />
            <path
                d="m 18.335938,60 h 83.328122 c 0.1875,0 0.33594,1.789062 0.33594,4 0,2.210938 -0.14844,4 -0.33594,4 H 18.335938 C 18.148438,68 18,66.210938 18,64 c 0,-2.210938 0.148438,-4 0.335938,-4 z m 0,0"
                fill="#f8f8ab"
                id="path23" />
            <path
                d="m 8,20 h 76 c 4.417969,0 8,3.582031 8,8 v 44 c 0,4.417969 -3.582031,8 -8,8 H 8 C 3.582031,80 0,76.417969 0,72 V 28 c 0,-4.417969 3.582031,-8 8,-8 z m 0,0"
                fill="url(#j)"
                id="path24"
                style="fill:url(#j)" />
            <path
                d="m 8,0 h 76 c 4.417969,0 8,3.582031 8,8 v 60 c 0,4.417969 -3.582031,8 -8,8 H 8 C 3.582031,76 0,72.417969 0,68 V 8 C 0,3.582031 3.582031,0 8,0 Z m 0,0"
                fill="#ffffff"
                id="path25" />
            <path
                d="m 44.484375,29.300781 c 1.402344,0 2.765625,0.128907 4.066406,0.367188 l 3.097657,-3.226563 c -2.164063,-0.75 -4.597657,-1.171875 -7.164063,-1.171875 -4.371094,0 -8.335937,1.21875 -11.277344,3.203125 l 2.699219,2.554688 c 2.496094,-1.09375 5.429688,-1.726563 8.578125,-1.726563"
                fill="#98999b"
                id="path26" />
            <path
                d="m 44.484375,25.269531 c 2.566406,0 5,0.421875 7.164063,1.171875 L 55.015625,22.9375 C 51.875,21.699219 48.289062,20.996094 44.484375,20.996094 c -5.675781,0 -10.855469,1.558594 -14.808594,4.128906 l 3.53125,3.347656 c 2.941407,-1.984375 6.90625,-3.203125 11.277344,-3.203125"
                fill="#ffffff"
                id="path27" />
            <path
                d="m 28.320312,38.984375 c 0.28125,-1.15625 0.804688,-2.253906 1.527344,-3.269531 L 28.488281,34.34375 c -0.265625,0.84375 -0.417969,1.722656 -0.417969,2.625 0,0.6875 0.08594,1.359375 0.25,2.015625"
                fill="#98999b"
                id="path28" />
            <path
                d="m 18.296875,46.566406 -0.570313,2.953125 5.667969,-2.441406 c -0.359375,-0.75 -0.429687,-1.730469 -0.636719,-2.527344 z m 0,0"
                fill="#98999b"
                id="path29" />
            <path
                d="m 14.3125,14.46875 -8.199219,44.898438 3.875,0.3125 8.121094,-45.550782 z m 0,0"
                fill="#98999b"
                id="path30" />
            <path
                d="M 41.882812,40.304688 V 36.671875 L 35.925781,31.027344 33.230469,28.472656 29.699219,25.125 18.09375,14.128906 9.972656,59.679688 27.15625,52.125 c -1.710938,-1.507812 -2.921875,-3.121094 -3.824219,-5.019531 l -5.59375,2.398437 0.5625,-2.9375 3.625,-18.890625 0.300781,-1.585937 2.996094,3.015625 3.847656,3.875 1.652344,1.667969 6.800782,5.855468 -6.597657,3.023438 c 1.710938,1.710937 3.742188,3.21875 6.476563,4.070312 l 8.234375,-3.695312 z m 0,0"
                fill="#ed1c24"
                id="path31" />
            <g
                fill="#98999b"
                id="g35"
                transform="translate(-8,-36)">
                <path
                d="m 68.648438,74.984375 c 0.160156,-0.65625 0.246093,-1.328125 0.246093,-2.015625 0,-2.027344 -0.742187,-3.941406 -2.015625,-5.605469 l -1.957031,2.023438 c 1.878906,1.5625 3.210937,3.488281 3.726563,5.597656"
                id="path32" />
                <path
                d="M 72.320312,94.199219 67.691406,89.75 c -0.742187,0.503906 -1.535156,0.976562 -2.363281,1.40625 l 6.992187,6.714844 21.566407,-22.464844 -0.02344,-3.636719 z m 0,0"
                id="path33" />
                <path
                d="m 56.605469,79.113281 -3.8125,-3.664062 -2.921875,-2.785157 -0.0078,3.640626 4.101563,3.941406 3.925781,3.769531 c 0.996094,-0.25 1.949219,-0.5625 2.84375,-0.9375 z m 0,0"
                id="path34" />
                <path
                d="m 71.601562,58.800781 -3.070312,3.195313 -3.386719,3.53125 -2.308593,2.402344 -4.375,4.554687 1.832031,1.757813 4.65625,-4.84375 1.945312,-2.027344 3.566407,-3.714844 1.140624,-1.1875 11.859376,11.367188 1.835937,-1.902344 z m 0,0"
                id="path35" />
            </g>
            <path
                d="m 62.460938,27.65625 -3.566407,3.714844 c 1.277344,1.660156 2,3.570312 2,5.597656 0,0.6875 -0.08594,1.359375 -0.246093,2.015625 -0.417969,1.707031 -1.351563,3.285156 -2.6875,4.652344 -1.363282,1.402343 -3.148438,2.578125 -5.226563,3.441406 -0.894531,0.375 -1.847656,0.6875 -2.84375,0.9375 -1.691406,0.417969 -3.511719,0.648437 -5.40625,0.648437 -2.335937,0 -4.550781,-0.347656 -6.558594,-0.972656 -2.738281,-0.851562 -5.085937,-2.21875 -6.800781,-3.933594 -0.625,-0.625 -1.167969,-1.296874 -1.613281,-2.003906 -0.550781,-0.871094 -0.953125,-1.800781 -1.191407,-2.769531 -0.164062,-0.65625 -0.25,-1.328125 -0.25,-2.015625 0,-0.902344 0.152344,-1.78125 0.417969,-2.625 l -4.09375,-4.144531 c -1.5,2.222656 -2.347656,4.730469 -2.347656,7.382812 0,0.683594 0.06641,1.355469 0.171875,2.015625 0.261719,1.605469 0.839844,3.136719 1.675781,4.566406 0.417969,0.714844 0.90625,1.40625 1.457031,2.070313 1.441407,1.738281 3.296876,3.261719 5.476563,4.5 3.777344,2.152344 8.511719,3.433594 13.65625,3.433594 3.597656,0 6.992187,-0.625 10.003906,-1.738281 0.917969,-0.335938 1.792969,-0.71875 2.632813,-1.144532 1.941406,-0.980468 3.664062,-2.183594 5.101562,-3.554687 2.414063,-2.304688 4.027344,-5.09375 4.523438,-8.132813 0.109375,-0.660156 0.171875,-1.332031 0.171875,-2.015625 0,-3.722656 -1.65625,-7.15625 -4.457031,-9.925781"
                fill="#ffffff"
                id="path36" />
            <path
                d="m 48.585938,43.140625 4.117187,3.953125 c 2.074219,-0.863281 3.871094,-2.027344 5.238281,-3.429688 l -5.640625,-5.417968 -1.835937,-1.757813 4.351562,-4.53125 2.308594,-2.40625 3.386719,-3.527343 3.070312,-3.195313 13.660157,13.113281 -1.832032,1.914063 -11.191406,11.652343 c -1.179688,1.613282 -2.707031,3.046876 -4.515625,4.285157 l 4.597656,4.4375 21.566407,-22.460938 -22.460938,-21.566406 -8.410156,8.761719 -3.367188,3.503906 -3.097656,3.226562 -6.660156,6.96875 z m 0,0"
                fill="#1c63b7"
                id="path37" />
            <path
                d="m 29.605469,41.867188 c 0,0 0.105469,0.175781 0.121093,0.195312 0.328126,0.523438 0.78125,1.003906 1.207032,1.480469 L 37.644531,40.445312 22.226562,26.085938 21.804688,28.308594 33.902344,40.28125 Z m 0,0"
                fill="#98999b"
                id="path38" />
            <path
                d="m 66.917969,37.582031 c 0,0.683594 -0.0625,1.355469 -0.171875,2.015625 -0.496094,3.039063 -2.109375,5.828125 -4.523438,8.132813 -1.4375,1.371093 -3.160156,2.574219 -5.101562,3.554687 -0.839844,0.425782 -1.714844,0.808594 -2.632813,1.144532 -3.011719,1.113281 -6.40625,1.738281 -10.003906,1.738281 -5.144531,0 -9.878906,-1.28125 -13.65625,-3.433594 -2.179687,-1.238281 -4.035156,-2.761719 -5.476563,-4.5 C 24.800781,45.570312 24.3125,44.878906 23.894531,44.164062 23.058594,42.734375 22.480469,41.203125 22.21875,39.597656 22.113281,38.9375 22.046875,38.265625 22.046875,37.582031 c 0,-0.214843 0.01953,-0.425781 0.02734,-0.636719 0,-0.01172 -0.0039,-0.03125 -0.0078,-0.03906 -0.109375,0.660156 -0.01953,4.027344 -0.01953,4.707031 0,1.082031 0.144531,2.140625 0.414063,3.164063 0.207031,0.800781 0.496093,1.574218 0.851562,2.328125 0.902344,1.898437 2.253906,3.640625 3.964844,5.152343 4.117187,3.628907 10.296875,5.941407 17.207031,5.941407 4.792969,0 9.234375,-1.113281 12.878906,-3.007813 0.828125,-0.429687 1.617188,-0.902344 2.355469,-1.40625 1.808594,-1.238281 3.339844,-2.691406 4.519531,-4.304687 1.707031,-2.339844 2.679688,-5.019531 2.679688,-7.867188 0,-0.597656 0.05078,-3.445312 -0.0078,-4.574219 -0.0078,-0.15625 0.0078,0.347657 0.0078,0.542969"
                fill="#98999b"
                id="path39" />
            <path
                d="m 66.839844,60.585938 h -0.425782 v -0.152344 h 1.035157 v 0.152344 h -0.425781 v 1.25 h -0.183594 z m 0,0"
                fill="#98999b"
                id="path40" />
            <path
                d="m 68.75,61.21875 c -0.01172,-0.195312 -0.02344,-0.429688 -0.01953,-0.605469 h -0.0078 c -0.04687,0.164063 -0.105468,0.339844 -0.175781,0.53125 l -0.25,0.683594 H 68.16017 l -0.226562,-0.667969 c -0.06641,-0.199218 -0.125,-0.378906 -0.164063,-0.546875 h -0.0039 c -0.0039,0.175781 -0.01563,0.410157 -0.02734,0.621094 l -0.03516,0.601563 H 67.53125 l 0.09766,-1.402344 h 0.230469 l 0.238281,0.675781 c 0.05859,0.175781 0.105469,0.328125 0.140625,0.472656 h 0.0078 c 0.03516,-0.140625 0.08594,-0.292969 0.148437,-0.472656 l 0.25,-0.675781 H 68.875 l 0.08594,1.402344 h -0.175782 z m 0,0"
                fill="#98999b"
                id="path41" />
            </svg>
        <h1>MatchBox&trade; for <em>FIRST&reg;</em> Tech Challenge <span style="font-size: 60%; font-weight: normal; margin-top: 0.55em">v{version}</span></h1>
    </div>
    <div class="content">
        <div class="status">
            <h3>Match Clips Server</h3>
            <p><strong>Event Code:</strong> {self.config.event_code}</p>
            <p><strong>Total Clips:</strong> {len(video_files)}</p>
        </div>

        <h3>&#x1F4C1; Available Match Clips</h3>
        {file_list_html}

        <div class="footer">
            <p>This page automatically refreshes every 30 seconds to show new clips.</p>
            <p><em>FIRST&reg;, FIRST&reg; Robotics Competition, and FIRST&reg; Tech Challenge, are registered trademarks of FIRST&reg; (<a href="https://www.firstinspires.org">www.firstinspires.org</a>) which is not overseeing, involved with, or responsible for this activity, product, or service.</em></p>
        </div>
    </div>
</body>
</html>"""

    def create_initial_web_interface(self) -> None:
        """Create initial web interface with existing files (sync version)"""
        video_files = self._scan_video_files()
        html_content = self._generate_html_content(video_files)

        index_path = self.clips_dir / "index.html"
        with open(index_path, 'w', encoding='utf-8') as f:
            _ = f.write(html_content)

    async def update_web_interface_clips(self) -> None:
        """Update web interface to show available clips"""
        try:
            video_files = self._scan_video_files()
            html_content = self._generate_html_content(video_files)

            index_path = self.clips_dir / "index.html"
            with open(index_path, 'w', encoding='utf-8') as f:
                _ = f.write(html_content)

        except Exception as e:
            logger.error(f"Error updating web interface: {e}")

    def start_sync(self) -> bool:
        """Start the rsync background sync loop. Returns False if validation fails."""
        if not self.config.rsync_host:
            logger.error("Sync: rsync host is required")
            return False
        if not self.config.rsync_module:
            logger.error("Sync: rsync module is required")
            return False
        if self.sync_running:
            logger.warning("Sync: Already running")
            return False

        self.sync_running = True
        self._sync_thread = threading.Thread(target=self._run_sync_loop, daemon=True)
        self._sync_thread.start()
        logger.info("Sync started")
        self._notify_status_change()
        return True

    def stop_sync(self) -> None:
        """Stop the rsync background sync loop"""
        if not self.sync_running:
            return
        self.sync_running = False
        logger.info("Stopping sync...")
        self._notify_status_change()

    def _run_sync_loop(self) -> None:
        """Background thread that periodically runs rsync"""
        while self.sync_running:
            success = self._run_rsync()

            if self.sync_running:
                if success:
                    logger.debug("Sync: Waiting for next interval")
                else:
                    logger.warning("Sync: Error occurred, will retry next interval")

                # Sleep with periodic checks for shutdown
                interval = self.config.rsync_interval_seconds or 60
                for _ in range(interval):
                    if not self.sync_running:
                        break
                    time.sleep(1)

        self.sync_running = False
        logger.info("Sync stopped")
        self._notify_status_change()

    def _run_rsync(self) -> bool:
        """Run rsync to sync clips to remote server. Returns True if successful."""
        host = self.config.rsync_host
        module = self.config.rsync_module
        username = self.config.rsync_username
        password = self.config.rsync_password

        # Sync entire clips directory (includes all event subdirectories)
        source_path = Path(self.config.output_dir).absolute()
        if not source_path.exists():
            logger.info(f"Sync: Clips directory does not exist yet: {source_path}")
            return True  # Not an error, just nothing to sync yet

        # Build rsync URL: username@host::module
        if username:
            rsync_url = f"{username}@{host}::{module}"
        else:
            rsync_url = f"{host}::{module}"

        # Build rsync command
        if sys.platform == 'win32':
            cwd = source_path.parent
            source_arg = './' + source_path.name + '/'
        else:
            cwd = None
            source_arg = str(source_path) + '/'

        cmd = [
            get_rsync_path(),
            '-avz',
            '--checksum',
            source_arg,
            rsync_url
        ]

        logger.info(f"Sync: Running rsync to {rsync_url}")

        # Set up environment with password
        env = os.environ.copy()
        if password:
            env['RSYNC_PASSWORD'] = password

        try:
            result = subprocess.run(
                cmd,
                env=env,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )

            if result.returncode == 0:
                lines = result.stdout.strip().split('\n') if result.stdout.strip() else []
                logger.info(f"Sync: Completed successfully ({len(lines)} items processed)")
                return True
            else:
                logger.error(f"Sync: rsync failed with code {result.returncode}")
                if result.stderr:
                    logger.error(f"Sync: {result.stderr.strip()}")
                return False

        except subprocess.TimeoutExpired:
            logger.error("Sync: rsync timed out after 5 minutes")
            return False
        except FileNotFoundError:
            logger.error("Sync: rsync command not found. Please install rsync.")
            return False
        except Exception as e:
            logger.error(f"Sync: Error running rsync: {e}")
            return False

    def start_tunnel(self) -> bool:
        """Start WebSocket tunnel to relay server. Returns True if started."""
        if self.tunnel_client and self.tunnel_client.is_connected():
            logger.warning("Tunnel already running")
            return False

        from web_api.websocket_server import WebSocketBroadcaster
        broadcaster = cast(WebSocketBroadcaster | None, self.ws_broadcaster)
        if not broadcaster or not broadcaster.loop:
            logger.error("Tunnel: Web server must be running first")
            return False

        from web_api.ws_tunnel_client import WSTunnelClient
        self.tunnel_client = WSTunnelClient(self.config)
        result = self.tunnel_client.start(broadcaster.loop)
        if result:
            self._notify_status_change()
        return result

    def stop_tunnel(self) -> None:
        """Stop WebSocket tunnel."""
        if self.tunnel_client:
            self.tunnel_client.stop()
            self.tunnel_client = None
            self._notify_status_change()

    async def stop_monitoring(self) -> None:
        """Stop FTC/OBS monitoring but keep web server running. Idempotent."""
        was_running = self.running
        self.running = False

        # Close FTC WebSocket
        if self.ftc_websocket and not self.ftc_websocket.closed:
            await self.ftc_websocket.close()
            logger.info("Closed FTC WebSocket connection")

        # Disconnect from OBS
        self.disconnect_from_obs()

        # Stop local video processor
        if self.local_video_processor:
            self.local_video_processor.stop_monitoring()
            self.local_video_processor = None
            logger.info("Stopped local video processor")

        if was_running:
            self._notify_status_change()
            logger.info("MatchBox monitoring stopped")

    async def shutdown(self) -> None:
        """Gracefully shutdown everything including web server"""
        await self.stop_monitoring()

        # Stop WebSocket tunnel
        self.stop_tunnel()

        # Stop WebSocket server
        if self.ws_broadcaster:
            self.ws_broadcaster.stop()

        # Stop web server and mDNS service
        self.stop_web_server()
        self.unregister_mdns_service()

        logger.info("MatchBox shutdown complete")

# Function to validate integer input
# From https://www.tutorialkart.com/python/tkinter/how-to-allow-only-integer-in-entry-widget-in-tkinter-python/
def validate_input(P: str):
    if P.isdigit() or P == "":
        return True
    return False

class MatchBoxGUI:
    """Tkinter GUI for MatchBox"""

    def __init__(self, root: tk.Tk, config: MatchBoxConfig) -> None:
        self.root: tk.Tk = root

        # Attempt to load version
        try:
            from _version import __version__  # pyright: ignore[reportMissingImports, reportUnknownVariableType]
            self.version = __version__
        except ModuleNotFoundError:
            self.version: str = "dev"

        self.root.title(f'MatchBox™ for FIRST® Tech Challenge - v{self.version}')  # pyright: ignore[reportUnknownMemberType]
        self.root.geometry("900x700")
        self.root.resizable(True, True)

        self.matchbox: MatchBoxCore | None = None
        self.config: MatchBoxConfig = config
        self.async_loop: asyncio.AbstractEventLoop | None = None
        self.monitor_task: asyncio.Task[None] | None = None
        self.thread: threading.Thread | None = None

        # Initialize instance variables for GUI widgets (set in load_config)
        self.event_code_var: tk.StringVar = tk.StringVar()
        self.scoring_host_var: tk.StringVar = tk.StringVar()
        self.scoring_port_var: tk.IntVar = tk.IntVar()
        self.obs_host_var: tk.StringVar = tk.StringVar()
        self.obs_port_var: tk.IntVar = tk.IntVar()
        self.obs_password_var: tk.StringVar = tk.StringVar()
        self.scene_mappings: dict[int, tk.StringVar] = {}
        self.output_dir_var: tk.StringVar = tk.StringVar()
        self.mdns_name_var: tk.StringVar = tk.StringVar()
        self.web_port_var: tk.IntVar = tk.IntVar()
        self.pre_match_buffer_var: tk.IntVar = tk.IntVar()
        self.post_match_buffer_var: tk.IntVar = tk.IntVar()
        self.match_duration_var: tk.IntVar = tk.IntVar()
        # rsync settings
        self.rsync_host_var: tk.StringVar = tk.StringVar()
        self.rsync_module_var: tk.StringVar = tk.StringVar()
        self.rsync_username_var: tk.StringVar = tk.StringVar()
        self.rsync_password_var: tk.StringVar = tk.StringVar()
        self.rsync_interval_var: tk.IntVar = tk.IntVar()

        # Sync thread state
        self.sync_thread: threading.Thread | None = None
        self.sync_running: bool = False

        # Sync UI elements (initialized in create_sync_settings_tab)
        self.start_sync_button: ttk.Button
        self.stop_sync_button: ttk.Button
        self.sync_status_var: tk.StringVar

        self.create_widgets()
        self.load_config_to_gui(self.config)

        # Create core instance and start web server immediately
        self.matchbox = MatchBoxCore(self.config)
        self.matchbox.ensure_web_server()
        self.matchbox.register_status_callback(self._on_core_status_change)
        logger.info(f"Admin UI available at http://localhost:{self.config.web_port}/admin")

    def create_widgets(self) -> None:
        """Create GUI widgets"""

        # Main frame
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Title
        bg_color: str = ttk.Style().lookup('TFrame', 'background')  # pyright: ignore[reportAny]
        if not bg_color:
            title_label = tk.Text(master=main_frame, height=1, wrap="none", relief="flat", borderwidth=0, highlightthickness=0, font=("", 16, "bold"))
        else:
            title_label = tk.Text(master=main_frame, height=1, wrap="none", relief="flat", borderwidth=0, highlightthickness=0, font=("", 16, "bold"), background=(bg_color))
        title_label.tag_configure("regular", font=("", 12))  # pyright: ignore[reportUnusedCallResult]
        title_label.tag_configure("bold", font=("", 16, "bold"))  # pyright: ignore[reportUnusedCallResult]
        title_label.tag_configure("bold_italic", font=("", 16, "bold italic"))  # pyright: ignore[reportUnusedCallResult]

        title_label.insert("1.0", "MatchBox™ for ", "bold")
        title_label.insert("end", "FIRST®", "bold_italic")
        title_label.insert("end", f" Tech Challenge ", "bold")
        title_label.insert("end", f"v{self.version}", "regular")
        title_label.config(state="disabled")  # pyright: ignore[reportUnusedCallResult]
        title_label.pack(pady=(0, 5), padx=(5, 0), fill="x")

        self.vcmd: str = self.root.register(validate_input)

        # Create notebook with tabs
        notebook = ttk.Notebook(main_frame)
        notebook.pack(fill=tk.BOTH, expand=False, pady=5)

        # Connection settings tab
        self.create_connection_tab(notebook)

        # Scene mapping tab
        self.create_scene_mapping_tab(notebook)

        # Video settings tab
        self.create_video_settings_tab(notebook)

        # Sync settings tab
        self.create_sync_settings_tab(notebook)

        # Control & Log Frame
        control_frame = ttk.Frame(main_frame, padding="10")
        control_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        # Control buttons
        button_frame = ttk.Frame(control_frame)
        button_frame.pack(fill=tk.X)

        self.configure_obs_button: ttk.Button = ttk.Button(button_frame, text="Configure OBS Scenes",
                                             command=self.configure_obs_scenes)
        self.configure_obs_button.pack(side=tk.LEFT, padx=5)

        self.start_button: ttk.Button = ttk.Button(button_frame, text="Start MatchBox",
                                     command=self.start_matchbox)
        self.start_button.pack(side=tk.LEFT, padx=5)

        self.stop_button: ttk.Button = ttk.Button(button_frame, text="Stop MatchBox",
                                    command=self.stop_matchbox, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=5)

        ttk.Button(button_frame, text="Save Config", command=self.save_config).pack(side=tk.LEFT, padx=5)

        # Status indicator
        self.status_var: tk.StringVar = tk.StringVar(value="Status: Not Running 🔴")
        status_label = ttk.Label(button_frame, textvariable=self.status_var)
        status_label.pack(side=tk.RIGHT, padx=5)

        # Log area
        ttk.Label(control_frame, text="Log", font=("", 10, "bold")).pack(anchor=tk.W, pady=(10, 5))

        # Scrolling text box with ttk Scrollbar (https://stackoverflow.com/a/13833338)
        class TextScrollCombo(ttk.Frame):

            def __init__(self, *args, **kwargs):  # pyright: ignore[reportUnknownParameterType, reportMissingParameterType]
                
                super().__init__(*args, **kwargs)  # pyright: ignore[reportUnknownArgumentType]
                
            # ensure a consistent GUI size
                self.grid_propagate(False)
            # implement stretchability
                _ = self.grid_rowconfigure(0, weight=1)
                _ = self.grid_columnconfigure(0, weight=1)
                
            # create a Text widget
                self.txt: tk.Text = tk.Text(self)
                self.txt.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)

            # create a Scrollbar and associate it with txt
                scrollb = ttk.Scrollbar(self, command=self.txt.yview)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                scrollb.grid(row=0, column=1, sticky='nsew', padx=0, pady=0)
                self.txt['yscrollcommand'] = scrollb.set

        log_combo = TextScrollCombo(control_frame)
        log_combo.pack(fill="both", expand=True)
        _ = log_combo.txt.config(state=tk.DISABLED)

        self.log_text: tk.Text = log_combo.txt

        # Set up GUI logging handler (thread-safe via root.after_idle)
        gui_handler.set_callback(self.root, self.log_to_gui)

    def create_connection_tab(self, notebook: ttk.Notebook) -> None:
        """Create connection settings tab"""
        conn_frame = ttk.Frame(notebook, padding="10")
        notebook.add(conn_frame, text="Connection Settings")

        # FTC Settings
        ttk.Label(conn_frame, text="FTC Scoring System", font=("", 12, "bold")).grid(
            row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 5))

        ttk.Label(conn_frame, text="Event Code:").grid(row=1, column=0, sticky=tk.W, pady=2, padx=(0, 10))
        ttk.Entry(conn_frame, textvariable=self.event_code_var, width=30).grid(
            row=1, column=1, sticky=tk.W, pady=2)

        ttk.Label(conn_frame, text="Scoring System Host:").grid(row=2, column=0, sticky=tk.W, pady=2, padx=(0, 10))
        ttk.Entry(conn_frame, textvariable=self.scoring_host_var, width=30).grid(
            row=2, column=1, sticky=tk.W, pady=2)

        ttk.Label(conn_frame, text="Port:").grid(row=2, column=2, sticky=tk.W, pady=2, padx=10)
        ttk.Entry(conn_frame, textvariable=self.scoring_port_var, width=6, validate="key", validatecommand=(self.vcmd, "%P")).grid(
            row=2, column=3, sticky=tk.W, pady=2)

        # OBS Settings
        ttk.Label(conn_frame, text="OBS Settings", font=("", 12, "bold")).grid(
            row=3, column=0, columnspan=3, sticky=tk.W, pady=(10, 5))

        ttk.Label(conn_frame, text="OBS WebSocket Host:").grid(row=4, column=0, sticky=tk.W, pady=2, padx=(0, 10))
        ttk.Entry(conn_frame, textvariable=self.obs_host_var, width=30).grid(
            row=4, column=1, sticky=tk.W, pady=2)

        ttk.Label(conn_frame, text="Port:").grid(row=4, column=2, sticky=tk.W, pady=2, padx=10)
        ttk.Entry(conn_frame, textvariable=self.obs_port_var, width=6, validate="key", validatecommand=(self.vcmd, "%P")).grid(
            row=4, column=3, sticky=tk.W, pady=2)

        ttk.Label(conn_frame, text="Password:").grid(row=5, column=0, sticky=tk.W, pady=2, padx=(0, 10))
        ttk.Entry(conn_frame, textvariable=self.obs_password_var, width=30, show="*").grid(
            row=5, column=1, sticky=tk.W, pady=2)

    def create_scene_mapping_tab(self, notebook: ttk.Notebook) -> None:
        """Create scene mapping tab"""
        mapping_frame = ttk.Frame(notebook, padding="10")
        notebook.add(mapping_frame, text="Scene Mapping")

        ttk.Label(mapping_frame, text="Field to Scene Mapping", font=("", 12, "bold")).grid(
            row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 10))

        # Scene mapping entries
        self.scene_mappings = {}
        for i in range(1, 4):
            ttk.Label(mapping_frame, text=f"Field {i} Scene:").grid(
                row=i, column=0, sticky=tk.W, pady=2, padx=(0, 10))
            scene_var = tk.StringVar()
            ttk.Entry(mapping_frame, textvariable=scene_var, width=30).grid(
                row=i, column=1, sticky=tk.W, pady=2)
            self.scene_mappings[i] = scene_var
        
        info_text = (f"Scenes for fields that do not exist at a given event/division can be safely ignored.")
        ttk.Label(mapping_frame, text=info_text, foreground="gray").grid(
            row=5, column=0, columnspan=3, sticky=tk.W, pady=5)

    def create_video_settings_tab(self, notebook: ttk.Notebook) -> None:
        """Create video settings tab"""
        video_frame = ttk.Frame(notebook, padding="10")
        notebook.add(video_frame, text="Video & Web Settings")

        # Output settings
        ttk.Label(video_frame, text="Video Output", font=("", 12, "bold")).grid(
            row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 5))

        ttk.Label(video_frame, text="Output Directory:").grid(row=1, column=0, sticky=tk.W, pady=2, padx=(0, 10))
        ttk.Entry(video_frame, textvariable=self.output_dir_var, width=30).grid(
            row=1, column=1, sticky=tk.W, pady=2)
        ttk.Button(video_frame, text="Browse...", command=self.browse_output_dir).grid(
            row=1, column=2, sticky=tk.W, pady=2, padx=(10, 0))

        # Web server settings
        ttk.Label(video_frame, text="Local Web Server", font=("", 12, "bold")).grid(
            row=2, column=0, columnspan=3, sticky=tk.W, pady=(10, 5))

        ttk.Label(video_frame, text="Web Server Name:").grid(row=3, column=0, sticky=tk.W, pady=2, padx=(0, 10))
        ttk.Entry(video_frame, textvariable=self.mdns_name_var, width=30).grid(
            row=3, column=1, sticky=tk.W, pady=2)

        server_port_frame = ttk.Frame(video_frame, padding="0")
        server_port_frame.grid(row=3, column=2, sticky=tk.W)
        ttk.Label(server_port_frame, text="Port:").grid(row=0, column=0, sticky=tk.W, pady=2, padx=10)
        ttk.Entry(server_port_frame, textvariable=self.web_port_var, width=6, validate="key", validatecommand=(self.vcmd, "%P")).grid(
            row=0, column=1, sticky=tk.W, pady=2)

        info_text = (f"Match clips will be available at http://{self.config.mdns_name}:{self.config.web_port}")
        ttk.Label(video_frame, text=info_text, foreground="gray").grid(
            row=4, column=0, columnspan=3, sticky=tk.W, pady=5)

        # Video processing settings
        ttk.Label(video_frame, text="Video Processing", font=("", 12, "bold")).grid(
            row=5, column=0, columnspan=3, sticky=tk.W, pady=(10, 5))

        ttk.Label(video_frame, text="Pre-match buffer (seconds):").grid(row=6, column=0, sticky=tk.W, pady=2, padx=(0, 10))
        ttk.Entry(video_frame, textvariable=self.pre_match_buffer_var, width=6, validate="key", validatecommand=(self.vcmd, "%P")).grid(
            row=6, column=1, sticky=tk.W, pady=2)

        ttk.Label(video_frame, text="Post-match buffer (seconds):").grid(row=7, column=0, sticky=tk.W, pady=2, padx=(0, 10))
        ttk.Entry(video_frame, textvariable=self.post_match_buffer_var, width=6, validate="key", validatecommand=(self.vcmd, "%P")).grid(
            row=7, column=1, sticky=tk.W, pady=2)

        ttk.Label(video_frame, text="Match duration (seconds):").grid(row=8, column=0, sticky=tk.W, pady=2, padx=(0, 10))
        ttk.Entry(video_frame, textvariable=self.match_duration_var, width=6, validate="key", validatecommand=(self.vcmd, "%P")).grid(
            row=8, column=1, sticky=tk.W, pady=2)

    def create_sync_settings_tab(self, notebook: ttk.Notebook) -> None:
        """Create sync settings tab for rsync configuration"""
        sync_frame = ttk.Frame(notebook, padding="10")
        notebook.add(sync_frame, text="Sync Settings")

        # rsync Settings header
        ttk.Label(sync_frame, text="Remote Sync (rsync)", font=("", 12, "bold")).grid(
            row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 5))

        # Host
        ttk.Label(sync_frame, text="rsync Host:").grid(row=1, column=0, sticky=tk.W, pady=2, padx=(0, 10))
        ttk.Entry(sync_frame, textvariable=self.rsync_host_var, width=30).grid(
            row=1, column=1, sticky=tk.W, pady=2)

        # Module
        ttk.Label(sync_frame, text="rsync Module:").grid(row=2, column=0, sticky=tk.W, pady=2, padx=(0, 10))
        ttk.Entry(sync_frame, textvariable=self.rsync_module_var, width=30).grid(
            row=2, column=1, sticky=tk.W, pady=2)

        # Username
        ttk.Label(sync_frame, text="Username:").grid(row=3, column=0, sticky=tk.W, pady=2, padx=(0, 10))
        ttk.Entry(sync_frame, textvariable=self.rsync_username_var, width=30).grid(
            row=3, column=1, sticky=tk.W, pady=2)

        # Password
        ttk.Label(sync_frame, text="Password:").grid(row=4, column=0, sticky=tk.W, pady=2, padx=(0, 10))
        ttk.Entry(sync_frame, textvariable=self.rsync_password_var, width=30, show="*").grid(
            row=4, column=1, sticky=tk.W, pady=2)

        # Interval
        ttk.Label(sync_frame, text="Sync interval (seconds):").grid(row=5, column=0, sticky=tk.W, pady=2, padx=(0, 10))
        ttk.Entry(sync_frame, textvariable=self.rsync_interval_var, width=6, validate="key", validatecommand=(self.vcmd, "%P")).grid(
            row=5, column=1, sticky=tk.W, pady=2)

        # Control buttons frame
        sync_button_frame = ttk.Frame(sync_frame)
        sync_button_frame.grid(row=6, column=0, columnspan=3, sticky=tk.W, pady=(15, 5))

        self.start_sync_button = ttk.Button(sync_button_frame, text="Start Sync",
                                             command=self.start_sync)
        self.start_sync_button.pack(side=tk.LEFT, padx=(0, 5))

        self.stop_sync_button = ttk.Button(sync_button_frame, text="Stop Sync",
                                            command=self.stop_sync, state=tk.DISABLED)
        self.stop_sync_button.pack(side=tk.LEFT, padx=5)

        # Sync status indicator
        self.sync_status_var = tk.StringVar(value="Sync: Stopped")
        ttk.Label(sync_button_frame, textvariable=self.sync_status_var).pack(side=tk.LEFT, padx=(15, 0))

    def browse_output_dir(self) -> None:
        """Browse for output directory"""
        directory = filedialog.askdirectory(
            initialdir=self.output_dir_var.get(),
            title="Select Output Directory for Match Clips"
        )
        if directory:
            self.output_dir_var.set(directory)

    def start_sync(self) -> None:
        """Start the rsync background thread via core"""
        self.load_gui_to_config()
        assert self.matchbox is not None

        if not self.config.rsync_host:
            _ = messagebox.showerror("Error", "rsync host is required")
            return
        if not self.config.rsync_module:
            _ = messagebox.showerror("Error", "rsync module is required")
            return

        if self.matchbox.start_sync():
            _ = self.start_sync_button.config(state=tk.DISABLED)
            _ = self.stop_sync_button.config(state=tk.NORMAL)
            _ = self.sync_status_var.set("Sync: Running")

    def stop_sync(self) -> None:
        """Stop the rsync background thread via core"""
        assert self.matchbox is not None
        self.matchbox.stop_sync()

        _ = self.start_sync_button.config(state=tk.NORMAL)
        _ = self.stop_sync_button.config(state=tk.DISABLED)
        _ = self.sync_status_var.set("Sync: Stopped")

    def load_gui_to_config(self):
        """Set configuration from GUI"""
        self.config.event_code = self.event_code_var.get()
        self.config.scoring_host = self.scoring_host_var.get()
        self.config.scoring_port = self.scoring_port_var.get()
        self.config.obs_host = self.obs_host_var.get()
        self.config.obs_port = self.obs_port_var.get()
        self.config.obs_password = self.obs_password_var.get()
        self.config.output_dir = self.output_dir_var.get()
        self.config.mdns_name = self.mdns_name_var.get()
        self.config.web_port = self.web_port_var.get()
        self.config.pre_match_buffer_seconds = self.pre_match_buffer_var.get()
        self.config.post_match_buffer_seconds = self.post_match_buffer_var.get()
        self.config.match_duration_seconds = self.match_duration_var.get()
        self.config.field_scene_mapping = {int(k): v.get() for k, v in self.scene_mappings.items()}
        # rsync settings
        self.config.rsync_host = self.rsync_host_var.get()
        self.config.rsync_module = self.rsync_module_var.get()
        self.config.rsync_username = self.rsync_username_var.get()
        self.config.rsync_password = self.rsync_password_var.get()
        self.config.rsync_interval_seconds = self.rsync_interval_var.get()

    def load_config_to_gui(self, config: MatchBoxConfig) -> None:
        """Load configuration into GUI"""
        self.event_code_var.set(config.event_code)
        self.scoring_host_var.set(config.scoring_host)
        self.scoring_port_var.set(config.scoring_port)
        self.obs_host_var.set(config.obs_host)
        self.obs_port_var.set(config.obs_port)
        self.obs_password_var.set(config.obs_password)
        self.output_dir_var.set(config.output_dir)
        self.mdns_name_var.set(config.mdns_name)
        self.web_port_var.set(config.web_port)
        self.pre_match_buffer_var.set(config.pre_match_buffer_seconds)
        self.post_match_buffer_var.set(config.post_match_buffer_seconds)
        self.match_duration_var.set(config.match_duration_seconds)

        # Load scene mappings
        field_scene_mapping = config.field_scene_mapping
        for field_num, scene_var in self.scene_mappings.items():
            scene_var.set(field_scene_mapping.get(field_num, f"Field {field_num}"))

        # Load rsync settings
        self.rsync_host_var.set(config.rsync_host)
        self.rsync_module_var.set(config.rsync_module)
        self.rsync_username_var.set(config.rsync_username)
        self.rsync_password_var.set(config.rsync_password)
        self.rsync_interval_var.set(config.rsync_interval_seconds)

    def save_config(self) -> None:
        """Save configuration to file"""
        try:
            # First load current GUI values into config
            self.load_gui_to_config()
            with open(get_config_path(), "w") as f:
                json.dump(vars(self.config), f, indent=2)
            logger.info("Configuration saved to " + get_config_path())
        except Exception as e:
            logger.error(f"Error saving configuration: {e}")
            _ = messagebox.showerror("Error", f"Failed to save configuration: {e}")

    def configure_obs_scenes(self) -> None:
        """Configure OBS scenes"""
        self.load_gui_to_config()
        if not self.config.event_code:
            _ = messagebox.showerror("Error", "Event code is required")
            return

        assert self.matchbox is not None
        self.matchbox.config = self.config

        if self.matchbox.configure_obs_scenes():
            logger.info("OBS scenes configured successfully!")
        else:
            logger.error("Failed to configure OBS scenes")

        # Only disconnect if not actively monitoring
        if not self.matchbox.running:
            self.matchbox.disconnect_from_obs()

    def start_matchbox(self) -> None:
        """Start MatchBox operation"""
        self.load_gui_to_config()

        if not self.config.event_code:
            _ = messagebox.showerror("Error", "Event code is required")
            return

        # Update existing core's config and reset clips dir
        assert self.matchbox is not None
        self.matchbox.config = self.config
        self.matchbox.clips_dir = Path(self.config.output_dir).absolute() / self.config.event_code
        self.matchbox.clips_dir.mkdir(exist_ok=True, parents=True)

        # Create new event loop
        self.async_loop = asyncio.new_event_loop()

        # Start monitoring in separate thread
        self.thread = threading.Thread(target=self.run_async_monitoring, daemon=True)
        self.thread.start()

        # Update UI
        _ = self.start_button.config(state=tk.DISABLED)
        _ = self.stop_button.config(state=tk.NORMAL)
        _ = self.configure_obs_button.config(state=tk.DISABLED)
        _ = self.status_var.set("Status: Running 🟢")

        logger.info("MatchBox started!")
        logger.info(f"Match clips will be available at http://{self.config.mdns_name}:{self.config.web_port}")

    def run_async_monitoring(self) -> None:
        """Run async monitoring in separate thread"""
        assert self.async_loop is not None
        assert self.matchbox is not None

        asyncio.set_event_loop(self.async_loop)
        self.monitor_task = self.async_loop.create_task(self.matchbox.monitor_ftc_websocket())

        try:
            self.async_loop.run_until_complete(self.monitor_task)
        except asyncio.CancelledError:
            pass
        finally:
            _ = self.root.after(0, self.update_ui_after_stop)

    def stop_matchbox(self) -> None:
        """Stop MatchBox monitoring (web server stays running)"""
        if self.matchbox and self.matchbox.running:
            logger.info("Stopping MatchBox...")
            self.matchbox.running = False

            # Cancel monitoring task - its finally block calls stop_monitoring()
            if self.monitor_task and not self.monitor_task.done() and self.async_loop:
                _ = self.async_loop.call_soon_threadsafe(self.monitor_task.cancel)

    def update_ui_after_stop(self) -> None:
        """Update UI after MatchBox stops"""
        _ = self.start_button.config(state=tk.NORMAL)
        _ = self.stop_button.config(state=tk.DISABLED)
        _ = self.configure_obs_button.config(state=tk.NORMAL)
        _ = self.status_var.set("Status: Not Running 🔴")
        logger.info("MatchBox stopped")

    def _on_core_status_change(self, status: dict[str, object]) -> None:
        """Called from any thread when core status changes - schedule GUI update"""
        try:
            self.root.after_idle(self._apply_core_status, status)
        except Exception:
            pass  # GUI might be destroyed

    def _apply_core_status(self, status: dict[str, object]) -> None:
        """Apply core status to GUI widgets (runs on main thread)"""
        # Update running state
        running = bool(status.get('running'))
        _ = self.start_button.config(state=tk.DISABLED if running else tk.NORMAL)
        _ = self.stop_button.config(state=tk.NORMAL if running else tk.DISABLED)
        _ = self.configure_obs_button.config(state=tk.DISABLED if running else tk.NORMAL)
        _ = self.status_var.set("Status: Running 🟢" if running else "Status: Not Running 🔴")

        # Update sync state
        sync_running = bool(status.get('sync_running'))
        _ = self.start_sync_button.config(state=tk.DISABLED if sync_running else tk.NORMAL)
        _ = self.stop_sync_button.config(state=tk.NORMAL if sync_running else tk.DISABLED)
        _ = self.sync_status_var.set("Sync: Running" if sync_running else "Sync: Stopped")

    def log_to_gui(self, level: str, message: str) -> None:
        """Log message to GUI (called by GUILogHandler)"""
        _ = self.log_text.config(state=tk.NORMAL)
        _ = self.log_text.insert(tk.END, f"{time.strftime('%H:%M:%S')} [{level}] {message}\n")
        _ = self.log_text.see(tk.END)
        _ = self.log_text.config(state=tk.DISABLED)
        _ = self.log_text.update_idletasks()  # Force GUI refresh to prevent text disappearing

    def on_closing(self) -> None:
        """Handle window close - full shutdown including web server"""
        if self.matchbox:
            # Stop monitoring if running
            if self.matchbox.running:
                self.matchbox.running = False
                if self.monitor_task and not self.monitor_task.done() and self.async_loop:
                    _ = self.async_loop.call_soon_threadsafe(self.monitor_task.cancel)
                # Give the finally block a moment to clean up
                time.sleep(0.5)

            # Stop sync if running
            self.matchbox.stop_sync()

            # Always tear down web server and mDNS on app close
            if self.matchbox.ws_broadcaster:
                self.matchbox.ws_broadcaster.stop()
            self.matchbox.stop_web_server()
            self.matchbox.unregister_mdns_service()
        self.root.destroy()

def get_config_path() -> str:
    """Get the appropriate path for saving config file"""
    # Check if running in macOS app bundle
    if sys.platform == "darwin" and getattr(sys, 'frozen', False):
        # Running in a macOS app bundle
        desktop = Path.home() / "Desktop"
        return str(desktop / "matchbox_config.json")
    
    # Default: save in current directory
    return "matchbox_config.json"

def main() -> None:
    """Main function"""
    parser = argparse.ArgumentParser(description="MatchBox™ for FIRST® Tech Challenge")
    _ = parser.add_argument("--config", "-c", help="Configuration file path")
    _ = parser.add_argument("--cli", action="store_true", help="Run in CLI mode (no GUI)")
    _ = parser.add_argument("--event-code", help="FTC Event Code")
    _ = parser.add_argument("--scoring-host", default="localhost", help="Scoring system host")
    _ = parser.add_argument("--scoring-port", type=int, default=80, help="Scoring system port")
    _ = parser.add_argument("--obs-host", default="localhost", help="OBS WebSocket host")
    _ = parser.add_argument("--obs-port", type=int, default=4455, help="OBS WebSocket port")
    _ = parser.add_argument("--obs-password", default="", help="OBS WebSocket password")

    args = parser.parse_args()

    # Load configuration
    config: MatchBoxConfig = MatchBoxConfig()
    if cast(str, args.config):
        try:
            with open(cast(str, args.config), 'r') as f:
                file = json.load(f)  # pyright: ignore[reportAny]
                config.__dict__.update(file)  # pyright: ignore[reportAny]
                # Fix field_scene_mapping keys to be integers (JSON deserializes them as strings)
                if 'field_scene_mapping' in file:
                    config.field_scene_mapping = {int(k): v for k, v in file['field_scene_mapping'].items()}  # pyright: ignore[reportAny]
            logger.info("Configuration loaded from" + cast(str, args.config))
        except Exception as e:
            print(f"Error loading config file: {e}")
            sys.exit(1)
    else:
        try:
            with open(get_config_path(), "r") as f:
                file = json.load(f)  # pyright: ignore[reportAny]
                config.__dict__.update(file)  # pyright: ignore[reportAny]
                # Fix field_scene_mapping keys to be integers (JSON deserializes them as strings)
                if 'field_scene_mapping' in file:
                    config.field_scene_mapping = {int(k): v for k, v in file['field_scene_mapping'].items()}  # pyright: ignore[reportAny]
            logger.info("Configuration loaded from " + get_config_path())
        except FileNotFoundError:
            logger.warning("No configuration file found")
        except Exception as e:
            logger.error(f"Error loading configuration: {e}")

    # Override config with command line arguments
    if cast(str, args.event_code):
        config.event_code = cast(str, args.event_code)
    if cast(str, args.scoring_host) != "localhost":
        config.scoring_host = cast(str, args.scoring_host)
    if cast(int, args.scoring_port) != 80:
        config.scoring_port = cast(int, args.scoring_port)
    if cast(str, args.obs_host) != "localhost":
        config.obs_host = cast(str, args.obs_host)
    if cast(int, args.obs_port) != 4455:
        config.obs_port = cast(int, args.obs_port)
    if cast(str, args.obs_password):
        config.obs_password = cast(str, args.obs_password)

    if cast(bool, args.cli):
        # CLI mode
        if not config.event_code:
            print("Event code is required")
            sys.exit(1)

        print("Starting MatchBox in CLI mode...")
        matchbox = MatchBoxCore(config)

        def signal_handler(_sig: int, _frame: object) -> None:
            print("\nShutting down...")
            _ = asyncio.create_task(matchbox.shutdown())
            sys.exit(0)

        _ = signal.signal(signal.SIGINT, signal_handler)

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

        # Hide the splash screen now that the application itself is launching
        try:
            import pyi_splash  # pyright: ignore[reportMissingModuleSource]
            pyi_splash.close()
        except:
            pass

        # Attempt to load version
        try:
            from _version import __version__  # pyright: ignore[reportMissingImports, reportUnknownVariableType]
            version = __version__  # pyright: ignore[reportUnknownVariableType]
        except ModuleNotFoundError:
            version: str = "dev"

        # Set application icon
        try:
            if "dev" in version:
                icon = PhotoImage(file=Path(__file__).with_name('us.brainstormz.MatchBox.Devel.png'))
            else:
                icon = PhotoImage(file=Path(__file__).with_name('us.brainstormz.MatchBox.png'))
            root.iconphoto(False, icon)
        except TclError as e:
            print("Icon loading failed!")
            print(e)

        app = MatchBoxGUI(root, config)
        root.protocol("WM_DELETE_WINDOW", app.on_closing)

        root.mainloop()


if __name__ == "__main__":
    main()