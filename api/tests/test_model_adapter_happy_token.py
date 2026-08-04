from __future__ import annotations

import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.request


JWT_SECRET = "PROTOTYPE_ADAPTER_JWT_SECRET"
SERVICE_KEY = "PROTOTYPE_ADAPTER_SERVICE_KEY"
GATEWAY_KEY = "PROTOTYPE_ADAPTER_GATEWAY_KEY"


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _encode_segment(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _user_jwt() -> str:
    header = _encode_segment({"alg": "HS256", "typ": "JWT"})
    payload = _encode_segment(
        {
            "iss": "open-webui",
            "sub": "adapter-user-1",
            "email": "adapter@example.invalid",
            "name": "Adapter User",
            "role": "user",
            "exp": int(time.time()) + 60,
        }
    )
    signing_input = f"{header}.{payload}"
    signature = hmac.new(
        JWT_SECRET.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{signing_input}.{encoded_signature}"


class GatewayHandler(BaseHTTPRequestHandler):
    authorization = ""
    path_seen = ""
    content_type_seen = ""

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b""
        try:
            payload = json.loads(raw_body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        self.__class__.authorization = self.headers.get("Authorization", "")
        self.__class__.path_seen = self.path
        self.__class__.content_type_seen = self.headers.get("Content-Type", "")
        if self.path == "/v1/audio/speech":
            body = b"ADAPTER_AUDIO_BYTES"
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/v1/images/generations":
            response_payload = {
                "data": [{"url": "https://example.invalid/adapter-image.png"}]
            }
        else:
            response_payload = {
                "model": payload.get("model"),
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "adapter-gateway-ok",
                        }
                    }
                ],
            }
        body = json.dumps(
            response_payload
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class ModelAdapterHappyTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        cls.gateway_thread = threading.Thread(
            target=cls.gateway.serve_forever,
            daemon=True,
        )
        cls.gateway_thread.start()
        cls.adapter_port = _free_port()
        app_dir = Path(__file__).resolve().parents[1] / "app"
        environment = {
            **os.environ,
            "BIND_HOST": "127.0.0.1",
            "ADAPTER_PORT": str(cls.adapter_port),
            "FORWARD_JWT_SECRET": JWT_SECRET,
            "OPENAI_API_KEY": SERVICE_KEY,
            "ADAPTER_MODE": "happy-token",
            "HAPPYCHAT_GATEWAY_API_BASE_URL": (
                f"http://127.0.0.1:{cls.gateway.server_port}/v1"
            ),
            "HAPPYCHAT_GATEWAY_MANAGEMENT_URL": (
                f"http://127.0.0.1:{cls.gateway.server_port}"
            ),
            "HAPPYCHAT_GATEWAY_API_KEY": GATEWAY_KEY,
            "HAPPYCHAT_GATEWAY_MODELS": "gpt-5.6",
        }
        cls.adapter = subprocess.Popen(
            [sys.executable, "-B", str(app_dir / "model_adapter.py")],
            cwd=app_dir,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{cls.adapter_port}/health",
                    timeout=0.2,
                ):
                    break
            except Exception:
                if cls.adapter.poll() is not None:
                    raise RuntimeError("model adapter failed to start")
                time.sleep(0.05)
        else:
            raise RuntimeError("model adapter did not become ready")

    @classmethod
    def tearDownClass(cls):
        cls.adapter.terminate()
        try:
            cls.adapter.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.adapter.kill()
            cls.adapter.wait(timeout=5)
        cls.gateway.shutdown()
        cls.gateway.server_close()
        cls.gateway_thread.join(timeout=5)

    def test_signed_user_request_reaches_gateway_without_returning_key(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.adapter_port}/v1/chat/completions",
            data=json.dumps(
                {
                    "model": "gpt-5.6",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": False,
                }
            ).encode(),
            headers={
                "Authorization": f"Bearer {SERVICE_KEY}",
                "X-OpenWebUI-User-Jwt": _user_jwt(),
                "Content-Type": "application/json",
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read())

        self.assertEqual(
            body["choices"][0]["message"]["content"],
            "adapter-gateway-ok",
        )
        self.assertEqual(GatewayHandler.authorization, f"Bearer sk-{GATEWAY_KEY}")
        self.assertNotIn(GATEWAY_KEY, json.dumps(body))

    def test_image_generation_uses_same_server_side_user_credential(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.adapter_port}/v1/images/generations",
            data=json.dumps(
                {"model": "gpt-image-2", "prompt": "a prototype"}
            ).encode(),
            headers={
                "Authorization": f"Bearer {SERVICE_KEY}",
                "X-OpenWebUI-User-Jwt": _user_jwt(),
                "Content-Type": "application/json",
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read())

        self.assertEqual(
            body["data"][0]["url"],
            "https://example.invalid/adapter-image.png",
        )
        self.assertEqual(GatewayHandler.path_seen, "/v1/images/generations")
        self.assertEqual(GatewayHandler.authorization, f"Bearer sk-{GATEWAY_KEY}")

    def test_audio_speech_relays_binary_response(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.adapter_port}/v1/audio/speech",
            data=json.dumps({"model": "audio", "input": "hello"}).encode(),
            headers={
                "Authorization": f"Bearer {SERVICE_KEY}",
                "X-OpenWebUI-User-Jwt": _user_jwt(),
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type")

        self.assertEqual(content_type, "audio/mpeg")
        self.assertEqual(body, b"ADAPTER_AUDIO_BYTES")
        self.assertEqual(GatewayHandler.path_seen, "/v1/audio/speech")


if __name__ == "__main__":
    unittest.main()
