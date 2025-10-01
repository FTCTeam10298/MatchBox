#!/usr/bin/env python3
"""
Local Video Processor for FIRST® MatchBox™
Handles real-time clipping from local OBS recording files
"""

import json
import time
import asyncio
import logging
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("local-video-processor")

class LocalVideoProcessor:
    """Process match clips from local OBS recording files"""

    def __init__(self, config=None):
        self.config = config or {}
        self.recording_path = None
        self.output_dir = Path(self.config.get('output_dir', './match_clips')).absolute()
        self.pre_match_buffer = self.config.get('pre_match_buffer_seconds', 10)
        self.post_match_buffer = self.config.get('post_match_buffer_seconds', 5)
        self.match_duration = self.config.get('match_duration_seconds', 158)  # FTC match: 30s auto + 8s transition + 120s teleop

        # Create output directory
        self.output_dir.mkdir(exist_ok=True, parents=True)

        # Recording monitoring
        self.recording_monitor_task = None
        self.is_monitoring = False
        self.last_file_size = 0
        self.file_growth_timestamps = []

    def set_recording_path(self, path: str):
        """Set the path to the OBS recording file"""
        self.recording_path = Path(path) if path else None
        if self.recording_path:
            logger.info(f"Set recording path: {self.recording_path}")
        else:
            logger.info("Recording path cleared")

    def is_recording_available(self) -> bool:
        """Check if recording file is available and growing"""
        if not self.recording_path or not self.recording_path.exists():
            return False

        try:
            # Check if file is growing (indicates active recording)
            current_size = self.recording_path.stat().st_size
            if current_size > self.last_file_size:
                self.last_file_size = current_size
                self.file_growth_timestamps.append(time.time())

                # Keep only recent timestamps (last 30 seconds)
                cutoff_time = time.time() - 30
                self.file_growth_timestamps = [
                    t for t in self.file_growth_timestamps if t > cutoff_time
                ]

                return len(self.file_growth_timestamps) > 0
            else:
                # No growth detected recently
                return len(self.file_growth_timestamps) > 0

        except Exception as e:
            logger.warning(f"Could not check recording availability: {e}")
            return False

    def get_recording_duration(self) -> float:
        """Get current duration of recording file in seconds"""
        if not self.recording_path or not self.recording_path.exists():
            return 0.0

        try:
            # Use ffprobe to get duration
            cmd = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_entries', 'format=duration',
                str(self.recording_path)
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                duration = float(data['format']['duration'])
                return duration
            else:
                logger.warning(f"ffprobe failed: {result.stderr}")
                return 0.0

        except Exception as e:
            logger.warning(f"Could not get recording duration: {e}")
            return 0.0

    def calculate_clip_times(self, match_start_time: datetime) -> tuple:
        """Calculate clip start and end times with buffers"""
        # Convert match start time to seconds from recording start
        recording_start = self.get_recording_start_time()
        if not recording_start:
            # Fallback: use current recording duration as estimate
            current_duration = self.get_recording_duration()
            match_offset_seconds = max(0, current_duration - 30)  # Estimate recent match
        else:
            match_offset_seconds = (match_start_time - recording_start).total_seconds()

        # Calculate clip boundaries
        clip_start = max(0, match_offset_seconds - self.pre_match_buffer)
        clip_duration = self.pre_match_buffer + self.match_duration + self.post_match_buffer

        return clip_start, clip_duration

    def get_recording_start_time(self) -> Optional[datetime]:
        """Estimate recording start time from file metadata"""
        if not self.recording_path or not self.recording_path.exists():
            return None

        try:
            # Use file creation time as approximation
            stat_result = self.recording_path.stat()
            return datetime.fromtimestamp(stat_result.st_ctime)
        except Exception as e:
            logger.warning(f"Could not get recording start time: {e}")
            return None

    async def extract_clip(self, match_info: Dict[str, Any]) -> Optional[Path]:
        """Extract a match clip from the local recording"""
        if not self.is_recording_available():
            logger.warning("Recording file not available for clipping")
            return None

        try:
            # Get match timing information
            match_start_time = self.parse_match_time(match_info)
            clip_start, clip_duration = self.calculate_clip_times(match_start_time)

            # Generate output filename
            match_name = self.generate_match_filename(match_info)
            output_path = self.output_dir / f"{match_name}.mp4"

            # Ensure we don't overwrite existing clips
            counter = 1
            while output_path.exists():
                output_path = self.output_dir / f"{match_name}_{counter}.mp4"
                counter += 1

            logger.info(f"Extracting clip: {clip_start:.1f}s + {clip_duration:.1f}s -> {output_path}")

            # Extract clip using FFmpeg
            success = await self.extract_clip_ffmpeg(
                input_path=self.recording_path,
                output_path=output_path,
                start_time=clip_start,
                duration=clip_duration
            )

            if success and output_path.exists():
                logger.info(f"✅ Successfully created clip: {output_path}")
                return output_path
            else:
                logger.error(f"❌ Failed to create clip: {output_path}")
                return None

        except Exception as e:
            logger.error(f"Error extracting clip: {e}")
            return None

    async def extract_clip_ffmpeg(self, input_path: Path, output_path: Path,
                                start_time: float, duration: float) -> bool:
        """Extract clip using FFmpeg"""
        try:
            cmd = [
                'ffmpeg', '-y',  # Overwrite output files
                '-i', str(input_path),
                '-ss', str(start_time),
                '-t', str(duration),
                '-c', 'copy',  # Copy streams without re-encoding for speed
                '-avoid_negative_ts', 'make_zero',
                str(output_path)
            ]

            logger.info(f"Running FFmpeg: {' '.join(cmd)}")

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                logger.info("FFmpeg completed successfully")
                return True
            else:
                logger.error(f"FFmpeg failed: {stderr.decode()}")
                return False

        except Exception as e:
            logger.error(f"Error running FFmpeg: {e}")
            return False

    def parse_match_time(self, match_info: Dict[str, Any]) -> datetime:
        """Parse match start time from match info"""
        # Try to extract start timestamp from match info
        if 'start_timestamp' in match_info:
            if isinstance(match_info['start_timestamp'], datetime):
                return match_info['start_timestamp']
            elif isinstance(match_info['start_timestamp'], (int, float)):
                return datetime.fromtimestamp(match_info['start_timestamp'])

        # Fallback to legacy timestamp field
        if 'timestamp' in match_info:
            if isinstance(match_info['timestamp'], datetime):
                return match_info['timestamp']
            elif isinstance(match_info['timestamp'], (int, float)):
                return datetime.fromtimestamp(match_info['timestamp'])

        # Final fallback: use current time
        return datetime.now()

    def generate_match_filename(self, match_info: Dict[str, Any]) -> str:
        """Generate filename for match clip"""
        timestamp = self.parse_match_time(match_info)
        time_str = timestamp.strftime("%Y%m%d %H%M%S")

        # Extract match details if available
        match_name = match_info.get('matchName', 'Match_unknown')
        field_number = match_info.get('field', 'unknown')

        return f"{match_name} - Field {field_number} - {time_str}"

    def start_monitoring(self):
        """Start monitoring recording file for growth"""
        if self.is_monitoring:
            return

        self.is_monitoring = True
        self.recording_monitor_task = asyncio.create_task(self._monitor_recording())
        logger.info("Started recording file monitoring")

    def stop_monitoring(self):
        """Stop monitoring recording file"""
        self.is_monitoring = False
        if self.recording_monitor_task:
            self.recording_monitor_task.cancel()
            self.recording_monitor_task = None
        logger.info("Stopped recording file monitoring")

    async def _monitor_recording(self):
        """Internal recording monitoring loop"""
        while self.is_monitoring:
            try:
                if self.recording_path and self.recording_path.exists():
                    current_size = self.recording_path.stat().st_size
                    if current_size != self.last_file_size:
                        logger.debug(f"Recording file grew: {self.last_file_size} -> {current_size}")
                        self.last_file_size = current_size
                        self.file_growth_timestamps.append(time.time())

                await asyncio.sleep(5)  # Check every 5 seconds

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Error monitoring recording file: {e}")
                await asyncio.sleep(5)


# Test functionality
async def test_local_processor():
    """Test function for local video processor"""
    config = {
        'output_dir': './test_clips',
        'pre_match_buffer_seconds': 10,
        'post_match_buffer_seconds': 5
    }

    processor = LocalVideoProcessor(config)

    # Test with a dummy recording file (replace with actual path)
    test_recording = Path("./test_recording.mp4")
    if test_recording.exists():
        processor.set_recording_path(str(test_recording))

        # Test clip extraction
        match_info = {
            'match_name': 'Match Q1',
            'field': 1,
            'timestamp': datetime.now()
        }

        clip_path = await processor.extract_clip(match_info)
        if clip_path:
            print(f"✅ Test clip created: {clip_path}")
        else:
            print("❌ Test clip failed")
    else:
        print(f"Test recording file not found: {test_recording}")


if __name__ == "__main__":
    # Run test if called directly
    asyncio.run(test_local_processor())