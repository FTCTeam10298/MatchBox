#!/usr/bin/env python3
"""
MatchBox CLI - Command line interface for MatchBox
"""

import argparse
import sys
import json
import asyncio
import signal
from typing import cast
from matchbox import MatchBoxConfig, MatchBoxCore

def main():
    """Main CLI function"""
    parser = argparse.ArgumentParser(
        description="MatchBox™ for FIRST® Tech Challenge - CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --event-code MYEVENT123 --obs-password mypass
  %(prog)s --config matchbox_config.json
  %(prog)s --event-code TEST --configure-obs-only
        """
    )

    # Configuration
    _ = parser.add_argument("--config", "-c", help="Load configuration from JSON file")
    _ = parser.add_argument("--save-config", help="Save current configuration to JSON file")

    # FTC Settings
    ftc_group = parser.add_argument_group("FTC Scoring System")
    _ = ftc_group.add_argument("--event-code", required=True, help="FTC Event Code (required)")
    _ = ftc_group.add_argument("--scoring-host", default="localhost", help="Scoring system host (default: localhost)")
    _ = ftc_group.add_argument("--scoring-port", type=int, default=80, help="Scoring system port (default: 80)")
    _ = ftc_group.add_argument("--num-fields", type=int, default=2, help="Number of fields (default: 2)")

    # OBS Settings
    obs_group = parser.add_argument_group("OBS Settings")
    _ = obs_group.add_argument("--obs-host", default="localhost", help="OBS WebSocket host (default: localhost)")
    _ = obs_group.add_argument("--obs-port", type=int, default=4455, help="OBS WebSocket port (default: 4455)")
    _ = obs_group.add_argument("--obs-password", default="", help="OBS WebSocket password")

    # Scene Mapping
    scene_group = parser.add_argument_group("Scene Mapping")
    _ = scene_group.add_argument("--field1-scene", default="Field 1", help="Scene name for Field 1 (default: 'Field 1')")
    _ = scene_group.add_argument("--field2-scene", default="Field 2", help="Scene name for Field 2 (default: 'Field 2')")

    # Video and Web Settings
    video_group = parser.add_argument_group("Video & Web Settings")
    _ = video_group.add_argument("--output-dir", default="./match_clips", help="Output directory for match clips (default: ./match_clips)")
    _ = video_group.add_argument("--web-port", type=int, default=8000, help="Local web server port (default: 8000)")

    # Actions
    action_group = parser.add_argument_group("Actions")
    _ = action_group.add_argument("--configure-obs-only", action="store_true", help="Only configure OBS scenes and exit")
    _ = action_group.add_argument("--test-connection", action="store_true", help="Test connections to OBS and FTC scoring system")

    # Other
    _ = parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()

    # Load configuration
    config: MatchBoxConfig = MatchBoxConfig()
    if cast(str, args.config):
        try:
            with open(cast(str, args.config), 'r') as f:
                file = json.load(f)
                config.__dict__.update(file)
                # Fix field_scene_mapping keys to be integers (JSON deserializes them as strings)
                if 'field_scene_mapping' in file:
                    config.field_scene_mapping = {int(k): v for k, v in file['field_scene_mapping'].items()}
            print("Configuration loaded from" + cast(str, args.config))
        except Exception as e:
            print(f"Error loading config file: {e}")
            sys.exit(1)
    else:
        try:
            with open("matchbox_config.json", "r") as f:
                file = json.load(f)
                config.__dict__.update(file)
                # Fix field_scene_mapping keys to be integers (JSON deserializes them as strings)
                if 'field_scene_mapping' in file:
                    config.field_scene_mapping = {int(k): v for k, v in file['field_scene_mapping'].items()}
            print("Configuration loaded from matchbox_config.json")
        except FileNotFoundError:
            print("No configuration file found")
        except Exception as e:
            print(f"Error loading configuration: {e}")
    
    # Update config from arguments
    if cast(str, args.event_code): config.event_code = cast(str, args.event_code)
    if cast(str, args.scoring_host): config.scoring_host = cast(str, args.scoring_host)
    if cast(int, args.scoring_port): config.scoring_port = cast(int, args.scoring_port)
    if cast(str, args.obs_host): config.obs_host = cast(str, args.obs_host)
    if cast(int, args.obs_port): config.obs_port = cast(int, args.obs_port)
    if cast(str, args.obs_password): config.obs_password = cast(str, args.obs_password)
    if cast(int, args.num_fields): config.num_fields = cast(int, args.num_fields)
    if cast(str, args.output_dir): config.output_dir = cast(str, args.output_dir)
    if cast(int, args.web_port): config.web_port = cast(int, args.web_port)
    if cast(str, args.field1_scene): config.field_scene_mapping = {
        1: cast(str, args.field1_scene),
        2: cast(str, args.field2_scene)
    }

    # Save config if requested
    if cast(str | None, args.save_config):
        try:
            with open(cast(str, args.save_config), 'w') as f:
                json.dump(config, f, indent=2)
            print(f"Configuration saved to {cast(str, args.save_config)}")
            return
        except Exception as e:
            print(f"Error saving config file: {e}")
            sys.exit(1)

    # Create MatchBox instance
    matchbox = MatchBoxCore(config)

    # Set up logging
    if cast(bool, args.verbose):
        import logging
        logging.getLogger().setLevel(logging.DEBUG)

    if cast(bool, args.configure_obs_only):
        print("Configuring OBS scenes...")
        if matchbox.configure_obs_scenes():
            print("✅ OBS scenes configured successfully!")
        else:
            print("❌ Failed to configure OBS scenes")
            sys.exit(1)
        return

    if cast(bool, args.test_connection):
        print("Testing connections...")

        # Test OBS connection
        print("Testing OBS connection...")
        if matchbox.connect_to_obs():
            print("✅ OBS connection successful")
            matchbox.disconnect_from_obs()
        else:
            print("❌ OBS connection failed")

        # Test FTC connection (basic websocket test)
        print("Testing FTC scoring system connection...")
        ftc_ws_url = f"ws://{config.scoring_host}:{config.scoring_port}/stream/display/command/?code={config.event_code}"
        print(f"Trying to connect to: {ftc_ws_url}")

        try:
            import websockets.client
            async def test_ftc():
                try:
                    async with websockets.client.connect(ftc_ws_url, open_timeout=5) as _:
                        print("✅ FTC scoring system connection successful")
                        return True
                except Exception as e:
                    print(f"❌ FTC scoring system connection failed: {e}")
                    return False

            success = asyncio.run(test_ftc())
            if not success:
                sys.exit(1)
        except Exception as e:
            print(f"❌ Error testing FTC connection: {e}")
            sys.exit(1)

        print("✅ All connections successful!")
        return

    # Normal operation
    print("Starting MatchBox™ for FIRST® Tech Challenge...")
    print(f"Event Code: {config.event_code}")
    print(f"Scoring System: {config.scoring_host}:{config.scoring_port}")
    print(f"OBS WebSocket: {config.obs_host}:{config.obs_port}")
    print(f"Match clips will be available at: http://localhost:{config.web_port}")
    print()
    print("Press Ctrl+C to stop")

    # Set up signal handler for graceful shutdown
    def signal_handler(_sig: int, _frame: object) -> None:
        print("\n🛑 Shutting down MatchBox...")
        sys.exit(0)

    _ = signal.signal(signal.SIGINT, signal_handler)
    _ = signal.signal(signal.SIGTERM, signal_handler)

    try:
        asyncio.run(matchbox.monitor_ftc_websocket())
    except KeyboardInterrupt:
        print("\n🛑 Shutting down MatchBox...")
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()