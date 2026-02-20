"""
WebSocket tunnel client for MatchBox remote access.

Connects to a relay server via WebSocket and proxies HTTP/WS requests
back to the local MatchBox HTTP and WebSocket servers.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import json
import logging
from typing import TYPE_CHECKING, Any

import websockets.client
import websockets.exceptions

if TYPE_CHECKING:
    from matchbox import MatchBoxConfig

logger = logging.getLogger("matchbox")


class WSTunnelClient:
    """Connects to a relay server and proxies requests back to local MatchBox."""

    def __init__(self, config: MatchBoxConfig) -> None:
        self.config = config
        self._ws: Any = None
        self._connected: bool = False
        self._running: bool = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._local_ws_connections: dict[str, Any] = {}  # id -> websocket

    def is_connected(self) -> bool:
        return self._connected

    def start(self, loop: asyncio.AbstractEventLoop) -> bool:
        """Start the tunnel client on the given event loop. Returns True if started."""
        url = self.config.tunnel_relay_url
        if not url:
            logger.error("Tunnel: No relay URL configured")
            return False

        self._loop = loop
        self._running = True
        asyncio.run_coroutine_threadsafe(self._connect_loop(), loop)
        logger.info(f"Tunnel: Connecting to relay {url}")
        return True

    def stop(self) -> None:
        """Stop the tunnel client."""
        self._running = False
        self._connected = False

        # Close all local WS connections
        for ws in list(self._local_ws_connections.values()):
            try:
                if self._loop:
                    asyncio.run_coroutine_threadsafe(ws.close(), self._loop)
            except Exception:
                pass
        self._local_ws_connections.clear()

        # Close tunnel WS
        if self._ws and self._loop:
            try:
                asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
            except Exception:
                pass
            self._ws = None

        logger.info("Tunnel: Stopped")

    async def _connect_loop(self) -> None:
        """Connect to relay with auto-reconnect."""
        retry_delay = 5

        while self._running:
            try:
                url = self.config.tunnel_relay_url
                if not url:
                    await asyncio.sleep(retry_delay)
                    continue

                # Normalize URL to ws:// or wss://
                ws_url = url.rstrip('/')
                if ws_url.startswith('http://'):
                    ws_url = 'ws://' + ws_url[7:]
                elif ws_url.startswith('https://'):
                    ws_url = 'wss://' + ws_url[8:]
                elif not ws_url.startswith(('ws://', 'wss://')):
                    ws_url = 'ws://' + ws_url

                if not ws_url.endswith('/tunnel'):
                    ws_url += '/tunnel'

                async with websockets.client.connect(ws_url) as ws:
                    self._ws = ws

                    # Send registration
                    from matchbox import ADMIN_SALT, ADMIN_HASH
                    await ws.send(json.dumps({
                        'type': 'register',
                        'event_code': self.config.event_code or 'default',
                        'password': self.config.tunnel_password,
                        'allow_admin': self.config.tunnel_allow_admin,
                        'admin_hash': ADMIN_HASH,
                        'admin_salt': ADMIN_SALT.hex(),
                    }))

                    # Wait for registration response
                    resp = json.loads(await ws.recv())
                    if resp.get('type') == 'error':
                        logger.error(f"Tunnel: Registration failed: {resp.get('message')}")
                        self._running = False
                        return

                    if resp.get('type') == 'registered':
                        instance_id = resp.get('instance_id', '')
                        self._connected = True
                        retry_delay = 5
                        logger.info(f"Tunnel: Connected (instance: {instance_id})")

                        # Notify status change
                        from matchbox import gui_handler
                        if gui_handler.ws_broadcaster:
                            try:
                                # Import core to trigger status broadcast
                                pass
                            except Exception:
                                pass

                    # Message loop
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            msg_type = msg.get('type', '')

                            if msg_type == 'http_request':
                                asyncio.ensure_future(self._handle_http_request(msg))
                            elif msg_type == 'ws_open':
                                asyncio.ensure_future(self._handle_ws_open(msg))
                            elif msg_type == 'ws_data':
                                await self._handle_ws_data(msg)
                            elif msg_type == 'ws_close':
                                await self._handle_ws_close(msg)
                            elif msg_type == 'error':
                                logger.error(f"Tunnel: Relay error: {msg.get('message')}")
                        except Exception as e:
                            logger.error(f"Tunnel: Error handling message: {e}")

            except websockets.exceptions.ConnectionClosed:
                pass
            except Exception as e:
                if self._running:
                    logger.warning(f"Tunnel: Connection error: {e}")

            self._connected = False
            self._ws = None

            # Clean up local WS connections
            for ws_conn in list(self._local_ws_connections.values()):
                try:
                    await ws_conn.close()
                except Exception:
                    pass
            self._local_ws_connections.clear()

            if self._running:
                logger.info(f"Tunnel: Reconnecting in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

    async def _handle_http_request(self, msg: dict[str, Any]) -> None:
        """Proxy an HTTP request to the local web server."""
        req_id = msg['id']
        method = msg.get('method', 'GET')
        path = msg.get('path', '/')
        headers = msg.get('headers', {})
        body_b64 = msg.get('body', '')

        try:
            body = base64.b64decode(body_b64) if body_b64 else None

            # Use http.client to hit the local server
            loop = asyncio.get_event_loop()
            status, resp_headers, resp_body = await loop.run_in_executor(
                None, self._do_http_request, method, path, headers, body
            )

            # Send response back through tunnel
            if self._ws:
                await self._ws.send(json.dumps({
                    'type': 'http_response',
                    'id': req_id,
                    'status': status,
                    'headers': resp_headers,
                    'body': base64.b64encode(resp_body).decode('ascii'),
                }))

        except Exception as e:
            logger.error(f"Tunnel: HTTP proxy error: {e}")
            if self._ws:
                try:
                    await self._ws.send(json.dumps({
                        'type': 'http_response',
                        'id': req_id,
                        'status': 502,
                        'headers': {'Content-Type': 'text/plain'},
                        'body': base64.b64encode(f"Tunnel proxy error: {e}".encode()).decode('ascii'),
                    }))
                except Exception:
                    pass

    def _do_http_request(
        self, method: str, path: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, dict[str, str], bytes]:
        """Execute HTTP request to local server (runs in executor)."""
        conn = http.client.HTTPConnection('127.0.0.1', self.config.web_port, timeout=30)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_headers = {k: v for k, v in resp.getheaders()}
            resp_body = resp.read()
            return resp.status, resp_headers, resp_body
        finally:
            conn.close()

    async def _handle_ws_open(self, msg: dict[str, Any]) -> None:
        """Open a local WebSocket connection for a proxied browser WS."""
        ws_id = msg['id']
        path = msg.get('path', '/ws/status')
        subprotocols = msg.get('subprotocols', [])

        ws_port = self.config.web_port + 1
        local_url = f"ws://127.0.0.1:{ws_port}{path}"

        logger.info(f"Tunnel: opening local WS to {local_url} (id={ws_id[:8]}, subprotocols={subprotocols})")

        try:
            local_ws = await websockets.client.connect(
                local_url,
                subprotocols=subprotocols or None,
            )
            self._local_ws_connections[ws_id] = local_ws
            logger.info(f"Tunnel: local WS connected (id={ws_id[:8]}, subprotocol={local_ws.subprotocol})")

            if self._ws:
                await self._ws.send(json.dumps({
                    'type': 'ws_opened',
                    'id': ws_id,
                }))

            # Spawn task to forward local WS → tunnel
            asyncio.ensure_future(self._bridge_local_ws(ws_id, local_ws))

        except Exception as e:
            logger.error(f"Tunnel: WS proxy open error: {e}")
            if self._ws:
                try:
                    await self._ws.send(json.dumps({
                        'type': 'ws_error',
                        'id': ws_id,
                        'message': str(e),
                    }))
                except Exception:
                    pass

    async def _bridge_local_ws(self, ws_id: str, local_ws: Any) -> None:
        """Forward messages from local WS to tunnel."""
        try:
            async for message in local_ws:
                if self._ws and ws_id in self._local_ws_connections:
                    logger.debug(f"Tunnel: local→tunnel WS (id={ws_id[:8]}, {len(message) if isinstance(message, (str, bytes)) else '?'} chars)")
                    await self._ws.send(json.dumps({
                        'type': 'ws_data',
                        'id': ws_id,
                        'data': message if isinstance(message, str) else base64.b64encode(message).decode('ascii'),
                    }))
        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"Tunnel: local WS closed (id={ws_id[:8]}): {e}")
        except Exception as e:
            logger.warning(f"Tunnel: WS bridge error (id={ws_id[:8]}): {e}")
        finally:
            logger.info(f"Tunnel: WS bridge ended, sending ws_close (id={ws_id[:8]})")
            self._local_ws_connections.pop(ws_id, None)
            if self._ws:
                try:
                    await self._ws.send(json.dumps({
                        'type': 'ws_close',
                        'id': ws_id,
                    }))
                except Exception:
                    pass

    async def _handle_ws_data(self, msg: dict[str, Any]) -> None:
        """Forward data from tunnel to local WS."""
        ws_id = msg['id']
        data = msg.get('data', '')
        local_ws = self._local_ws_connections.get(ws_id)
        if local_ws:
            try:
                await local_ws.send(data)
            except Exception:
                self._local_ws_connections.pop(ws_id, None)

    async def _handle_ws_close(self, msg: dict[str, Any]) -> None:
        """Close a local WS connection."""
        ws_id = msg['id']
        local_ws = self._local_ws_connections.pop(ws_id, None)
        if local_ws:
            try:
                await local_ws.close()
            except Exception:
                pass
