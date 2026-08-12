"""HappyChat policy proxy in front of Open WebUI."""

from __future__ import annotations

import json
import os
import re
import select
import socket
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


UPSTREAM = os.environ["OPEN_WEBUI_URL"].rstrip("/")
UPSTREAM_PARTS = urllib.parse.urlsplit(UPSTREAM)
if UPSTREAM_PARTS.scheme != "http" or not UPSTREAM_PARTS.hostname:
    raise RuntimeError("OPEN_WEBUI_URL must be an internal http URL")
UPSTREAM_HOST = UPSTREAM_PARTS.hostname
UPSTREAM_PORT = UPSTREAM_PARTS.port or 80
MODEL_ADAPTER_URL = f"http://127.0.0.1:{os.environ.get('ADAPTER_PORT', '8000')}"
MODEL_ADAPTER_SERVICE_KEY = os.environ.get("OPENAI_API_KEY", "")
BLOCK_PUBLIC_LOCAL_AUTH = os.environ.get(
    "HAPPYCHAT_BLOCK_PUBLIC_LOCAL_AUTH", "false"
).strip().lower() in {"1", "true", "yes", "on"}
PUBLIC_LOCAL_AUTH_PATHS = frozenset(
    {
        "/api/v1/auths/signin",
        "/api/v1/auths/signup",
    }
)
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


UPSTREAM_OPENER = urllib.request.build_opener(NoRedirectHandler())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.headers.get("Upgrade", "").casefold() == "websocket":
            self._proxy_websocket()
            return
        parsed = urllib.parse.urlsplit(self.path)
        refresh = urllib.parse.parse_qs(parsed.query).get("refresh", ["false"])[-1]
        if (
            parsed.path.rstrip("/") in {"/api/models", "/api/v1/models"}
            and refresh.casefold() == "true"
        ):
            try:
                if not self._is_admin_request():
                    self._json(403, {"detail": "Administrator access required"})
                    return
            except ConnectionAbortedError:
                return
            if not self._invalidate_model_catalog():
                self._json(502, {"detail": "HappyChat model catalog refresh failed"})
                return
        self._proxy()

    def do_POST(self):
        request_path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        if BLOCK_PUBLIC_LOCAL_AUTH and request_path in PUBLIC_LOCAL_AUTH_PATHS:
            self.close_connection = True
            self._json(403, {"detail": "Local authentication is not available on the public HappyChat entry"})
            return
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_PATCH(self):
        self._proxy()

    def do_OPTIONS(self):
        self._proxy()

    def do_DELETE(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.rstrip("/") == "/api/v1/chats":
            self._json(409, {"detail": "prototype blocks batch hard delete"})
            return

        chat_match = re.fullmatch(r"/api/v1/chats/([^/]+)", parsed.path)
        if chat_match:
            chat_id = chat_match.group(1)
            response = self._request_upstream(
                method="POST",
                path=f"/api/v1/chats/{chat_id}/archive",
                body=b"",
            )
            body = response.read()
            if 200 <= response.status < 300:
                archived = json.loads(body or b"{}").get("archived")
                if archived:
                    self._json(200, True)
                    return
                self._json(409, {"detail": "upstream did not archive chat"})
                return
            self._send_buffered(response.status, response.headers, body)
            return

        if re.fullmatch(r"/api/v1/folders/[^/]+", parsed.path):
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if query.get("delete_contents", ["true"])[-1].lower() == "true":
                query["delete_contents"] = ["false"]
                safe_path = urllib.parse.urlunsplit(
                    (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query, doseq=True), parsed.fragment)
                )
                self._proxy(path=safe_path)
                return

        self._proxy()

    def _proxy(self, path: str | None = None):
        try:
            response = self._request_upstream(path=path)
        except ConnectionAbortedError:
            return
        self._send_upstream(response)

    def _proxy_websocket(self):
        upstream_socket = None
        response_started = False
        try:
            upstream_socket = socket.create_connection(
                (UPSTREAM_HOST, UPSTREAM_PORT), timeout=10
            )
            request_lines = [
                f"GET {self.path} {self.request_version}",
                f"Host: {UPSTREAM_PARTS.netloc}",
            ]
            request_lines.extend(
                f"{key}: {value}"
                for key, value in self.headers.raw_items()
                if key.casefold() not in {"host", "proxy-connection"}
            )
            upstream_socket.sendall(("\r\n".join(request_lines) + "\r\n\r\n").encode())

            handshake = bytearray()
            while b"\r\n\r\n" not in handshake:
                chunk = upstream_socket.recv(4096)
                if not chunk:
                    raise ConnectionError("Open WebUI closed the WebSocket handshake")
                handshake.extend(chunk)
                if len(handshake) > 64 * 1024:
                    raise ConnectionError("Open WebUI WebSocket handshake is too large")

            self.connection.sendall(handshake)
            response_started = True
            self.close_connection = True
            status_line = bytes(handshake).split(b"\r\n", 1)[0]
            if b" 101 " not in status_line:
                return

            upstream_socket.settimeout(None)
            self.connection.settimeout(None)
            peers = (self.connection, upstream_socket)
            while True:
                readable, _, _ = select.select(peers, [], [], 30)
                if not readable:
                    continue
                for source in readable:
                    data = source.recv(64 * 1024)
                    if not data:
                        return
                    target = upstream_socket if source is self.connection else self.connection
                    target.sendall(data)
        except (ConnectionError, OSError):
            if not response_started:
                self.close_connection = True
                self._json(502, {"detail": "Open WebUI WebSocket unavailable"})
        finally:
            if upstream_socket is not None:
                upstream_socket.close()

    def _request_upstream(self, method: str | None = None, path: str | None = None, body: bytes | None = None):
        if body is None:
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length) if content_length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP | {"host", "content-length", "accept-encoding"}
        }
        request = urllib.request.Request(
            f"{UPSTREAM}{path or self.path}",
            data=body,
            headers=headers,
            method=method or self.command,
        )
        try:
            return UPSTREAM_OPENER.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            return error
        except urllib.error.URLError as error:
            self._json(502, {"detail": f"Open WebUI unavailable: {error.reason}"})
            raise ConnectionAbortedError from error

    def _invalidate_model_catalog(self) -> bool:
        if not MODEL_ADAPTER_SERVICE_KEY:
            return False
        request = urllib.request.Request(
            f"{MODEL_ADAPTER_URL}/internal/models/cache/invalidate",
            data=b"",
            headers={
                "Authorization": f"Bearer {MODEL_ADAPTER_SERVICE_KEY}",
            },
            method="POST",
        )
        try:
            with UPSTREAM_OPENER.open(request, timeout=5) as response:
                response.read()
                return 200 <= response.status < 300
        except (urllib.error.HTTPError, urllib.error.URLError):
            return False

    def _is_admin_request(self) -> bool:
        response = self._request_upstream(
            method="GET",
            path="/api/v1/auths/",
            body=b"",
        )
        body = response.read()
        if not 200 <= response.status < 300:
            return False
        try:
            return json.loads(body).get("role") == "admin"
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            return False

    def _send_upstream(self, response):
        content_type = response.headers.get("Content-Type", "")
        if content_type.startswith("text/event-stream"):
            self.send_response(response.status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            read = getattr(response, "read1", response.read)
            while chunk := read(4096):
                self.wfile.write(chunk)
                self.wfile.flush()
            self.close_connection = True
            return
        self._send_buffered(response.status, response.headers, response.read())

    def _send_buffered(self, status: int, headers, body: bytes):
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() not in HOP_BY_HOP | {"content-length", "content-encoding"}:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    host = os.environ.get("BIND_HOST", "0.0.0.0")
    port = int(os.environ.get("CONTROL_PORT", "8080"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()
