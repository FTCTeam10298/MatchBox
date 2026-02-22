#!/usr/bin/env python3
"""
Download match clips from a remote MatchBox instance via the relay server.

Usage:
    python download_clips.py --url https://jacobspctuneup.tk/FTC/MatchBox/admin/<instance_id> --password <password>
    python download_clips.py --url https://jacobspctuneup.tk/FTC/MatchBox/admin/<instance_id> --password <password> --loop 60
"""

import argparse
import sys
import time
from pathlib import Path
from typing import cast
from urllib.parse import quote

import requests


def authenticate(session: requests.Session, base_url: str, password: str) -> bool:
    """Authenticate and get a session cookie."""
    auth_url = base_url.rstrip('/') + '/_auth'
    resp = session.post(auth_url, data={'password': password}, allow_redirects=False)
    # Successful auth sets a cookie and returns 302
    if resp.status_code == 302 and 'mb_session' in session.cookies.get_dict():
        return True
    # Some setups might return 200
    if 'mb_session' in session.cookies.get_dict():
        return True
    return False


def get_clips_list(session: requests.Session, base_url: str) -> list[dict[str, object]]:
    """Fetch the list of available clips."""
    api_url = base_url.rstrip('/') + '/api/clips'
    resp = session.get(api_url)
    resp.raise_for_status()
    return cast(list[dict[str, object]], resp.json())


def download_clip(session: requests.Session, base_url: str, filename: str, output_dir: Path) -> bool:
    """Download a single clip file. Returns True if downloaded, False if skipped."""
    output_path = output_dir / filename
    if output_path.exists():
        return False  # Already downloaded

    clip_url = base_url.rstrip('/') + '/' + quote(filename)
    print(f"  Downloading {filename}...", end=' ', flush=True)

    resp = session.get(clip_url, stream=True)
    resp.raise_for_status()

    # Download to temp file first, then rename
    tmp_path = output_path.with_suffix(output_path.suffix + '.tmp')
    size = 0
    with open(tmp_path, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=8192):  # pyright: ignore[reportAny]
            chunk_bytes: bytes = chunk if isinstance(chunk, bytes) else b''
            _ = f.write(chunk_bytes)
            size += len(chunk_bytes)

    _ = tmp_path.rename(output_path)
    print(f"{size / (1024*1024):.1f} MB")
    return True


def download_index(session: requests.Session, base_url: str, output_dir: Path) -> None:
    """Download the index.html clip listing page (always overwritten)."""
    index_url = base_url.rstrip('/') + '/'
    resp = session.get(index_url)
    resp.raise_for_status()
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(output_dir / 'index.html', 'wb') as f:
        _ = f.write(resp.content)


def sync_clips(session: requests.Session, base_url: str, output_dir: Path) -> int:
    """Download all new clips and index.html. Returns count of newly downloaded clips."""
    output_dir.mkdir(parents=True, exist_ok=True)

    clips = get_clips_list(session, base_url)
    downloaded = 0

    for clip in clips:
        name = str(clip.get('name', ''))
        if not name or name.endswith('.partial'):
            continue
        if download_clip(session, base_url, name, output_dir):
            downloaded += 1

    if not clips:
        print("No clips available yet.")

    # Always update index.html to reflect current clip list
    download_index(session, base_url, output_dir)

    return downloaded


def main() -> None:
    parser = argparse.ArgumentParser(description="Download clips from a remote MatchBox instance")
    _ = parser.add_argument("--url", required=True,
                            help="Base URL of the MatchBox instance (relay URL including instance ID)")
    _ = parser.add_argument("--password", required=True,
                            help="Browser password for authentication")
    _ = parser.add_argument("--output", "-o", default="./downloaded_clips",
                            help="Output directory (default: ./downloaded_clips)")
    _ = parser.add_argument("--loop", type=int, default=0,
                            help="Re-check every N seconds (0 = run once and exit)")

    args = parser.parse_args()
    base_url: str = args.url  # pyright: ignore[reportAny]
    password: str = args.password  # pyright: ignore[reportAny]
    output_dir = Path(str(args.output))  # pyright: ignore[reportAny]
    loop_interval: int = args.loop  # pyright: ignore[reportAny]

    session = requests.Session()

    print(f"Authenticating to {base_url}...")
    if not authenticate(session, base_url, password):
        print("Authentication failed. Check your password and URL.")
        sys.exit(1)
    print("Authenticated successfully.")

    while True:
        print(f"\nChecking for clips...")
        try:
            downloaded = sync_clips(session, base_url, output_dir)
            total = len(list(output_dir.iterdir())) if output_dir.exists() else 0
            print(f"Done. {downloaded} new clips downloaded, {total} total in {output_dir}")
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 302:
                # Session expired, re-authenticate
                print("Session expired, re-authenticating...")
                if authenticate(session, base_url, password):
                    continue
                else:
                    print("Re-authentication failed.")
                    sys.exit(1)
            else:
                print(f"Error: {e}")
        except Exception as e:
            print(f"Error: {e}")

        if loop_interval <= 0:
            break

        print(f"Next check in {loop_interval}s...")
        try:
            time.sleep(loop_interval)
        except KeyboardInterrupt:
            print("\nStopped.")
            break


if __name__ == "__main__":
    main()
