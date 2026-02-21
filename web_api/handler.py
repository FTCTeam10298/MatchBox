"""
Extended HTTP handler with REST API routes and admin UI static file serving.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import logging
import time
from http import cookies
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
import socket
from socketserver import BaseServer
from typing import TYPE_CHECKING, cast, override
from urllib.parse import urlparse, parse_qs

if TYPE_CHECKING:
    from matchbox import MatchBoxCore

logger = logging.getLogger("matchbox")

_LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MatchBox Login</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 400px; margin: 80px auto; padding: 0 20px; background: #1e1e1e; color: #ddd; }}
        h1 {{ color: #fff; text-align: center; }}
        form {{ background: #272727; padding: 24px; border-radius: 12px; border: 1px solid #444; }}
        label {{ display: block; margin-bottom: 6px; font-weight: 600; }}
        input[type=password] {{ width: 100%; padding: 10px; border: 1px solid #444; border-radius: 6px; background: #363636; color: #ddd; box-sizing: border-box; font-size: 16px; }}
        input[type=password]:focus {{ outline: none; border-color: #4a9eff; }}
        button {{ width: 100%; padding: 10px; margin-top: 16px; border: none; border-radius: 6px; background: #4a9eff; color: #fff; font-weight: 600; font-size: 16px; cursor: pointer; }}
        button:hover {{ background: #6bb3ff; }}
        .error {{ color: #f44336; margin-bottom: 12px; text-align: center; }}
    </style>
</head>
<body>
    <h1>MatchBox</h1>
    <form method="POST" action="/admin/_auth">
        {error}
        <label for="password">Password</label>
        <input type="password" name="password" id="password" autofocus>
        <button type="submit">Log In</button>
    </form>
</body>
</html>"""


def get_web_admin_dir() -> Path:
    """Get path to web_admin static files directory"""
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', None)
        if isinstance(meipass, str):
            return Path(meipass) / 'web_admin'
    return Path(__file__).parent.parent / 'web_admin'


def make_admin_handler(clips_directory: str, core: MatchBoxCore) -> type[SimpleHTTPRequestHandler]:
    """Create an HTTP handler class with REST API and admin UI support.

    Args:
        clips_directory: Path to the clips directory for serving video files
        core: MatchBoxCore instance for API access
    """

    class AdminHandler(SimpleHTTPRequestHandler):
        _core: MatchBoxCore = core
        _clips_dir: str = clips_directory
        _web_admin_dir: str = str(get_web_admin_dir())
        path: str

        def __init__(self, request: socket.socket, client_address: tuple[str, int], server: BaseServer) -> None:
            super().__init__(request, client_address, server, directory=self._clips_dir)

        @override
        def log_message(self, format: str, *args: object) -> None:
            message = format % args
            if "Broken pipe" not in message and "Connection reset" not in message:
                print(f"HTTP: {message}")

        @override
        def address_string(self) -> str:
            return str(self.client_address[0])

        @override
        def end_headers(self) -> None:
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Cache-Control', 'no-cache')
            super().end_headers()

        def send_json(self, data: object, status: int = 200) -> None:
            """Send a JSON response"""
            body = json.dumps(data, default=str).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            _ = self.wfile.write(body)

        def read_body(self) -> dict[str, object]:
            """Read and parse JSON request body"""
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                return {}
            body = self.rfile.read(content_length)
            return cast(dict[str, object], json.loads(body))

        def _get_session_secret(self) -> bytes:
            """Derive a session signing secret from config."""
            key = self._core.config.tunnel_password or 'matchbox'
            return hmac.new(key.encode(), b'session-key', hashlib.sha256).digest()

        def _make_session_cookie(self) -> str:
            """Create an HMAC-signed session cookie value."""
            instance_id = '_local'
            expiry = int(time.time()) + 86400  # 24 hours
            payload = f"{instance_id}:{expiry}"
            sig = hmac.new(self._get_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
            return f"{instance_id}:{expiry}:{sig}"

        def _check_auth(self) -> bool:
            """Check if the request has a valid session cookie or is from localhost tunnel."""
            # Trust requests from localhost (tunnel client proxies relay-authenticated requests)
            if self.client_address[0] == '127.0.0.1':
                return True
            cookie_header = self.headers.get('Cookie', '')
            if not cookie_header:
                return False
            # Parse cookies
            c = cookies.SimpleCookie()
            try:
                c.load(cookie_header)
            except cookies.CookieError:
                return False
            morsel = c.get('mb_session')
            if not morsel:
                return False
            value = morsel.value
            parts = value.split(':')
            if len(parts) != 3:
                return False
            instance_id, expiry_str, sig = parts
            try:
                expiry = int(expiry_str)
            except ValueError:
                return False
            if time.time() > expiry:
                return False
            expected = hmac.new(self._get_session_secret(), f"{instance_id}:{expiry_str}".encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, expected)

        def _check_password(self, password: str) -> bool:
            """Check if password matches instance password or admin password."""
            from matchbox import ADMIN_SALT, ADMIN_HASH
            # Check instance password
            if self._core.config.tunnel_password and password == self._core.config.tunnel_password:
                return True
            # Check admin password (if allowed)
            if self._core.config.tunnel_allow_admin:
                if hashlib.sha256(ADMIN_SALT + password.encode()).hexdigest() == ADMIN_HASH:
                    return True
            return False

        def _send_login_page(self, error: str = '') -> None:
            """Serve the login page."""
            error_html = f'<p class="error">{error}</p>' if error else ''
            body = _LOGIN_HTML.format(error=error_html).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            _ = self.wfile.write(body)

        def _send_redirect(self, location: str, cookie: str | None = None) -> None:
            """Send a 302 redirect, optionally setting a cookie."""
            self.send_response(302)
            self.send_header('Location', location)
            if cookie:
                self.send_header('Set-Cookie', f'mb_session={cookie}; Path=/; Max-Age=86400; HttpOnly; SameSite=Lax')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_OPTIONS(self) -> None:
            """Handle CORS preflight"""
            self.send_response(204)
            self.end_headers()

        @override
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            # Login page (unauthenticated)
            if path == '/admin/_login':
                self._send_login_page()
                return

            # Auth check for /admin/*, /api/*, and /obs-web/*
            if path.startswith('/admin') or path.startswith('/api/') or path.startswith('/obs-web'):
                if not self._check_auth():
                    self._send_redirect('/admin/_login')
                    return

            # API routes
            if path == '/api/status':
                self.send_json(self._core.get_status())
                return

            if path == '/api/config':
                self.send_json(self._core.get_config_dict())
                return

            if path == '/api/clips':
                clips: list[dict[str, object]] = []
                for f in self._core.scan_video_files():
                    clips.append({
                        'name': f.name,
                        'size': f.stat().st_size,
                        'mtime': f.stat().st_mtime,
                    })
                self.send_json(clips)
                return

            # obs-web static files (served at root /obs-web/ for iframe)
            if path == '/obs-web' or path == '/obs-web/':
                self.path = '/obs-web/index.html'
                return self._serve_admin_static()

            if path.startswith('/obs-web/'):
                self.path = path  # Keep full path, served from web_admin dir
                return self._serve_admin_static()

            # Admin UI static files
            if path == '/admin' or path == '/admin/':
                self.path = '/index.html'
                return self._serve_admin_static()

            if path.startswith('/admin/'):
                self.path = path[6:]  # Strip /admin prefix
                return self._serve_admin_static()

            # Favicon for clips page
            if path == '/favicon.ico':
                self.path = '/favicon.ico'
                return self._serve_admin_static()

            # Fall through to clip-serving with range request support
            self._serve_clip_file()

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            # Auth endpoint (unauthenticated)
            if path == '/admin/_auth':
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8') if content_length else ''
                params = parse_qs(body)
                password = params.get('password', [''])[0]
                if self._check_password(password):
                    cookie = self._make_session_cookie()
                    self._send_redirect('/admin/', cookie)
                else:
                    self._send_login_page('Invalid password')
                return

            # Auth check for /api/*
            if path.startswith('/api/'):
                if not self._check_auth():
                    self.send_json({'error': 'Unauthorized'}, 401)
                    return

            if path == '/api/start':
                if self._core.running:
                    self.send_json({'ok': False, 'error': 'Already running'}, 400)
                else:
                    if not self._core.config.event_code:
                        self.send_json({'ok': False, 'error': 'Event code is required'}, 400)
                        return
                    # Update clips dir in case config changed
                    from pathlib import Path as _Path
                    self._core.clips_dir = _Path(self._core.config.output_dir).absolute() / self._core.config.event_code
                    self._core.clips_dir.mkdir(exist_ok=True, parents=True)
                    # Start monitoring in a background thread
                    import asyncio
                    import threading

                    def _run() -> None:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(self._core.monitor_ftc_websocket())

                    t = threading.Thread(target=_run, daemon=True)
                    t.start()
                    self.send_json({'ok': True})
                return

            if path == '/api/stop':
                if self._core.running:
                    # Just set running=False; the monitor loop will exit
                    # and its finally block handles cleanup via stop_monitoring()
                    self._core.running = False
                    self._core.notify_status_change()
                    self.send_json({'ok': True})
                else:
                    self.send_json({'ok': False, 'error': 'Not running'}, 400)
                return

            if path == '/api/configure-obs':
                result = self._core.configure_obs_scenes()
                self.send_json({'ok': result})
                return

            if path == '/api/sync/start':
                try:
                    if self._core.start_sync():
                        self.send_json({'ok': True})
                    else:
                        self.send_json({'ok': False, 'error': 'Check logs for details (missing host/module or already running)'}, 400)
                except Exception as e:
                    self.send_json({'ok': False, 'error': str(e)}, 500)
                return

            if path == '/api/sync/stop':
                try:
                    self._core.stop_sync()
                    self.send_json({'ok': True})
                except Exception as e:
                    self.send_json({'ok': False, 'error': str(e)}, 500)
                return

            if path == '/api/config':
                try:
                    data = self.read_body()
                    self._core.update_config(data)
                    logger.info("Configuration updated via web API")
                    self.send_json({'ok': True})
                except Exception as e:
                    logger.error(f"Error updating configuration: {e}")
                    self.send_json({'ok': False, 'error': str(e)}, 400)
                return

            if path == '/api/save-config':
                try:
                    # Import here to avoid circular imports
                    from matchbox import get_config_path
                    config_path = get_config_path()
                    with open(config_path, "w") as f:
                        json.dump(vars(self._core.config), f, indent=2)
                    logger.info(f"Configuration saved to {config_path}")
                    self.send_json({'ok': True})
                except Exception as e:
                    logger.error(f"Error saving configuration: {e}")
                    self.send_json({'ok': False, 'error': str(e)}, 500)
                return

            if path == '/api/tunnel/start':
                try:
                    if self._core.start_tunnel():
                        self.send_json({'ok': True})
                    else:
                        self.send_json({'ok': False, 'error': 'Check logs for details (missing host or already running)'}, 400)
                except Exception as e:
                    self.send_json({'ok': False, 'error': str(e)}, 500)
                return

            if path == '/api/tunnel/stop':
                try:
                    self._core.stop_tunnel()
                    self.send_json({'ok': True})
                except Exception as e:
                    self.send_json({'ok': False, 'error': str(e)}, 500)
                return

            self.send_json({'error': 'Not found'}, 404)

        def do_PUT(self) -> None:
            # Treat PUT same as POST for API
            self.do_POST()

        def _serve_admin_static(self) -> None:
            """Serve static files from web_admin directory"""
            # Sanitize path
            rel_path = self.path.lstrip('/')
            if not rel_path:
                rel_path = 'index.html'

            file_path = os.path.join(self._web_admin_dir, rel_path)
            file_path = os.path.realpath(file_path)

            # Security: ensure we stay within web_admin dir
            if not file_path.startswith(os.path.realpath(self._web_admin_dir)):
                self.send_error(403, "Forbidden")
                return

            if not os.path.isfile(file_path):
                self.send_error(404, "File not found")
                return

            # Determine content type
            content_type = self.guess_type(file_path)
            try:
                file_size = os.path.getsize(file_path)
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(file_size))
                self.end_headers()
                with open(file_path, 'rb') as f:
                    _ = self.wfile.write(f.read())
            except Exception:
                self.send_error(500, "Internal server error")

        def _serve_clip_file(self) -> None:
            """Serve clip files with range request support (original behavior)"""
            path = self.translate_path(self.path)

            if not os.path.isfile(path):
                return super().do_GET()

            try:
                file_size = os.path.getsize(path)
            except OSError:
                self.send_error(404, "File not found")
                return

            content_type = self.guess_type(path)
            range_header = self.headers.get('Range')

            if range_header:
                try:
                    range_match = range_header.replace('bytes=', '').split('-')
                    start = int(range_match[0]) if range_match[0] else 0
                    end = int(range_match[1]) if range_match[1] else file_size - 1

                    if start >= file_size or end >= file_size or start > end:
                        self.send_error(416, "Requested Range Not Satisfiable")
                        self.send_header('Content-Range', f'bytes */{file_size}')
                        self.end_headers()
                        return

                    self.send_response(206)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
                    self.send_header('Content-Length', str(end - start + 1))
                    self.send_header('Accept-Ranges', 'bytes')
                    self.end_headers()

                    with open(path, 'rb') as f:
                        _ = f.seek(start)
                        bytes_to_send = end - start + 1
                        chunk_size = 8192
                        while bytes_to_send > 0:
                            chunk = f.read(min(chunk_size, bytes_to_send))
                            if not chunk:
                                break
                            _ = self.wfile.write(chunk)
                            bytes_to_send -= len(chunk)

                except (ValueError, IndexError):
                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', str(file_size))
                    self.send_header('Accept-Ranges', 'bytes')
                    self.end_headers()
                    with open(path, 'rb') as f:
                        _ = self.wfile.write(f.read())
            else:
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(file_size))
                self.send_header('Accept-Ranges', 'bytes')
                self.end_headers()
                with open(path, 'rb') as f:
                    chunk_size = 8192
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        _ = self.wfile.write(chunk)

        @override
        def handle_one_request(self) -> None:
            """Handle a single HTTP request with better error handling"""
            import time
            start_time = time.time()
            try:
                super().handle_one_request()
                duration = time.time() - start_time
                if duration > 1.0:
                    print(f"Slow HTTP request took {duration:.2f}s")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                logger.error(f"Request handling error: {e}")

    return AdminHandler
