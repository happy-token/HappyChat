"""Happy Token/NewAPI connector for HappyChat.

The connector intentionally keeps gateway credentials behind the adapter. It
supports the same provisioning contract and PostgreSQL fallback used by
HappyImage, plus an explicitly opt-in shared-key mode for tests.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504, 522, 523, 524})
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MODEL_POST_PATHS = frozenset(
    {
        "/chat/completions",
        "/images/generations",
        "/images/edits",
        "/audio/transcriptions",
        "/audio/speech",
    }
)


class GatewayConfigurationError(RuntimeError):
    pass


class GatewayRequestError(RuntimeError):
    def __init__(self, message: str, *, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class UserIdentity:
    open_webui_user_id: str
    email: str
    name: str
    role: str


@dataclass(frozen=True)
class GatewayCredential:
    token: str
    subject: str


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalize_api_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if not url:
        return ""
    return url if url.endswith("/v1") else f"{url}/v1"


def _normalize_management_url(value: str, api_url: str) -> str:
    url = value.strip().rstrip("/") or api_url.removesuffix("/v1")
    return url.rstrip("/")


def _normalize_token(value: object) -> str:
    token = _clean(value)
    if token and not token.startswith("sk-"):
        token = f"sk-{token}"
    return token


def _configured_models(value: str) -> list[str]:
    models: list[str] = []
    seen: set[str] = set()
    for raw in value.split(","):
        model = raw.strip()
        if model and model not in seen:
            models.append(model)
            seen.add(model)
    return models


def _configured_terms(value: str) -> list[str]:
    return [term.strip().casefold() for term in value.split(",") if term.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _is_valid_token(value: object) -> bool:
    token = _clean(value)
    if token.startswith("sk-"):
        token = token[3:]
    return bool(token) and token.isascii() and token.isalnum()


class HappyTokenGateway:
    def __init__(
        self,
        *,
        api_base_url: str,
        management_url: str = "",
        static_api_key: str = "",
        provision_url: str = "",
        provision_secret: str = "",
        newapi_sql_dsn: str = "",
        open_webui_url: str = "",
        open_webui_admin_api_key: str = "",
        identity_provider: str = "oidc",
        token_name: str = "HappyChat Default",
        token_group: str = "default",
        configured_models: list[str] | None = None,
        catalog_url: str = "",
        catalog_secret: str = "",
        enable_group_selection: bool = False,
        excluded_group_terms: list[str] | None = None,
        excluded_model_terms: list[str] | None = None,
        max_channel_test_age_seconds: int = 7 * 24 * 60 * 60,
        max_channel_response_time_ms: int = 30_000,
        credential_ttl_seconds: int = 30,
        sql_connect_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.api_base_url = _normalize_api_url(api_base_url)
        self.management_url = _normalize_management_url(management_url, self.api_base_url)
        self.static_api_key = _normalize_token(static_api_key)
        self.provision_url = provision_url.strip()
        self.provision_secret = provision_secret.strip()
        self.newapi_sql_dsn = newapi_sql_dsn.strip()
        self.open_webui_url = open_webui_url.strip().rstrip("/")
        self.open_webui_admin_api_key = open_webui_admin_api_key.strip()
        self.identity_provider = identity_provider.strip() or "oidc"
        self.token_name = (token_name.strip() or "HappyChat Default")[:50]
        self.token_group = token_group.strip() or "default"
        self.configured_models = list(configured_models or [])
        self.catalog_url = catalog_url.strip()
        self.catalog_secret = catalog_secret.strip() or self.provision_secret
        self.enable_group_selection = enable_group_selection
        self.excluded_group_terms = [
            term.strip().casefold()
            for term in (excluded_group_terms or [])
            if term.strip()
        ]
        self.excluded_model_terms = [
            term.strip().casefold()
            for term in (excluded_model_terms or [])
            if term.strip()
        ]
        self.max_channel_test_age_seconds = max(1, max_channel_test_age_seconds)
        self.max_channel_response_time_ms = max(1, max_channel_response_time_ms)
        self.credential_ttl_seconds = max(1, credential_ttl_seconds)
        self._sql_connect_factory = sql_connect_factory
        self._credential_cache: dict[tuple[str, str], tuple[float, GatewayCredential]] = {}
        self._catalog_cache: tuple[float, list[dict[str, object]]] | None = None
        self._non_chat_catalog_cache: tuple[float, list[dict[str, object]]] | None = None
        self._model_failures: dict[str, tuple[float, int]] = {}
        self._quarantined_models: dict[str, float] = {}
        self._health_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "HappyTokenGateway":
        return cls(
            api_base_url=os.environ.get(
                "HAPPYCHAT_GATEWAY_API_BASE_URL",
                "https://gateway.happy-token.cn/v1",
            ),
            management_url=os.environ.get(
                "HAPPYCHAT_GATEWAY_MANAGEMENT_URL",
                "https://gateway.happy-token.cn",
            ),
            static_api_key=os.environ.get("HAPPYCHAT_GATEWAY_API_KEY", ""),
            provision_url=os.environ.get("HAPPYCHAT_GATEWAY_PROVISION_URL", ""),
            provision_secret=os.environ.get("HAPPYCHAT_GATEWAY_PROVISION_SECRET", ""),
            newapi_sql_dsn=os.environ.get(
                "HAPPYCHAT_NEWAPI_SQL_DSN",
                os.environ.get(
                    "HAPPYIMAGE_NEWAPI_SQL_DSN",
                    os.environ.get("NEWAPI_SQL_DSN", ""),
                ),
            ),
            open_webui_url=os.environ.get("HAPPYCHAT_OPEN_WEBUI_URL", ""),
            open_webui_admin_api_key=os.environ.get(
                "HAPPYCHAT_OPEN_WEBUI_ADMIN_API_KEY", ""
            ),
            identity_provider=os.environ.get(
                "HAPPYCHAT_OPEN_WEBUI_OAUTH_PROVIDER", "oidc"
            ),
            token_name=os.environ.get(
                "HAPPYCHAT_GATEWAY_TOKEN_NAME", "HappyChat Default"
            ),
            token_group=os.environ.get("HAPPYCHAT_GATEWAY_GROUP", "default"),
            configured_models=_configured_models(
                os.environ.get("HAPPYCHAT_GATEWAY_MODELS", "")
            ),
            catalog_url=os.environ.get("HAPPYCHAT_GATEWAY_CATALOG_URL", ""),
            catalog_secret=os.environ.get("HAPPYCHAT_GATEWAY_CATALOG_SECRET", ""),
            enable_group_selection=_env_bool(
                "HAPPYCHAT_GATEWAY_ENABLE_GROUP_SELECTION"
            ),
            excluded_group_terms=_configured_terms(
                os.environ.get("HAPPYCHAT_GATEWAY_EXCLUDED_GROUP_TERMS", "")
            ),
            excluded_model_terms=_configured_terms(
                os.environ.get(
                    "HAPPYCHAT_GATEWAY_CHAT_EXCLUDED_MODEL_TERMS",
                    "image,audio,realtime",
                )
            ),
            max_channel_test_age_seconds=_env_int(
                "HAPPYCHAT_GATEWAY_MAX_CHANNEL_TEST_AGE_SECONDS", 7 * 24 * 60 * 60
            ),
            max_channel_response_time_ms=_env_int(
                "HAPPYCHAT_GATEWAY_MAX_CHANNEL_RESPONSE_TIME_MS", 30_000
            ),
            credential_ttl_seconds=_env_int(
                "HAPPYCHAT_GATEWAY_CREDENTIAL_TTL_SECONDS", 30
            ),
        )

    def validate(self) -> None:
        if not self.api_base_url:
            raise GatewayConfigurationError("Happy Token gateway API URL is not configured")
        if self.static_api_key:
            if self.catalog_url and not self.catalog_secret:
                raise GatewayConfigurationError(
                    "Happy Token catalog secret is not configured"
                )
            return
        if self.catalog_url and not self.catalog_secret:
            raise GatewayConfigurationError(
                "Happy Token catalog secret is not configured"
            )
        if self.provision_url and not self.provision_secret:
            raise GatewayConfigurationError(
                "Happy Token provisioning secret is not configured"
            )
        if not self.provision_url and not self.newapi_sql_dsn:
            raise GatewayConfigurationError(
                "Happy Token per-user provisioning is not configured"
            )
        if not self.open_webui_url or not self.open_webui_admin_api_key:
            raise GatewayConfigurationError(
                "Open WebUI identity resolver credentials are not configured"
            )

    def model_catalog(
        self, *, include_excluded_models: bool = False
    ) -> list[dict[str, object]]:
        if self.configured_models:
            return self._filter_quarantined_models(
                [
                    self._model_item(model)
                    for model in self.configured_models
                    if include_excluded_models or not self._model_is_excluded(model)
                ]
            )

        now = time.monotonic()
        cache = (
            self._non_chat_catalog_cache
            if include_excluded_models
            else self._catalog_cache
        )
        if cache and cache[0] > now:
            return self._filter_quarantined_models(cache[1])

        if self.catalog_url:
            models = self._catalog_via_endpoint(
                include_excluded_models=include_excluded_models
            )
        elif self.enable_group_selection and self.newapi_sql_dsn:
            models = self.available_group_models(
                include_excluded_models=include_excluded_models
            )
        elif self.newapi_sql_dsn:
            models = [
                self._model_item(model)
                for model in self.available_model_ids(
                    include_excluded_models=include_excluded_models
                )
            ]
        else:
            models = self._pricing_catalog(
                include_excluded_models=include_excluded_models
            )
        if not models:
            raise GatewayRequestError("Happy Token has no recently healthy models")
        if include_excluded_models:
            self._non_chat_catalog_cache = (now + 60, models)
        else:
            self._catalog_cache = (now + 60, models)
        return self._filter_quarantined_models(models)

    def available_model_ids(
        self, *, include_excluded_models: bool = False
    ) -> list[str]:
        connection = None
        try:
            connection = self._make_sql_connection(self.newapi_sql_dsn)
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT DISTINCT ability.model
                        FROM abilities AS ability
                        JOIN channels AS channel ON channel.id = ability.channel_id
                        WHERE ability.enabled = true
                          AND ability."group" = %s
                          AND channel.status = 1
                          AND channel.test_time > 0
                          AND channel.test_time >= EXTRACT(EPOCH FROM NOW())::bigint - %s
                          AND channel.response_time > 0
                          AND channel.response_time <= %s
                        ORDER BY ability.model
                        """,
                        (
                            self.token_group,
                            self.max_channel_test_age_seconds,
                            self.max_channel_response_time_ms,
                        ),
                    )
                    return [
                        model
                        for row in cursor.fetchall()
                        if row and (model := _clean(row[0]))
                        and (
                            include_excluded_models
                            or not self._model_is_excluded(model)
                        )
                    ]
        except (GatewayConfigurationError, GatewayRequestError):
            raise
        except Exception:
            raise GatewayRequestError("Happy Token model health lookup failed") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def available_group_models(
        self, *, include_excluded_models: bool = False
    ) -> list[dict[str, object]]:
        connection = None
        try:
            connection = self._make_sql_connection(self.newapi_sql_dsn)
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT key, value
                        FROM options
                        WHERE key IN ('GroupRatio', 'UserUsableGroups')
                        """
                    )
                    option_values = {
                        _clean(row[0]): row[1]
                        for row in cursor.fetchall()
                        if row and len(row) > 1
                    }
                    ratios = self._json_object_option(
                        option_values.get("GroupRatio"), "GroupRatio"
                    )
                    usable_groups = self._json_object_option(
                        option_values.get("UserUsableGroups"), "UserUsableGroups"
                    )
                    cursor.execute(
                        """
                        SELECT DISTINCT ability."group", ability.model
                        FROM abilities AS ability
                        JOIN channels AS channel ON channel.id = ability.channel_id
                        WHERE ability.enabled = true
                          AND channel.status = 1
                          AND channel.test_time > 0
                          AND channel.test_time >= EXTRACT(EPOCH FROM NOW())::bigint - %s
                          AND channel.response_time > 0
                          AND channel.response_time <= %s
                        ORDER BY ability."group", ability.model
                        """,
                        (
                            self.max_channel_test_age_seconds,
                            self.max_channel_response_time_ms,
                        ),
                    )
                    items: list[dict[str, object]] = []
                    for row in cursor.fetchall():
                        group = _clean(row[0] if row else "")
                        model = _clean(row[1] if row and len(row) > 1 else "")
                        if (
                            not group
                            or not model
                            or group not in usable_groups
                            or group not in ratios
                            or self._group_is_excluded(group)
                            or (
                                not include_excluded_models
                                and self._model_is_excluded(model)
                            )
                        ):
                            continue
                        ratio = self._numeric_ratio(ratios[group], group)
                        items.append(self._group_model_item(model, group, ratio))
                    return items
        except (GatewayConfigurationError, GatewayRequestError):
            raise
        except Exception:
            raise GatewayRequestError("Happy Token group catalog lookup failed") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    @staticmethod
    def _json_object_option(value: object, name: str) -> dict[str, object]:
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
        except json.JSONDecodeError:
            raise GatewayRequestError(f"Happy Token {name} configuration is invalid") from None
        if not isinstance(parsed, dict):
            raise GatewayRequestError(f"Happy Token {name} configuration is invalid")
        return {_clean(key): item for key, item in parsed.items() if _clean(key)}

    @staticmethod
    def _numeric_ratio(value: object, group: str) -> int | float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GatewayRequestError(
                f"Happy Token ratio for group {group} is invalid"
            )
        return value

    def _group_is_excluded(self, group: str) -> bool:
        normalized = group.casefold()
        return any(term in normalized for term in self.excluded_group_terms)

    def _model_is_excluded(self, model: str) -> bool:
        normalized = model.casefold()
        return any(term in normalized for term in self.excluded_model_terms)

    def _catalog_via_endpoint(
        self, *, include_excluded_models: bool = False
    ) -> list[dict[str, object]]:
        separator = "&" if "?" in self.catalog_url else "?"
        query = (
            "all=true"
            if self.enable_group_selection
            else f"group={urllib.parse.quote(self.token_group, safe='')}"
        )
        if include_excluded_models:
            query = f"{query}&include_excluded_models=true"
        url = f"{self.catalog_url}{separator}{query}"
        payload = self._request_json(
            "GET",
            url,
            headers={
                "Authorization": f"Bearer {self.catalog_secret}",
                "Accept": "application/json",
            },
            timeout=15,
        )
        items = payload.get("data") if isinstance(payload, dict) else None
        models = []
        for item in items if isinstance(items, list) else []:
            model = _clean(item.get("id")) if isinstance(item, dict) else ""
            upstream_model = (
                _clean(item.get("upstream_model"))
                if isinstance(item, dict)
                else ""
            )
            if (
                model
                and (
                    include_excluded_models
                    or not self._model_is_excluded(upstream_model or model)
                )
            ):
                models.append(dict(item))
        return models

    def _pricing_catalog(
        self, *, include_excluded_models: bool = False
    ) -> list[dict[str, object]]:
        payload = self._request_json(
            "GET",
            f"{self.management_url}/api/pricing",
            headers={"Accept": "application/json"},
            timeout=15,
        )
        items = payload.get("data") if isinstance(payload, dict) else None
        models: list[dict[str, object]] = []
        seen: set[str] = set()
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            model = _clean(item.get("model_name") or item.get("model"))
            endpoint_types = item.get("supported_endpoint_types")
            if (
                not model
                or model in seen
                or (
                    not include_excluded_models
                    and self._model_is_excluded(model)
                )
                or (
                    isinstance(endpoint_types, list)
                    and "openai" not in endpoint_types
                )
            ):
                continue
            models.append(self._model_item(model))
            seen.add(model)
        return models

    def _filter_quarantined_models(
        self, models: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        now = time.monotonic()
        with self._health_lock:
            expired = [
                model for model, until in self._quarantined_models.items() if until <= now
            ]
            for model in expired:
                self._quarantined_models.pop(model, None)
            quarantined = set(self._quarantined_models)
        return [dict(item) for item in models if item.get("id") not in quarantined]

    def open_chat_completion(
        self, identity: UserIdentity, payload: dict[str, object]
    ):
        return self.open_model_request(
            identity,
            path="/chat/completions",
            body=json.dumps(payload).encode(),
            content_type="application/json",
            accept="text/event-stream" if payload.get("stream") else "application/json",
        )

    def open_model_request(
        self,
        identity: UserIdentity,
        *,
        path: str,
        body: bytes,
        content_type: str,
        accept: str,
    ):
        self.validate()
        if path not in MODEL_POST_PATHS:
            raise GatewayRequestError("The Happy Token capability is unavailable", status=404)

        requested_model = self._request_model(body, content_type)
        upstream_model, selected_group = self._resolve_model_selection(
            requested_model,
            include_excluded_models=path.startswith("/images/"),
        )
        if upstream_model and upstream_model != requested_model:
            body = self._replace_request_model(
                body,
                content_type,
                requested_model,
                upstream_model,
            )
        credential = self.credential_for(identity, group=selected_group)
        request = urllib.request.Request(
            f"{self.api_base_url}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {credential.token}",
                "Content-Type": content_type or "application/octet-stream",
                "Accept": accept or "*/*",
            },
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=300)
            self._record_model_success(requested_model)
            return response
        except urllib.error.HTTPError as error:
            error.read(MAX_RESPONSE_BYTES)
            self._record_model_failure(requested_model, error.code)
            raise self._gateway_http_error(error.code) from None
        except urllib.error.URLError:
            raise GatewayRequestError("Happy Token gateway is unavailable") from None

    @staticmethod
    def _request_model(body: bytes, content_type: str) -> str:
        if content_type.lower().startswith("application/json"):
            try:
                payload = json.loads(body or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return ""
            return _clean(payload.get("model")) if isinstance(payload, dict) else ""
        if content_type.lower().startswith("multipart/form-data"):
            match = re.search(
                br'(?i)content-disposition:[^\r\n]*\bname="model"[^\r\n]*\r\n\r\n([^\r\n]*)',
                body,
            )
            return _clean(match.group(1).decode(errors="replace")) if match else ""
        return ""

    def _resolve_model_selection(
        self,
        requested_model: str,
        *,
        include_excluded_models: bool = False,
    ) -> tuple[str, str]:
        if not self.enable_group_selection or not requested_model:
            return requested_model, self.token_group

        catalog = self.model_catalog(
            include_excluded_models=include_excluded_models
        )
        selected = next(
            (item for item in catalog if item.get("id") == requested_model),
            None,
        )
        if selected is not None:
            upstream_model = _clean(selected.get("upstream_model"))
            group = _clean(selected.get("group"))
            if upstream_model and group:
                return upstream_model, group

        default_match = next(
            (
                item
                for item in catalog
                if item.get("upstream_model") == requested_model
                and item.get("group") == self.token_group
            ),
            None,
        )
        if default_match is not None:
            return requested_model, self.token_group
        raise GatewayRequestError(
            "The selected Happy Token model or group is unavailable",
            status=404,
        )

    @staticmethod
    def _replace_request_model(
        body: bytes,
        content_type: str,
        requested_model: str,
        upstream_model: str,
    ) -> bytes:
        if content_type.lower().startswith("application/json"):
            try:
                payload = json.loads(body or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise GatewayRequestError("The model request body is invalid", status=400)
            if not isinstance(payload, dict):
                raise GatewayRequestError("The model request body is invalid", status=400)
            payload["model"] = upstream_model
            return json.dumps(payload, separators=(",", ":")).encode()

        if content_type.lower().startswith("multipart/form-data"):
            requested = requested_model.encode()
            replacement = upstream_model.encode()
            pattern = re.compile(
                br'((?i:content-disposition):[^\r\n]*\bname="model"[^\r\n]*\r\n\r\n)'
                + re.escape(requested)
                + br'(?=\r\n)'
            )
            rewritten, count = pattern.subn(lambda match: match.group(1) + replacement, body, count=1)
            if count != 1:
                raise GatewayRequestError("The multipart model selection is invalid", status=400)
            return rewritten
        raise GatewayRequestError("The model request content type is unsupported", status=400)

    def _record_model_success(self, model: str) -> None:
        if not model:
            return
        with self._health_lock:
            self._model_failures.pop(model, None)
            self._quarantined_models.pop(model, None)

    def _record_model_failure(self, model: str, status: int) -> None:
        if not model or status in {401, 402, 403, 429}:
            return
        now = time.monotonic()
        with self._health_lock:
            if status == 404:
                self._quarantined_models[model] = now + 5 * 60
                self._model_failures.pop(model, None)
                return
            if status not in RETRYABLE_STATUS_CODES:
                return
            started, count = self._model_failures.get(model, (now, 0))
            if now - started > 60:
                started, count = now, 0
            count += 1
            self._model_failures[model] = (started, count)
            if count >= 2:
                self._quarantined_models[model] = now + 2 * 60
                self._model_failures.pop(model, None)

    def credential_for(
        self, identity: UserIdentity, *, group: str | None = None
    ) -> GatewayCredential:
        if self.static_api_key:
            return GatewayCredential(token=self.static_api_key, subject="shared-local-key")

        provider, subject = self._resolve_casdoor_identity(identity)
        effective_group = _clean(group) or self.token_group
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", effective_group):
            raise GatewayRequestError("The selected Happy Token group is invalid", status=400)
        token_name = self._token_name_for_group(effective_group)
        now = time.monotonic()
        cache_key = (subject, effective_group)
        cached = self._credential_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        if self.provision_url:
            credential = self._provision_via_endpoint(
                provider=provider,
                subject=subject,
                identity=identity,
                group=effective_group,
                token_name=token_name,
            )
        else:
            credential = self._provision_via_sql(
                provider=provider,
                subject=subject,
                identity=identity,
                group=effective_group,
                token_name=token_name,
            )
        self._credential_cache[cache_key] = (
            now + self.credential_ttl_seconds,
            credential,
        )
        return credential

    def _token_name_for_group(self, group: str) -> str:
        if group == self.token_group:
            return self.token_name
        candidate = f"HappyChat {group}"
        if len(candidate) <= 50:
            return candidate
        digest = hashlib.sha256(group.encode()).hexdigest()[:8]
        return f"{candidate[:41]}-{digest}"

    def _resolve_casdoor_identity(self, identity: UserIdentity) -> tuple[str, str]:
        self.validate()
        user_id = urllib.parse.quote(identity.open_webui_user_id, safe="")
        payload = self._request_json(
            "GET",
            f"{self.open_webui_url}/api/v1/users/{user_id}",
            headers={
                "Authorization": f"Bearer {self.open_webui_admin_api_key}",
                "Accept": "application/json",
            },
            timeout=15,
        )
        oauth = payload.get("oauth") if isinstance(payload, dict) else None
        provider_data = oauth.get(self.identity_provider) if isinstance(oauth, dict) else None
        subject = _clean(provider_data.get("sub")) if isinstance(provider_data, dict) else ""
        if not subject:
            raise GatewayRequestError(
                "The Open WebUI account is not linked to Casdoor",
                status=403,
            )
        return self.identity_provider, subject

    def _provision_via_endpoint(
        self,
        *,
        provider: str,
        subject: str,
        identity: UserIdentity,
        group: str | None = None,
        token_name: str | None = None,
    ) -> GatewayCredential:
        effective_group = _clean(group) or self.token_group
        effective_token_name = _clean(token_name) or self._token_name_for_group(
            effective_group
        )
        model_ids = []
        for item in self.model_catalog(include_excluded_models=True):
            if self.enable_group_selection and item.get("group") != effective_group:
                continue
            model = _clean(item.get("upstream_model") or item.get("id"))
            if model:
                model_ids.append(model)
        payload = self._request_json(
            "POST",
            self.provision_url,
            headers={
                "Authorization": f"Bearer {self.provision_secret}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            payload={
                "provider": provider,
                "subject": subject,
                "email": identity.email,
                "name": identity.name,
                "token_name": effective_token_name,
                "group": effective_group,
                "models": model_ids,
            },
            timeout=20,
        )
        token = _normalize_token(payload.get("token") if isinstance(payload, dict) else "")
        if not isinstance(payload, dict) or payload.get("ok") is not True or not token:
            raise GatewayRequestError("Happy Token provisioning returned an invalid response")
        return GatewayCredential(token=token, subject=subject)

    def _provision_via_sql(
        self,
        *,
        provider: str,
        subject: str,
        identity: UserIdentity,
        group: str | None = None,
        token_name: str | None = None,
    ) -> GatewayCredential:
        if provider not in {"oidc", "casdoor"}:
            raise GatewayConfigurationError(
                "HappyChat SQL provisioning only supports Casdoor/OIDC identities"
            )
        connection = None
        try:
            connection = self._make_sql_connection(self.newapi_sql_dsn)
            with connection:
                with connection.cursor() as cursor:
                    user_id = self._find_or_create_user(
                        cursor,
                        subject=subject,
                        identity=identity,
                    )
                    effective_group = _clean(group) or self.token_group
                    effective_token_name = _clean(token_name) or self._token_name_for_group(
                        effective_group
                    )
                    token = self._find_or_create_token(
                        cursor,
                        user_id=user_id,
                        group=effective_group,
                        token_name=effective_token_name,
                    )
            return GatewayCredential(token=_normalize_token(token), subject=subject)
        except (GatewayConfigurationError, GatewayRequestError):
            raise
        except Exception:
            raise GatewayRequestError("Happy Token user provisioning failed") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def _make_sql_connection(self, dsn: str):
        if self._sql_connect_factory is not None:
            return self._sql_connect_factory(dsn)
        try:
            import psycopg2
        except ImportError:
            raise GatewayConfigurationError(
                "psycopg2 is required for HappyChat SQL provisioning"
            ) from None
        return psycopg2.connect(dsn)

    def _find_or_create_user(
        self, cursor: Any, *, subject: str, identity: UserIdentity
    ) -> int:
        cursor.execute(
            "SELECT id FROM users WHERE oidc_id = %s AND deleted_at IS NULL ORDER BY id LIMIT 1",
            (subject,),
        )
        row = cursor.fetchone()
        if row:
            return int(row[0])

        if identity.email:
            cursor.execute(
                "SELECT id, oidc_id FROM users WHERE email = %s AND deleted_at IS NULL ORDER BY id LIMIT 1",
                (identity.email,),
            )
            row = cursor.fetchone()
            if row:
                user_id = int(row[0])
                existing_subject = _clean(row[1] if len(row) > 1 else "")
                if existing_subject and existing_subject != subject:
                    raise GatewayRequestError(
                        "Happy Token account identity does not match Casdoor",
                        status=409,
                    )
                cursor.execute(
                    "UPDATE users SET oidc_id = %s WHERE id = %s AND (oidc_id IS NULL OR oidc_id = '')",
                    (subject, user_id),
                )
                return user_id

        digest = hashlib.sha256(subject.encode()).hexdigest()[:10]
        username = f"happychat-{digest}"[:20]
        now = int(time.time())
        cursor.execute(
            """
            INSERT INTO users (
                username, password, display_name, role, status, email,
                access_token, quota, used_quota, request_count, "group",
                created_at, last_login_at, oidc_id
            )
            VALUES (%s, %s, %s, 1, 1, %s, %s, 0, 0, 0, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                username,
                secrets.token_urlsafe(32),
                identity.name or username,
                identity.email,
                secrets.token_hex(16),
                self.token_group,
                now,
                now,
                subject,
            ),
        )
        row = cursor.fetchone()
        if not row:
            raise GatewayRequestError("Happy Token user provisioning failed")
        return int(row[0])

    def _find_or_create_token(
        self,
        cursor: Any,
        *,
        user_id: int,
        group: str | None = None,
        token_name: str | None = None,
    ) -> str:
        effective_group = _clean(group) or self.token_group
        effective_token_name = _clean(token_name) or self.token_name
        cursor.execute(
            """
            SELECT id, key FROM tokens
            WHERE user_id = %s AND name = %s AND status = 1 AND deleted_at IS NULL
            ORDER BY id LIMIT 1
            """,
            (user_id, effective_token_name),
        )
        row = cursor.fetchone()
        if row:
            token_id = int(row[0])
            token = _clean(row[1])
            if not _is_valid_token(token):
                token = secrets.token_hex(24)
                cursor.execute("UPDATE tokens SET key = %s WHERE id = %s", (token, token_id))
            if effective_group:
                cursor.execute(
                    'UPDATE tokens SET "group" = %s WHERE id = %s',
                    (effective_group, token_id),
                )
            return token

        now = int(time.time())
        token = secrets.token_hex(24)
        cursor.execute(
            """
            INSERT INTO tokens (
                user_id, key, status, name, created_time, accessed_time,
                expired_time, remain_quota, unlimited_quota,
                model_limits_enabled, model_limits, allow_ips, used_quota,
                "group", cross_group_retry
            )
            VALUES (%s, %s, 1, %s, %s, %s, -1, 0, true, false, '', '', 0, %s, false)
            RETURNING id
            """,
            (user_id, token, effective_token_name, now, now, effective_group),
        )
        if not cursor.fetchone():
            raise GatewayRequestError("Happy Token token provisioning failed")
        return token

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, object] | None = None,
        timeout: int,
    ) -> dict[str, object]:
        body = json.dumps(payload).encode() if payload is not None else None
        for attempt in range(2):
            request = urllib.request.Request(
                url,
                data=body,
                headers=headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read(MAX_RESPONSE_BYTES)
                parsed = json.loads(raw or b"{}")
                if not isinstance(parsed, dict):
                    raise GatewayRequestError("Happy Token returned an invalid response")
                return parsed
            except urllib.error.HTTPError as error:
                error.read(MAX_RESPONSE_BYTES)
                if attempt == 0 and error.code in RETRYABLE_STATUS_CODES:
                    continue
                raise self._gateway_http_error(error.code) from None
            except (urllib.error.URLError, TimeoutError):
                if attempt == 0:
                    continue
                raise GatewayRequestError("Happy Token gateway is unavailable") from None
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise GatewayRequestError("Happy Token returned an invalid response") from None
        raise GatewayRequestError("Happy Token gateway is unavailable")

    @staticmethod
    def _gateway_http_error(status: int) -> GatewayRequestError:
        if status in {400, 422}:
            return GatewayRequestError(
                "The selected Happy Token model does not support this capability or request",
                status=400,
            )
        if status in {401, 403}:
            return GatewayRequestError(
                "Happy Token credential was rejected",
                status=403,
            )
        if status in {402, 429}:
            return GatewayRequestError(
                "Happy Token quota is insufficient or rate limited",
                status=429,
            )
        if status == 404:
            return GatewayRequestError("The selected Happy Token model was not found", status=404)
        return GatewayRequestError("Happy Token gateway request failed", status=502)

    @staticmethod
    def _model_item(model: str) -> dict[str, object]:
        return {
            "id": model,
            "object": "model",
            "created": 0,
            "owned_by": "happy-token",
        }

    @classmethod
    def _group_model_item(
        cls, model: str, group: str, ratio: int | float
    ) -> dict[str, object]:
        ratio_label = f"{ratio:g}"
        item = cls._model_item(f"{group}::{model}")
        item.update(
            {
                "name": f"{model} · {group} ({ratio_label}×)",
                "group": group,
                "group_ratio": ratio,
                "upstream_model": model,
            }
        )
        return item
