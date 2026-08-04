from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from happy_token_gateway import (
    GatewayRequestError,
    HappyTokenGateway,
    UserIdentity,
)


class MockHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    provision_status = 200
    include_oauth = True

    def do_GET(self):
        self.requests.append({"method": "GET", "path": self.path})
        if self.path.startswith("/models?"):
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "gpt-pro::gpt-5.6",
                            "upstream_model": "gpt-5.6",
                            "group": "gpt-pro",
                        },
                        {
                            "id": "gpt-pro::gpt-4o-audio-preview",
                            "upstream_model": "gpt-4o-audio-preview",
                            "group": "gpt-pro",
                        },
                        {
                            "id": "gpt-pro::gpt-4o-realtime-preview",
                            "upstream_model": "gpt-4o-realtime-preview",
                            "group": "gpt-pro",
                        },
                        {
                            "id": "gpt-pro::gpt-image-2",
                            "upstream_model": "gpt-image-2",
                            "group": "gpt-pro",
                        },
                    ],
                },
            )
            return
        if self.path == "/api/pricing":
            self._json(
                200,
                {
                    "success": True,
                    "data": [
                        {
                            "model_name": "gpt-5.6",
                            "supported_endpoint_types": ["openai"],
                        },
                        {
                            "model_name": "gpt-image-2",
                            "supported_endpoint_types": ["openai"],
                        },
                        {
                            "model_name": "not-openai",
                            "supported_endpoint_types": ["anthropic"],
                        },
                    ],
                },
            )
            return
        if self.path == "/api/v1/users/webui-user-1":
            oauth = {"oidc": {"sub": "casdoor-sub-1"}} if self.include_oauth else {}
            self._json(
                200,
                {
                    "id": "webui-user-1",
                    "email": "user@example.invalid",
                    "oauth": oauth,
                },
            )
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b""
        try:
            payload = json.loads(raw_body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        self.requests.append(
            {
                "method": "POST",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "accept": self.headers.get("Accept"),
                "raw_body": raw_body,
                "payload": payload,
            }
        )
        if self.path == "/bind":
            if self.provision_status != 200:
                self._json(
                    self.provision_status,
                    {
                        "ok": False,
                        "message": "secret=PROTOTYPE_SECRET token=sk-never-log-this",
                    },
                )
                return
            self._json(
                200,
                {
                    "ok": True,
                    "user_id": "42",
                    "token_id": "7",
                    "token": "PROTOTYPE_USER_TOKEN",
                },
            )
            return
        if self.path == "/v1/chat/completions":
            self._json(
                200,
                {
                    "model": payload.get("model"),
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "happy-token-ok",
                            }
                        }
                    ],
                },
            )
            return
        if self.path in {"/v1/images/generations", "/v1/images/edits"}:
            self._json(200, {"data": [{"url": "https://example.invalid/image.png"}]})
            return
        if self.path == "/v1/audio/transcriptions":
            self._json(200, {"text": "prototype transcription"})
            return
        if self.path == "/v1/audio/speech":
            body = b"PROTOTYPE_AUDIO_BYTES"
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"error": "not found"})

    def _json(self, status: int, payload: dict[str, object]):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class FakeSQLCursor:
    def __init__(self):
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement, parameters=()):
        normalized = " ".join(str(statement).split())
        self.queries.append((normalized, tuple(parameters)))
        if normalized.startswith("SELECT id FROM users WHERE oidc_id"):
            self._row = None
        elif normalized.startswith("SELECT id, oidc_id FROM users WHERE email"):
            self._row = None
        elif normalized.startswith("INSERT INTO users"):
            self._row = (42,)
        elif normalized.startswith("SELECT id, key FROM tokens"):
            self._row = None
        elif normalized.startswith("INSERT INTO tokens"):
            self._row = (7,)
        else:
            self._row = None

    def fetchone(self):
        row = self._row
        self._row = None
        return row

    def fetchall(self):
        return []


class CatalogSQLCursor(FakeSQLCursor):
    def __init__(self):
        super().__init__()
        self._rows = []

    def execute(self, statement, parameters=()):
        super().execute(statement, parameters)
        if "SELECT DISTINCT ability.model" in " ".join(str(statement).split()):
            self._rows = [("gpt-5.6",), ("gpt-image-2",)]

    def fetchall(self):
        rows = self._rows
        self._rows = []
        return rows


class GroupCatalogSQLCursor(FakeSQLCursor):
    def __init__(self):
        super().__init__()
        self._rows = []

    def execute(self, statement, parameters=()):
        super().execute(statement, parameters)
        normalized = " ".join(str(statement).split())
        if "FROM options" in normalized:
            self._rows = [
                (
                    "GroupRatio",
                    json.dumps(
                        {
                            "gpt-pro": 0.3,
                            "gpt-pro-dev": 1,
                            "image": 0.5,
                            "gpt-image-web": 0.05,
                        }
                    ),
                ),
                (
                    "UserUsableGroups",
                    json.dumps(
                        {
                            "gpt-pro": "标准",
                            "gpt-pro-dev": "开发",
                            "image": "图片",
                            "gpt-image-web": "图片网页",
                        }
                    ),
                ),
            ]
        elif 'SELECT DISTINCT ability."group", ability.model' in normalized:
            self._rows = [
                ("gpt-pro", "gpt-5.6"),
                ("gpt-pro", "gpt-image-2"),
                ("gpt-pro-dev", "gpt-5.6"),
                ("image", "gpt-image-2"),
                ("gpt-image-web", "gpt-5.6"),
            ]

    def fetchall(self):
        rows = self._rows
        self._rows = []
        return rows


class FakeSQLConnection:
    def __init__(self, cursor=None):
        self.cursor_instance = cursor or FakeSQLCursor()
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


class HappyTokenGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        MockHandler.requests = []
        MockHandler.provision_status = 200
        MockHandler.include_oauth = True
        self.identity = UserIdentity(
            open_webui_user_id="webui-user-1",
            email="user@example.invalid",
            name="Gateway User",
            role="user",
        )

    def gateway(self) -> HappyTokenGateway:
        return HappyTokenGateway(
            api_base_url=f"{self.base_url}/v1",
            management_url=self.base_url,
            provision_url=f"{self.base_url}/bind",
            provision_secret="PROTOTYPE_SECRET",
            open_webui_url=self.base_url,
            open_webui_admin_api_key="PROTOTYPE_OPEN_WEBUI_ADMIN_KEY",
            identity_provider="oidc",
            token_name="HappyChat Default",
            token_group="default",
        )

    def test_catalog_keeps_all_openai_compatible_platform_models(self):
        models = self.gateway().model_catalog()

        self.assertEqual(
            [item["id"] for item in models],
            ["gpt-5.6", "gpt-image-2"],
        )
        self.assertEqual(models[0]["owned_by"], "happy-token")

    def test_endpoint_catalog_excludes_non_chat_capabilities(self):
        gateway = HappyTokenGateway(
            api_base_url=f"{self.base_url}/v1",
            static_api_key="PROTOTYPE_SHARED_KEY",
            catalog_url=f"{self.base_url}/models",
            catalog_secret="PROTOTYPE_CATALOG_SECRET",
            enable_group_selection=True,
            excluded_model_terms=["image", "audio", "realtime"],
        )

        self.assertEqual(
            [item["id"] for item in gateway.model_catalog()],
            ["gpt-pro::gpt-5.6"],
        )
        self.assertEqual(
            [
                item["id"]
                for item in gateway.model_catalog(include_excluded_models=True)
            ],
            [
                "gpt-pro::gpt-5.6",
                "gpt-pro::gpt-4o-audio-preview",
                "gpt-pro::gpt-4o-realtime-preview",
                "gpt-pro::gpt-image-2",
            ],
        )

    def test_resolves_casdoor_identity_and_provisions_user_token(self):
        gateway = self.gateway()

        credential = gateway.credential_for(self.identity)

        self.assertEqual(credential.subject, "casdoor-sub-1")
        self.assertEqual(credential.token, "sk-PROTOTYPE_USER_TOKEN")
        provision = next(item for item in MockHandler.requests if item["path"] == "/bind")
        self.assertEqual(provision["authorization"], "Bearer PROTOTYPE_SECRET")
        self.assertEqual(
            provision["payload"],
            {
                "provider": "oidc",
                "subject": "casdoor-sub-1",
                "email": "user@example.invalid",
                "name": "Gateway User",
                "token_name": "HappyChat Default",
                "group": "default",
                "models": ["gpt-5.6", "gpt-image-2"],
            },
        )

    def test_chat_uses_provisioned_token_without_exposing_it_to_client(self):
        gateway = self.gateway()

        response = gateway.open_chat_completion(
            self.identity,
            {
                "model": "gpt-5.6",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            },
        )
        body = json.loads(response.read())

        self.assertEqual(body["choices"][0]["message"]["content"], "happy-token-ok")
        chat = next(
            item for item in MockHandler.requests if item["path"] == "/v1/chat/completions"
        )
        self.assertEqual(chat["authorization"], "Bearer sk-PROTOTYPE_USER_TOKEN")
        self.assertNotIn("PROTOTYPE_USER_TOKEN", json.dumps(body))

    def test_image_edit_preserves_multipart_body_and_user_token(self):
        gateway = self.gateway()
        multipart = b"--prototype\r\nimage-bytes\r\n--prototype--\r\n"

        response = gateway.open_model_request(
            self.identity,
            path="/images/edits",
            body=multipart,
            content_type="multipart/form-data; boundary=prototype",
            accept="application/json",
        )
        body = json.loads(response.read())

        self.assertEqual(body["data"][0]["url"], "https://example.invalid/image.png")
        request = next(
            item for item in MockHandler.requests if item["path"] == "/v1/images/edits"
        )
        self.assertEqual(request["authorization"], "Bearer sk-PROTOTYPE_USER_TOKEN")
        self.assertEqual(
            request["content_type"],
            "multipart/form-data; boundary=prototype",
        )
        self.assertEqual(request["raw_body"], multipart)

    def test_audio_speech_returns_binary_response(self):
        gateway = self.gateway()

        response = gateway.open_model_request(
            self.identity,
            path="/audio/speech",
            body=json.dumps({"model": "audio", "input": "hello"}).encode(),
            content_type="application/json",
            accept="audio/mpeg",
        )

        self.assertEqual(response.headers.get("Content-Type"), "audio/mpeg")
        self.assertEqual(response.read(), b"PROTOTYPE_AUDIO_BYTES")

    def test_missing_casdoor_link_is_rejected(self):
        MockHandler.include_oauth = False

        with self.assertRaisesRegex(
            GatewayRequestError,
            "not linked to Casdoor",
        ):
            self.gateway().credential_for(self.identity)

    def test_provisioning_failure_redacts_upstream_body(self):
        MockHandler.provision_status = 500

        with self.assertRaises(GatewayRequestError) as raised:
            self.gateway().credential_for(self.identity)

        message = str(raised.exception)
        self.assertNotIn("PROTOTYPE_SECRET", message)
        self.assertNotIn("sk-never-log-this", message)

    def test_shared_key_mode_is_explicit_and_does_not_resolve_identity(self):
        gateway = HappyTokenGateway(
            api_base_url=f"{self.base_url}/v1",
            management_url=self.base_url,
            static_api_key="PROTOTYPE_SHARED_KEY",
            configured_models=["gpt-5.6"],
        )

        credential = gateway.credential_for(self.identity)

        self.assertEqual(credential.token, "sk-PROTOTYPE_SHARED_KEY")
        self.assertEqual(MockHandler.requests, [])

    def test_sql_mode_creates_newapi_user_and_token_like_happyimage(self):
        connection = FakeSQLConnection()
        observed_dsns: list[str] = []
        gateway = HappyTokenGateway(
            api_base_url=f"{self.base_url}/v1",
            management_url=self.base_url,
            newapi_sql_dsn="postgresql://placeholder.invalid/newapi",
            open_webui_url=self.base_url,
            open_webui_admin_api_key="PROTOTYPE_OPEN_WEBUI_ADMIN_KEY",
            identity_provider="oidc",
            token_name="HappyChat Default",
            token_group="default",
            configured_models=["gpt-5.6"],
            sql_connect_factory=lambda dsn: (
                observed_dsns.append(dsn) or connection
            ),
        )

        credential = gateway.credential_for(self.identity)

        self.assertEqual(observed_dsns, ["postgresql://placeholder.invalid/newapi"])
        self.assertEqual(credential.subject, "casdoor-sub-1")
        self.assertTrue(credential.token.startswith("sk-"))
        statements = [statement for statement, _ in connection.cursor_instance.queries]
        self.assertTrue(
            any(statement.startswith("INSERT INTO users") for statement in statements)
        )
        self.assertTrue(
            any(statement.startswith("INSERT INTO tokens") for statement in statements)
        )
        self.assertTrue(connection.closed)

    def test_sql_catalog_only_lists_recently_healthy_group_channels(self):
        cursor = CatalogSQLCursor()
        connection = FakeSQLConnection(cursor)
        gateway = HappyTokenGateway(
            api_base_url=f"{self.base_url}/v1",
            static_api_key="PROTOTYPE_SHARED_KEY",
            newapi_sql_dsn="postgresql://placeholder.invalid/newapi",
            token_group="gpt-pro",
            max_channel_test_age_seconds=600,
            max_channel_response_time_ms=10_000,
            sql_connect_factory=lambda _dsn: connection,
        )

        models = gateway.model_catalog()

        self.assertEqual([item["id"] for item in models], ["gpt-5.6", "gpt-image-2"])
        statement, parameters = next(
            query for query in cursor.queries if "SELECT DISTINCT ability.model" in query[0]
        )
        self.assertIn("channel.status = 1", statement)
        self.assertIn("channel.test_time", statement)
        self.assertIn("channel.response_time", statement)
        self.assertEqual(parameters, ("gpt-pro", 600, 10_000))

    def test_group_catalog_expands_models_and_excludes_image_named_groups(self):
        cursor = GroupCatalogSQLCursor()
        gateway = HappyTokenGateway(
            api_base_url=f"{self.base_url}/v1",
            static_api_key="PROTOTYPE_SHARED_KEY",
            newapi_sql_dsn="postgresql://placeholder.invalid/newapi",
            token_group="gpt-pro",
            enable_group_selection=True,
            excluded_group_terms=["image"],
            excluded_model_terms=["image"],
            sql_connect_factory=lambda _dsn: FakeSQLConnection(cursor),
        )

        models = gateway.model_catalog()

        self.assertEqual(
            [item["id"] for item in models],
            ["gpt-pro::gpt-5.6", "gpt-pro-dev::gpt-5.6"],
        )
        self.assertEqual(models[0]["name"], "gpt-5.6 · gpt-pro (0.3×)")
        self.assertEqual(models[1]["name"], "gpt-5.6 · gpt-pro-dev (1×)")
        self.assertEqual(models[1]["upstream_model"], "gpt-5.6")
        self.assertEqual(models[1]["group"], "gpt-pro-dev")

    def test_image_request_can_use_image_model_hidden_from_chat_catalog(self):
        gateway = HappyTokenGateway(
            api_base_url=f"{self.base_url}/v1",
            static_api_key="PROTOTYPE_SHARED_KEY",
            newapi_sql_dsn="postgresql://placeholder.invalid/newapi",
            token_group="gpt-pro",
            enable_group_selection=True,
            excluded_group_terms=["image"],
            excluded_model_terms=["image"],
            sql_connect_factory=lambda _dsn: FakeSQLConnection(GroupCatalogSQLCursor()),
        )

        self.assertEqual(
            [item["id"] for item in gateway.model_catalog()],
            ["gpt-pro::gpt-5.6", "gpt-pro-dev::gpt-5.6"],
        )

        response = gateway.open_model_request(
            self.identity,
            path="/images/generations",
            body=json.dumps(
                {
                    "model": "gpt-pro::gpt-image-2",
                    "prompt": "a prototype image",
                }
            ).encode(),
            content_type="application/json",
            accept="application/json",
        )
        response.read()

        request = next(
            item
            for item in MockHandler.requests
            if item["path"] == "/v1/images/generations"
        )
        self.assertEqual(request["payload"]["model"], "gpt-image-2")

    def test_upstream_validation_error_is_not_reported_as_gateway_failure(self):
        error = HappyTokenGateway._gateway_http_error(400)

        self.assertEqual(error.status, 400)
        self.assertIn("does not support", str(error))

    def test_virtual_model_routes_to_selected_group_and_rewrites_model(self):
        gateway = self.gateway()
        gateway.enable_group_selection = True
        gateway.configured_models = []
        gateway._catalog_cache = (
            float("inf"),
            [
                gateway._group_model_item("gpt-5.6", "gpt-pro", 0.3),
                gateway._group_model_item("gpt-5.6", "gpt-pro-dev", 1),
            ],
        )

        response = gateway.open_chat_completion(
            self.identity,
            {
                "model": "gpt-pro-dev::gpt-5.6",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            },
        )
        response.read()

        provision = next(item for item in MockHandler.requests if item["path"] == "/bind")
        chat = next(
            item for item in MockHandler.requests if item["path"] == "/v1/chat/completions"
        )
        self.assertEqual(provision["payload"]["group"], "gpt-pro-dev")
        self.assertEqual(provision["payload"]["token_name"], "HappyChat gpt-pro-dev")
        self.assertEqual(chat["payload"]["model"], "gpt-5.6")

    def test_virtual_model_rewrites_multipart_model_field(self):
        gateway = self.gateway()
        gateway.enable_group_selection = True
        gateway.configured_models = []
        gateway._non_chat_catalog_cache = (
            float("inf"),
            [gateway._group_model_item("gpt-image-2", "gpt-pro-dev", 1)],
        )
        multipart = (
            b"--prototype\r\n"
            b'Content-Disposition: form-data; name="model"\r\n\r\n'
            b"gpt-pro-dev::gpt-image-2\r\n"
            b"--prototype\r\n"
            b'Content-Disposition: form-data; name="image"; filename="a.png"\r\n\r\n'
            b"image-bytes\r\n"
            b"--prototype--\r\n"
        )

        response = gateway.open_model_request(
            self.identity,
            path="/images/edits",
            body=multipart,
            content_type="multipart/form-data; boundary=prototype",
            accept="application/json",
        )
        response.read()

        request = next(item for item in MockHandler.requests if item["path"] == "/v1/images/edits")
        self.assertIn(b"\r\n\r\ngpt-image-2\r\n", request["raw_body"])
        self.assertNotIn(b"gpt-pro-dev::gpt-image-2", request["raw_body"])
        provision = next(item for item in MockHandler.requests if item["path"] == "/bind")
        self.assertEqual(provision["payload"]["group"], "gpt-pro-dev")

    def test_group_credentials_are_cached_separately(self):
        gateway = self.gateway()

        gateway.credential_for(self.identity, group="gpt-pro")
        gateway.credential_for(self.identity, group="gpt-pro-dev")
        gateway.credential_for(self.identity, group="gpt-pro")

        provisions = [item for item in MockHandler.requests if item["path"] == "/bind"]
        self.assertEqual(len(provisions), 2)
        self.assertEqual(
            [item["payload"]["group"] for item in provisions],
            ["gpt-pro", "gpt-pro-dev"],
        )

    def test_model_404_temporarily_removes_it_from_catalog(self):
        gateway = HappyTokenGateway(
            api_base_url=f"{self.base_url}/v1",
            static_api_key="PROTOTYPE_SHARED_KEY",
            configured_models=["gpt-5.6"],
        )

        gateway._record_model_failure("gpt-5.6", 404)
        self.assertEqual(gateway.model_catalog(), [])
        gateway._record_model_success("gpt-5.6")
        self.assertEqual([item["id"] for item in gateway.model_catalog()], ["gpt-5.6"])

    def test_user_quota_error_does_not_hide_model(self):
        gateway = HappyTokenGateway(
            api_base_url=f"{self.base_url}/v1",
            static_api_key="PROTOTYPE_SHARED_KEY",
            configured_models=["gpt-5.6"],
        )

        gateway._record_model_failure("gpt-5.6", 429)
        self.assertEqual([item["id"] for item in gateway.model_catalog()], ["gpt-5.6"])


if __name__ == "__main__":
    unittest.main()
