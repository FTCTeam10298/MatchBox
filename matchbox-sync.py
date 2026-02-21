#!/usr/bin/env python3
"""
MatchBox Sync - Standalone rsync daemon for syncing match clips to a remote server.

This script runs independently of the main MatchBox application and periodically
syncs match clips using rsync daemon protocol.

Usage:
    python matchbox-sync.py              # Run continuously with configured interval
    python matchbox-sync.py --once       # Single sync then exit
    python matchbox-sync.py --config /path/to/config.json
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger("matchbox-sync")

# Flag for graceful shutdown
shutdown_requested = False


def get_config_path() -> str:
    """Get the appropriate path for config file"""
    if sys.platform == "darwin" and getattr(sys, 'frozen', False):
        desktop = Path.home() / "Desktop"
        return str(desktop / "matchbox_config.json")
    return "matchbox_config.json"


def load_config(config_path: str) -> dict[str, object]:
    """Load configuration from JSON file"""
    with open(config_path, 'r') as f:
        return cast(dict[str, object], json.load(f))


def run_rsync(config: dict[str, object]) -> bool:
    """
    Run rsync to sync clips to remote server.
    Returns True if successful, False otherwise.
    """
    # Get rsync settings
    host = str(config.get('rsync_host', ''))
    module = str(config.get('rsync_module', ''))
    username = str(config.get('rsync_username', ''))
    password = str(config.get('rsync_password', ''))

    if not host or not module:
        logger.error("rsync host and module are required")
        return False

    # Build source path: output_dir/event_code/
    output_dir = str(config.get('output_dir', './match_clips'))
    event_code = str(config.get('event_code', ''))

    if not event_code:
        logger.error("Event code is required")
        return False

    source_path = Path(output_dir).absolute() / event_code
    if not source_path.exists():
        logger.warning(f"Source directory does not exist: {source_path}")
        return True  # Not an error, just nothing to sync yet

    # Build rsync URL: rsync://username@host/module/
    if username:
        rsync_url = f"rsync://{username}@{host}/{module}/"
    else:
        rsync_url = f"rsync://{host}/{module}/"

    # Build rsync command
    cmd = [
        'rsync',
        '-avz',
        '--checksum',
        str(source_path) + '/',  # Trailing slash to sync contents
        rsync_url
    ]

    logger.info(f"Running: {' '.join(cmd)}")

    # Set up environment with password
    env = os.environ.copy()
    if password:
        env['RSYNC_PASSWORD'] = password

    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

        if result.returncode == 0:
            if result.stdout.strip():
                logger.info(f"rsync output:\n{result.stdout}")
            logger.info("Sync completed successfully")
            return True
        else:
            logger.error(f"rsync failed with code {result.returncode}")
            if result.stderr:
                logger.error(f"rsync stderr: {result.stderr}")
            return False

    except subprocess.TimeoutExpired:
        logger.error("rsync timed out after 5 minutes")
        return False
    except FileNotFoundError:
        logger.error("rsync command not found. Please install rsync.")
        return False
    except Exception as e:
        logger.error(f"Error running rsync: {e}")
        return False


def signal_handler(_signum: int, _frame: object) -> None:
    """Handle shutdown signals"""
    global shutdown_requested
    logger.info("Shutdown signal received, finishing current operation...")
    shutdown_requested = True


def main() -> None:
    """Main function"""
    global shutdown_requested

    parser = argparse.ArgumentParser(
        description="MatchBox Sync - rsync daemon for match clip synchronization"
    )
    _ = parser.add_argument(
        "--config", "-c",
        help="Path to configuration file (default: matchbox_config.json)"
    )
    _ = parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync and exit"
    )

    args = parser.parse_args()
    once: bool = bool(args.once)  # pyright: ignore[reportAny]
    config_arg: str | None = args.config  # pyright: ignore[reportAny]

    # Determine config path
    config_path: str = config_arg if config_arg else get_config_path()

    # Load configuration
    try:
        config = load_config(config_path)
        logger.info(f"Configuration loaded from {config_path}")
    except FileNotFoundError:
        logger.error(f"Configuration file not found: {config_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing configuration: {e}")
        sys.exit(1)

    # Check if rsync is enabled
    if not config.get('rsync_enabled', False):
        logger.warning("rsync is not enabled in configuration. Enable it in MatchBox settings.")
        if once:
            sys.exit(0)
        else:
            logger.info("Waiting for rsync to be enabled...")

    # Set up signal handlers for graceful shutdown
    _ = signal.signal(signal.SIGINT, signal_handler)
    _ = signal.signal(signal.SIGTERM, signal_handler)

    interval = int(str(config.get('rsync_interval_seconds', 60)))

    if once:
        # Single sync mode
        logger.info("Running single sync...")
        success = run_rsync(config)
        sys.exit(0 if success else 1)

    # Continuous sync mode
    logger.info(f"Starting continuous sync with {interval} second interval")
    logger.info("Press Ctrl+C to stop")

    while not shutdown_requested:
        # Reload config each iteration to pick up changes
        try:
            config = load_config(config_path)
        except Exception as e:
            logger.warning(f"Could not reload config: {e}")

        if config.get('rsync_enabled', False):
            _ = run_rsync(config)
        else:
            logger.debug("rsync is disabled, skipping sync")

        # Update interval from config
        interval = int(str(config.get('rsync_interval_seconds', 60)))

        # Sleep with periodic checks for shutdown
        for _ in range(interval):
            if shutdown_requested:
                break
            time.sleep(1)

    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
