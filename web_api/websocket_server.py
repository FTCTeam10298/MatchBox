"""
WebSocket server for real-time log streaming, status updates, and OBS proxy.
Runs in its own daemon thread with its own asyncio event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import TYPE_CHECKING

import websockets.client
import websockets.server
import websockets.exceptions
from websockets.typing import Subprotocol

if TYPE_CHECKING:
    from matchbox import MatchBoxCore

logger = logging.getLogger("matchbox")

LOG_BUFFER_SIZE = 500


class WebSocketBroadcaster:
    """Manages WebSocket connections for logs, status, and OBS proxy."""

    def __init__(self, port: int, core: MatchBoxCore) -> None:
        self.port: int = port
        self._core: MatchBoxCore = core
        self._log_clients: set[websockets.server.WebSocketServerProtocol] = set()
        self._status_clients: set[websockets.server.WebSocketServerProtocol] = set()
        self._log_buffer: deque[dict[str, str]] = deque(maxlen=LOG_BUFFER_SIZE)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: websockets.server.WebSocketServer | None = None

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The event loop this server runs on (available after start)."""
        return self._loop

    def start(self) -> None:
        """Start the WebSocket server in a daemon thread"""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """Thread entry point: create event loop and run server"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            logger.error(f"WebSocket server error: {e}")

    async def _serve(self) -> None:
        """Start the WebSocket server"""
        try:
            self._server = await websockets.server.serve(
                self._handler,
                '0.0.0.0',
                self.port,
                subprotocols=[Subprotocol('obswebsocket.json')],
            )
            logger.info(f"WebSocket server listening on port {self.port}")
            await self._server.wait_closed()
        except OSError as e:
            if "Address already in use" in str(e):
                logger.error(f"WebSocket server port {self.port} is already in use")
            else:
                logger.error(f"WebSocket server error: {e}")

    async def _handler(self, websocket: websockets.server.WebSocketServerProtocol) -> None:
        """Route WebSocket connections by path"""
        path = websocket.path

        if path == '/ws/logs':
            await self._handle_logs(websocket)
        elif path == '/ws/status':
            await self._handle_status(websocket)
        elif path == '/ws/obs':
            await self._handle_obs_proxy(websocket)
        else:
            await websocket.close(4004, f"Unknown path: {path}")

    async def _handle_logs(self, websocket: websockets.server.WebSocketServerProtocol) -> None:
        """Handle log streaming WebSocket connection"""
        self._log_clients.add(websocket)
        try:
            # Send buffered logs
            for entry in self._log_buffer:
                await websocket.send(json.dumps(entry))

            # Keep connection alive until client disconnects
            async for _ in websocket:
                pass  # Client shouldn't send anything, just consume
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._log_clients.discard(websocket)

    async def _handle_status(self, websocket: websockets.server.WebSocketServerProtocol) -> None:
        """Handle status WebSocket connection"""
        self._status_clients.add(websocket)
        try:
            # Send current status immediately
            status = self._core.get_status()
            await websocket.send(json.dumps(status, default=str))

            # Keep connection alive
            async for _ in websocket:
                pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._status_clients.discard(websocket)

    async def _handle_obs_proxy(self, websocket: websockets.server.WebSocketServerProtocol) -> None:
        """Proxy WebSocket messages between client and OBS"""
        obs_host = self._core.config.obs_host
        obs_port = self._core.config.obs_port
        obs_url = f"ws://{obs_host}:{obs_port}"

        try:
            # Pass through the subprotocol requested by the client (obs-web uses obswebsocket.json)
            subprotocols = [Subprotocol(p) for p in websocket.request_headers.get_all('Sec-WebSocket-Protocol')]
            async with websockets.client.connect(
                obs_url,
                subprotocols=subprotocols or [Subprotocol('obswebsocket.json')],
            ) as obs_ws:
                async def client_to_obs() -> None:
                    async for message in websocket:
                        await obs_ws.send(message)

                async def obs_to_client() -> None:
                    async for message in obs_ws:
                        await websocket.send(message)

                _ = await asyncio.gather(client_to_obs(), obs_to_client())
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"OBS proxy error: {e}")
            try:
                await websocket.close(4002, f"OBS connection failed: {e}")
            except Exception:
                pass

    def broadcast_log(self, level: str, message: str) -> None:
        """Broadcast a log message to all connected log clients (thread-safe)"""
        entry = {
            'level': level,
            'message': message,
            'timestamp': time.strftime('%H:%M:%S'),
        }
        self._log_buffer.append(entry)

        if self._loop and self._log_clients:
            _ = asyncio.run_coroutine_threadsafe(
                self._broadcast_log_async(entry), self._loop
            )

    async def _broadcast_log_async(self, entry: dict[str, str]) -> None:
        """Send log entry to all connected log clients"""
        msg = json.dumps(entry)
        closed: set[websockets.server.WebSocketServerProtocol] = set()
        for ws in self._log_clients:
            try:
                await ws.send(msg)
            except websockets.exceptions.ConnectionClosed:
                closed.add(ws)
            except Exception:
                closed.add(ws)
        self._log_clients -= closed

    def broadcast_status(self, status: dict[str, object]) -> None:
        """Broadcast status update to all connected status clients (thread-safe)"""
        if self._loop and self._status_clients:
            _ = asyncio.run_coroutine_threadsafe(
                self._broadcast_status_async(status), self._loop
            )

    async def _broadcast_status_async(self, status: dict[str, object]) -> None:
        """Send status to all connected status clients"""
        msg = json.dumps(status, default=str)
        closed: set[websockets.server.WebSocketServerProtocol] = set()
        for ws in self._status_clients:
            try:
                await ws.send(msg)
            except websockets.exceptions.ConnectionClosed:
                closed.add(ws)
            except Exception:
                closed.add(ws)
        self._status_clients -= closed

    def stop(self) -> None:
        """Stop the WebSocket server"""
        if self._server:
            self._server.close()
        if self._loop:
            _ = self._loop.call_soon_threadsafe(self._loop.stop)
