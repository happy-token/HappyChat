"""OpenAI-compatible adapter between Open WebUI and HappyToken."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from happy_token_gateway import (
    GatewayConfigurationError,
    GatewayRequestError,
    HappyTokenGateway,
    UserIdentity,
)


JWT_SECRET = os.environ["FORWARD_JWT_SECRET"].encode()
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
ADAPTER_MODE = os.environ.get("ADAPTER_MODE", "contract").strip().lower()
HAPPY_TOKEN = HappyTokenGateway.from_env() if ADAPTER_MODE == "happy-token" else None
try:
    MAX_REQUEST_BYTES = int(
        os.environ.get("HAPPYCHAT_MAX_REQUEST_BYTES", str(32 * 1024 * 1024))
    )
except ValueError:
    MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = max(1, MAX_REQUEST_BYTES)
GATEWAY_POST_PATHS = {
    "/v1/chat/completions": "/chat/completions",
    "/v1/images/generations": "/images/generations",
    "/v1/images/edits": "/images/edits",
    "/v1/audio/transcriptions": "/audio/transcriptions",
    "/v1/audio/speech": "/audio/speech",
}


def _base64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _identity(headers) -> UserIdentity:
    if headers.get("Authorization") != f"Bearer {OPENAI_API_KEY}":
        raise ValueError("invalid service credential")

    token = headers.get("X-OpenWebUI-User-Jwt", "")
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("missing signed user JWT")

    signing_input = f"{parts[0]}.{parts[1]}".encode()
    expected_signature = hmac.new(JWT_SECRET, signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(_base64url_decode(parts[2]), expected_signature):
        raise ValueError("invalid signed user JWT")

    header = json.loads(_base64url_decode(parts[0]))
    payload = json.loads(_base64url_decode(parts[1]))
    if header.get("alg") != "HS256" or payload.get("iss") != "open-webui":
        raise ValueError("unexpected JWT issuer or algorithm")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("expired signed user JWT")
    if not payload.get("sub"):
        raise ValueError("signed user JWT has no subject")
    return UserIdentity(
        open_webui_user_id=str(payload["sub"]),
        email=str(payload.get("email") or ""),
        name=str(payload.get("name") or ""),
        role=str(payload.get("role") or ""),
    )


def _model_for(user_id: str) -> str:
    digest = hashlib.sha256(user_id.encode()).hexdigest()[:8]
    return f"contract-model-{digest}"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"})
            return
        if self.path != "/v1/models":
            self._json(404, {"error": "not found"})
            return
        try:
            identity = _identity(self.headers)
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            print(f"models rejected: {error}", flush=True)
            self._json(401, {"error": str(error)})
            return

        if HAPPY_TOKEN is not None:
            try:
                HAPPY_TOKEN.validate()
                models = HAPPY_TOKEN.model_catalog()
            except GatewayConfigurationError as error:
                self._gateway_error(503, str(error))
                return
            except GatewayRequestError as error:
                self._gateway_error(error.status, str(error))
                return
            print(
                f"models accepted: happy-token user={identity.open_webui_user_id}",
                flush=True,
            )
            self._json(200, {"object": "list", "data": models})
            return

        model = _model_for(identity.open_webui_user_id)
        print(f"models accepted: {model}", flush=True)
        self._json(
            200,
            {
                "object": "list",
                "data": [
                    {
                        "id": model,
                        "object": "model",
                        "created": 0,
                        "owned_by": "happy-token-contract",
                    }
                ],
            },
        )

    def do_POST(self):
        request_path = urllib.parse.urlsplit(self.path).path
        if request_path == "/internal/models/cache/invalidate":
            authorization = self.headers.get("Authorization", "")
            if not hmac.compare_digest(
                authorization.encode(),
                f"Bearer {OPENAI_API_KEY}".encode(),
            ):
                self._json(401, {"error": "invalid service credential"})
                return
            if HAPPY_TOKEN is None:
                self._json(503, {"error": "model catalog is unavailable"})
                return
            HAPPY_TOKEN.invalidate_model_catalog()
            self._json(200, {"status": "ok"})
            return

        gateway_path = GATEWAY_POST_PATHS.get(request_path)
        if gateway_path is None:
            self._json(404, {"error": "not found"})
            return
        try:
            identity = _identity(self.headers)
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            print(f"model request rejected: {error}", flush=True)
            self._json(401, {"error": str(error)})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length < 0 or content_length > MAX_REQUEST_BYTES:
                raise ValueError("invalid request body size")
            raw_body = self.rfile.read(content_length) if content_length else b""
        except ValueError as error:
            self._json(400, {"error": str(error)})
            return

        if HAPPY_TOKEN is not None:
            try:
                response = HAPPY_TOKEN.open_model_request(
                    identity,
                    path=gateway_path,
                    body=raw_body,
                    content_type=self.headers.get(
                        "Content-Type", "application/octet-stream"
                    ),
                    accept=self.headers.get("Accept", "*/*"),
                )
            except GatewayConfigurationError as error:
                self._gateway_error(503, str(error))
                return
            except GatewayRequestError as error:
                self._gateway_error(error.status, str(error))
                return
            self._relay_gateway(response)
            return

        if request_path != "/v1/chat/completions":
            self._json(404, {"error": "capability unavailable in contract mode"})
            return
        try:
            payload = json.loads(raw_body or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
        except (ValueError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})
            return

        expected_model = _model_for(identity.open_webui_user_id)
        if payload.get("model") != expected_model:
            self._json(403, {"error": "model does not belong to signed user"})
            return

        if payload.get("stream"):
            self._stream(expected_model)
            return

        self._json(
            200,
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": expected_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": f"contract-ok:{expected_model}",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def _stream(self, model: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        first = {
            "id": "chatcmpl-contract-stream",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "contract-"}}],
        }
        self.wfile.write(f"data: {json.dumps(first)}\n\n".encode())
        self.wfile.flush()
        time.sleep(0.8)
        second = {
            "id": "chatcmpl-contract-stream",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {"index": 0, "delta": {"content": "stream-ok"}, "finish_reason": "stop"}
            ],
        }
        self.wfile.write(f"data: {json.dumps(second)}\n\ndata: [DONE]\n\n".encode())
        self.wfile.flush()
        self.close_connection = True

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _gateway_error(self, status: int, message: str):
        self._json(
            status,
            {
                "error": {
                    "message": message,
                    "type": "happy_token_gateway_error",
                }
            },
        )

    def _relay_gateway(self, response):
        content_type = response.headers.get("Content-Type", "application/json")
        self.send_response(response.status)
        self.send_header("Content-Type", content_type)
        if content_type.startswith("text/event-stream"):
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            read = getattr(response, "read1", response.read)
            while chunk := read(4096):
                self.wfile.write(chunk)
                self.wfile.flush()
            self.close_connection = True
            return

        body = response.read()
        content_disposition = response.headers.get("Content-Disposition")
        if content_disposition:
            self.send_header("Content-Disposition", content_disposition)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    host = os.environ.get("BIND_HOST", "0.0.0.0")
    port = int(os.environ.get("ADAPTER_PORT", "8000"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()
