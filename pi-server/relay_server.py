#!/usr/bin/env python3
"""
MatchBox Relay Server

Standalone aiohttp server that accepts WebSocket tunnel connections from
MatchBox instances and proxies browser HTTP/WS requests through the tunnel.

Usage:
    pip install aiohttp
    python relay_server.py --token <shared-secret> [--port 8080] [--base-path /FTC/MatchBox]

Example nginx config:
    location /FTC/MatchBox/ {
        proxy_pass http://127.0.0.1:8080/FTC/MatchBox/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
"""

import argparse
import asyncio
import base64
import json
import logging
import time
import uuid
from typing import cast

from aiohttp import web, WSMsgType

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger("relay")


class TunnelInstance:
    """Represents a connected MatchBox instance."""

    def __init__(self, ws: web.WebSocketResponse, event_code: str, instance_id: str) -> None:
        self.ws: web.WebSocketResponse = ws
        self.event_code: str = event_code
        self.instance_id: str = instance_id
        self.connected_at: float = time.time()
        self.pending_http: dict[str, asyncio.Future[dict[str, object]]] = {}
        self.browser_ws_connections: dict[str, web.WebSocketResponse] = {}


class RelayServer:
    """Manages tunnel connections and proxies requests."""

    def __init__(self, token: str, base_path: str) -> None:
        self.token: str = token
        self.base_path: str = base_path  # e.g. "/FTC/MatchBox" or ""
        self.instances: dict[str, TunnelInstance] = {}  # instance_id -> TunnelInstance
        self.id_by_event: dict[str, str] = {}  # event_code -> instance_id

    def get_instance_by_event(self, event_code: str) -> TunnelInstance | None:
        instance_id = self.id_by_event.get(event_code)
        if instance_id:
            return self.instances.get(instance_id)
        return None

    async def handle_dashboard(self, _request: web.Request) -> web.Response:
        """Serve dashboard listing connected instances."""
        instances_html = ""
        for inst in self.instances.values():
            uptime = int(time.time() - inst.connected_at)
            minutes, seconds = divmod(uptime, 60)
            hours, minutes = divmod(minutes, 60)
            uptime_str = f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s"
            link = f"{self.base_path}/{inst.instance_id}/admin"
            instances_html += (
                f'<div class="instance">'
                f'<a href="{link}">{inst.event_code}</a>'
                f' &mdash; connected {uptime_str}'
                f'</div>\n'
            )

        if not instances_html:
            instances_html = '<p class="empty">No MatchBox instances connected.</p>'

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MatchBox Relay</title>
    <style>
        body {{ font-family: -apple-system, sans-serif; max-width: 600px; margin: 40px auto; padding: 0 20px; background: #1a1a2e; color: #e0e0e0; }}
        h1 {{ color: #fff; }}
        .instance {{ padding: 12px 16px; margin: 8px 0; background: #16213e; border-radius: 8px; }}
        .instance a {{ color: #4fc3f7; text-decoration: none; font-weight: 600; }}
        .instance a:hover {{ text-decoration: underline; }}
        .empty {{ color: #888; font-style: italic; }}
        .refresh {{ color: #888; font-size: 0.85em; }}
    </style>
    <script>setTimeout(() => location.reload(), 10000);</script>
</head>
<body>
    <h1>MatchBox Relay</h1>
    <h2>Connected Instances</h2>
    {instances_html}
    <p class="refresh">Auto-refreshes every 10 seconds.</p>
</body>
</html>"""
        return web.Response(text=html, content_type='text/html')

    async def handle_tunnel_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Accept a MatchBox tunnel WebSocket connection."""
        ws = web.WebSocketResponse(compress=False)
        _ = await ws.prepare(request)

        instance: TunnelInstance | None = None

        try:
            # First message must be registration
            msg = await asyncio.wait_for(ws.receive(), timeout=10)
            if msg.type != WSMsgType.TEXT:
                _ = await ws.close(code=4000, message=b"Expected text frame")
                return ws

            reg_data: dict[str, str] = cast(dict[str, str], json.loads(cast(str, msg.data)))
            if reg_data.get('type') != 'register':
                _ = await ws.send_json({'type': 'error', 'message': 'First message must be register'})
                _ = await ws.close()
                return ws

            # Validate token
            if reg_data.get('token') != self.token:
                _ = await ws.send_json({'type': 'error', 'message': 'Invalid token'})
                _ = await ws.close(code=4001, message=b"Invalid token")
                return ws

            event_code: str = reg_data.get('event_code', 'default')
            instance_id: str = event_code  # Use event code as the URL slug

            # If there's already an instance with this event code, disconnect it
            old_id = self.id_by_event.get(event_code)
            if old_id and old_id in self.instances:
                old_inst = self.instances.pop(old_id)
                del self.id_by_event[event_code]
                logger.info(f"Replacing existing instance for {event_code}")
                # Close all browser WS connections on old instance
                for browser_ws in list(old_inst.browser_ws_connections.values()):
                    try:
                        _ = await browser_ws.close()
                    except Exception:
                        pass
                old_inst.browser_ws_connections.clear()
                # Cancel pending HTTP futures
                for future in list(old_inst.pending_http.values()):
                    if not future.done():
                        _ = future.cancel()
                old_inst.pending_http.clear()
                try:
                    _ = await old_inst.ws.close(code=4010, message=b"Replaced by new connection")
                except Exception:
                    pass

            instance = TunnelInstance(ws, event_code, instance_id)
            self.instances[instance_id] = instance
            self.id_by_event[event_code] = instance_id

            _ = await ws.send_json({'type': 'registered', 'instance_id': instance_id})
            logger.info(f"Instance registered: {event_code} (id: {instance_id})")

            # Message loop
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data: dict[str, object] = cast(dict[str, object], json.loads(cast(str, msg.data)))
                        msg_type = str(data.get('type', ''))

                        if msg_type == 'http_response':
                            req_id = str(data.get('id', ''))
                            future = instance.pending_http.pop(req_id, None)
                            if future and not future.done():
                                future.set_result(data)

                        elif msg_type == 'ws_opened':
                            ws_id = str(data.get('id', ''))
                            logger.info(f"WS proxy: tunnel confirmed local WS opened (id={ws_id[:8]})")

                        elif msg_type == 'ws_error':
                            ws_id = str(data.get('id', ''))
                            logger.warning(f"WS proxy: tunnel reported WS error (id={ws_id[:8]}): {data.get('message', '')}")
                            browser_ws = instance.browser_ws_connections.pop(ws_id, None)
                            if browser_ws:
                                _ = await browser_ws.close()

                        elif msg_type == 'ws_data':
                            ws_id = str(data.get('id', ''))
                            browser_ws = instance.browser_ws_connections.get(ws_id)
                            if browser_ws is not None:
                                try:
                                    ws_data_str = str(data.get('data', ''))
                                    logger.debug(f"WS proxy: tunnel→browser (id={ws_id[:8]}, {len(ws_data_str)} chars)")
                                    await browser_ws.send_str(ws_data_str)
                                except Exception as e:
                                    logger.warning(f"WS proxy: tunnel→browser send failed (id={ws_id[:8]}): {e}")
                                    _ = instance.browser_ws_connections.pop(ws_id, None)
                            else:
                                logger.warning(f"WS proxy: tunnel→browser no browser WS (id={ws_id}, keys={list(instance.browser_ws_connections.keys())})")

                        elif msg_type == 'ws_close':
                            ws_id = str(data.get('id', ''))
                            logger.info(f"WS proxy: tunnel sent ws_close (id={ws_id[:8]})")
                            browser_ws = instance.browser_ws_connections.pop(ws_id, None)
                            if browser_ws:
                                try:
                                    _ = await browser_ws.close()
                                except Exception:
                                    pass

                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON from tunnel")

                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break

        except asyncio.TimeoutError:
            logger.warning("Tunnel: Registration timeout")
            _ = await ws.close(code=4000, message=b"Registration timeout")
        except Exception as e:
            logger.error(f"Tunnel handler error: {e}")
        finally:
            if instance:
                # Cancel pending HTTP futures
                for future in list(instance.pending_http.values()):
                    if not future.done():
                        _ = future.cancel()
                instance.pending_http.clear()

                # Close browser WS connections (copy to avoid RuntimeError)
                for browser_ws in list(instance.browser_ws_connections.values()):
                    try:
                        _ = await browser_ws.close()
                    except Exception:
                        pass
                instance.browser_ws_connections.clear()

                _ = self.instances.pop(instance.instance_id, None)
                _ = self.id_by_event.pop(instance.event_code, None)
                logger.info(f"Instance disconnected: {instance.event_code}")

        return ws

    async def handle_proxy(self, request: web.Request) -> web.StreamResponse:
        """Proxy HTTP requests to a MatchBox instance via tunnel."""
        instance_id = request.match_info['instance_id']
        instance = self.instances.get(instance_id)

        if not instance:
            return web.Response(text="Instance not connected", status=502)

        # Check if this is a WebSocket upgrade
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            return await self._proxy_ws(request, instance)

        # Regular HTTP proxy
        path = request.match_info.get('path', '')
        path = '/' + path
        if request.query_string:
            path += '?' + request.query_string

        req_id = str(uuid.uuid4())
        body = await request.read()

        # Build headers dict (skip hop-by-hop headers)
        skip_headers = {'host', 'connection', 'upgrade', 'transfer-encoding'}
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in skip_headers
        }

        future: asyncio.Future[dict[str, object]] = asyncio.get_event_loop().create_future()
        instance.pending_http[req_id] = future

        try:
            _ = await instance.ws.send_json({
                'type': 'http_request',
                'id': req_id,
                'method': request.method,
                'path': path,
                'headers': headers,
                'body': base64.b64encode(body).decode('ascii') if body else '',
            })

            # Wait for response with timeout
            resp_data = await asyncio.wait_for(future, timeout=30)

            status = cast(int, resp_data.get('status', 502))
            resp_headers = cast(dict[str, str], resp_data.get('headers', {}))
            resp_body = base64.b64decode(str(resp_data.get('body', '')))

            # Build response, skip hop-by-hop headers
            response = web.Response(
                status=status,
                body=resp_body,
            )
            skip_resp = {'transfer-encoding', 'content-length', 'connection'}
            for k, v in resp_headers.items():
                if k.lower() not in skip_resp:
                    response.headers[k] = v

            return response

        except asyncio.TimeoutError:
            _ = instance.pending_http.pop(req_id, None)
            return web.Response(text="Tunnel request timeout", status=504)
        except Exception as e:
            _ = instance.pending_http.pop(req_id, None)
            return web.Response(text=f"Tunnel proxy error: {e}", status=502)

    async def _proxy_ws(self, request: web.Request, instance: TunnelInstance) -> web.WebSocketResponse:
        """Proxy a WebSocket connection through the tunnel."""
        # Accept with the subprotocols the browser requested
        req_protocols = request.headers.get('Sec-WebSocket-Protocol', '')
        protocols = tuple(p.strip() for p in req_protocols.split(',') if p.strip()) if req_protocols else ()
        browser_ws = web.WebSocketResponse(protocols=protocols, compress=False)
        _ = await browser_ws.prepare(request)

        ws_id = str(uuid.uuid4())
        path = '/' + request.match_info.get('path', '')

        selected = cast(str, getattr(browser_ws, '_ws_protocol', None))
        logger.info(f"WS proxy: browser connected for {path} (id={ws_id[:8]}, protocols={protocols}, selected={selected})")

        instance.browser_ws_connections[ws_id] = browser_ws

        try:
            # Ask MatchBox to open local WS, include subprotocols
            _ = await instance.ws.send_json({
                'type': 'ws_open',
                'id': ws_id,
                'path': path,
                'subprotocols': list(protocols),
            })
            logger.info(f"WS proxy: sent ws_open to tunnel (id={ws_id[:8]})")

            # Forward browser messages to tunnel
            msg_count = 0
            async for msg in browser_ws:
                if msg.type == WSMsgType.TEXT:
                    msg_count += 1
                    logger.debug(f"WS proxy: browser→tunnel (id={ws_id[:8]}, {len(cast(str, msg.data))} chars)")
                    _ = await instance.ws.send_json({
                        'type': 'ws_data',
                        'id': ws_id,
                        'data': cast(str, msg.data),
                    })
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    logger.info(f"WS proxy: browser sent {msg.type} (id={ws_id[:8]}, data={cast(str, msg.data)!r}, extra={cast(str, msg.extra)!r})")
                    break

            logger.info(f"WS proxy: browser loop ended (id={ws_id[:8]}, msgs={msg_count}, close_code={browser_ws.close_code})")

        except Exception as e:
            logger.warning(f"WS proxy error: {e}")
        finally:
            _ = instance.browser_ws_connections.pop(ws_id, None)
            # Tell MatchBox to close local WS
            try:
                _ = await instance.ws.send_json({
                    'type': 'ws_close',
                    'id': ws_id,
                })
            except Exception:
                pass

        return browser_ws


def main() -> None:
    parser = argparse.ArgumentParser(description="MatchBox Relay Server")
    _ = parser.add_argument('--port', type=int, default=8080, help='Port to listen on (default: 8080)')
    _ = parser.add_argument('--token', required=True, help='Shared authentication token')
    _ = parser.add_argument('--base-path', default='', help='URL base path (e.g. /FTC/MatchBox)')
    args = parser.parse_args()
    port = cast(int, args.port)
    token = cast(str, args.token)
    base_path = cast(str, args.base_path).rstrip('/')

    relay = RelayServer(token=token, base_path=base_path)

    app = web.Application()
    prefix = base_path if base_path else ''
    _ = app.router.add_get(prefix + '/', relay.handle_dashboard)
    _ = app.router.add_get(prefix + '/tunnel', relay.handle_tunnel_ws)
    _ = app.router.add_route('*', prefix + '/{instance_id}/{path:.*}', relay.handle_proxy)

    logger.info(f"Starting relay server on port {port} (base path: {base_path or '/'})")
    web.run_app(app, port=port, print=None)


if __name__ == '__main__':
    main()
