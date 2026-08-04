"""Single-process HappyChat companion for Open WebUI.

The public listener enforces HappyChat HTTP policies before forwarding to
Open WebUI.  The internal listener exposes the OpenAI-compatible model routes
used by Open WebUI.  They are two listeners in one process and one deployable
service, not separate control and adapter services.
"""

from __future__ import annotations

import os
import threading
from http.server import ThreadingHTTPServer

from control_proxy import Handler as PublicHandler
from model_adapter import Handler as ModelHandler


class Server(ThreadingHTTPServer):
    daemon_threads = True


def serve() -> None:
    public_host = os.environ.get("BIND_HOST", "0.0.0.0")
    public_port = int(os.environ.get("CONTROL_PORT", "8080"))
    model_host = os.environ.get("INTERNAL_BIND_HOST", public_host)
    model_port = int(os.environ.get("ADAPTER_PORT", "8000"))

    model_server = Server((model_host, model_port), ModelHandler)
    model_thread = threading.Thread(
        target=model_server.serve_forever,
        name="happychat-model-listener",
        daemon=True,
    )
    model_thread.start()

    public_server = Server((public_host, public_port), PublicHandler)
    try:
        public_server.serve_forever()
    finally:
        public_server.server_close()
        model_server.shutdown()
        model_server.server_close()
        model_thread.join(timeout=5)


if __name__ == "__main__":
    serve()
