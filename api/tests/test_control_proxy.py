from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import unittest
import urllib.error
import urllib.request


APP_DIR = Path(__file__).resolve().parents[1] / "app"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class UpstreamHandler(BaseHTTPRequestHandler):
    calls: list[tuple[str, str]] = []

    def do_GET(self):
        type(self).calls.append(("GET", self.path))
        if (
            self.path.startswith("/ws/socket.io/")
            and self.headers.get("Upgrade", "").casefold() == "websocket"
        ):
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", "prototype")
            self.end_headers()
            payload = self.connection.recv(4)
            self.connection.sendall(payload)
            return
        if self.path == "/oauth/oidc/login":
            self.send_response(302)
            self.send_header("Location", "https://auth.example/authorize")
            self.send_header("Set-Cookie", "oauth-state=test; HttpOnly")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(200, {"path": self.path})

    def do_POST(self):
        type(self).calls.append(("POST", self.path))
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        if self.path.endswith("/archive"):
            self._json(200, {"archived": True})
            return
        self._json(200, {"path": self.path})

    def _json(self, status: int, payload: dict[str, str]):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class ControlProxyTests(unittest.TestCase):
    def setUp(self):
        UpstreamHandler.calls = []
        self.upstream_port = free_port()
        self.proxy_port = free_port()
        self.upstream = ThreadingHTTPServer(
            ("127.0.0.1", self.upstream_port), UpstreamHandler
        )
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()

        environment = {
            **os.environ,
            "BIND_HOST": "127.0.0.1",
            "CONTROL_PORT": str(self.proxy_port),
            "OPEN_WEBUI_URL": f"http://127.0.0.1:{self.upstream_port}",
            "HAPPYCHAT_BLOCK_PUBLIC_LOCAL_AUTH": "true",
        }
        self.proxy = subprocess.Popen(
            ["python3", "-B", str(APP_DIR / "control_proxy.py")],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.proxy_port}/health", timeout=0.2
                ).read()
                break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.05)
        else:
            self.fail("control proxy did not start")
        UpstreamHandler.calls = []

    def tearDown(self):
        self.proxy.terminate()
        self.proxy.wait(timeout=5)
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(timeout=5)

    def request(self, method: str, path: str):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy_port}{path}",
            data=b"{}" if method == "POST" else None,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            opener = urllib.request.build_opener(NoRedirectHandler())
            return opener.open(request, timeout=2)
        except urllib.error.HTTPError as error:
            return error

    def test_public_local_signin_and_signup_are_blocked(self):
        for path in ("/api/v1/auths/signin", "/api/v1/auths/signup"):
            with self.request("POST", path) as response:
                self.assertEqual(response.status, 403)
        self.assertEqual(UpstreamHandler.calls, [])

    def test_oidc_callback_remains_available(self):
        with self.request("GET", "/oauth/oidc/callback?code=test&state=test") as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(
            UpstreamHandler.calls,
            [("GET", "/oauth/oidc/callback?code=test&state=test")],
        )

    def test_oidc_login_redirect_and_cookie_are_relayed_to_browser(self):
        with self.request("GET", "/oauth/oidc/login") as response:
            self.assertEqual(response.status, 302)
            self.assertEqual(response.headers["Location"], "https://auth.example/authorize")
            self.assertEqual(response.headers["Set-Cookie"], "oauth-state=test; HttpOnly")
        self.assertEqual(UpstreamHandler.calls, [("GET", "/oauth/oidc/login")])

    def test_websocket_upgrade_is_tunneled_to_open_webui(self):
        request = (
            "GET /ws/socket.io/?EIO=4&transport=websocket HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.proxy_port}\r\n"
            "Connection: Upgrade\r\n"
            "Upgrade: websocket\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Origin: http://localhost\r\n\r\n"
        ).encode()

        with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=2) as client:
            client.sendall(request)
            handshake = client.recv(1024)
            status_line = handshake.split(b"\r\n", 1)[0]
            client.sendall(b"ping")
            echoed = client.recv(4)

        self.assertEqual(status_line, b"HTTP/1.0 101 Switching Protocols")
        self.assertEqual(echoed, b"ping")

    def test_chat_delete_is_always_converted_to_archive(self):
        for _ in range(2):
            with self.request("DELETE", "/api/v1/chats/chat-1") as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(json.loads(response.read()))
        self.assertEqual(
            UpstreamHandler.calls,
            [
                ("POST", "/api/v1/chats/chat-1/archive"),
                ("POST", "/api/v1/chats/chat-1/archive"),
            ],
        )


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


if __name__ == "__main__":
    unittest.main()
