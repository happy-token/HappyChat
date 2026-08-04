from __future__ import annotations

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


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class OpenWebUIHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "open-webui-ok", "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class HappyChatAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.open_webui = ThreadingHTTPServer(("127.0.0.1", 0), OpenWebUIHandler)
        cls.open_webui_thread = threading.Thread(
            target=cls.open_webui.serve_forever,
            daemon=True,
        )
        cls.open_webui_thread.start()
        cls.public_port = _free_port()
        cls.model_port = _free_port()
        app_dir = Path(__file__).resolve().parents[1] / "app"
        environment = {
            **os.environ,
            "BIND_HOST": "127.0.0.1",
            "CONTROL_PORT": str(cls.public_port),
            "INTERNAL_BIND_HOST": "127.0.0.1",
            "ADAPTER_PORT": str(cls.model_port),
            "OPEN_WEBUI_URL": (
                f"http://127.0.0.1:{cls.open_webui.server_port}"
            ),
            "FORWARD_JWT_SECRET": "PROTOTYPE_COMBINED_JWT_SECRET",
            "OPENAI_API_KEY": "PROTOTYPE_COMBINED_SERVICE_KEY",
            "ADAPTER_MODE": "contract",
        }
        cls.process = subprocess.Popen(
            [sys.executable, "-B", str(app_dir / "happychat_api.py")],
            cwd=app_dir,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        urls = [
            f"http://127.0.0.1:{cls.public_port}/health",
            f"http://127.0.0.1:{cls.model_port}/health",
        ]
        while time.monotonic() < deadline:
            try:
                for url in urls:
                    with urllib.request.urlopen(url, timeout=0.2):
                        pass
                break
            except Exception:
                if cls.process.poll() is not None:
                    raise RuntimeError("happychat-api failed to start")
                time.sleep(0.05)
        else:
            raise RuntimeError("happychat-api did not become ready")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)
        cls.open_webui.shutdown()
        cls.open_webui.server_close()
        cls.open_webui_thread.join(timeout=5)

    def test_one_process_serves_public_proxy_and_internal_model_health(self):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.public_port}/api/version",
            timeout=5,
        ) as response:
            public_payload = json.loads(response.read())
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.model_port}/health",
            timeout=5,
        ) as response:
            model_payload = json.loads(response.read())

        self.assertEqual(public_payload["status"], "open-webui-ok")
        self.assertEqual(public_payload["path"], "/api/version")
        self.assertEqual(model_payload, {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
