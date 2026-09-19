from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tools.url_safety import is_safe_url


_WEBHOOK_PATH_RE = re.compile(r"^/rest/([^/]+)/([^/]+)$")
_URL_REDACTION_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _sanitize_error_text(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return ""
    return _URL_REDACTION_RE.sub("[redacted_url]", text)


def _transport_error_message(exc: BaseException) -> str:
    reason = getattr(exc, "reason", exc)
    reason_type = type(reason).__name__
    reason_text = _sanitize_error_text(reason)
    if reason_type and reason_type != "str":
        if reason_text and reason_text != reason_type:
            return f"transport_error:{reason_type}:{reason_text}"
        return f"transport_error:{reason_type}"
    if reason_text:
        return f"transport_error:{reason_text}"
    return "transport_error"


def _validated_webhook_base_url(webhook_url: str | None) -> str:
    raw = (webhook_url or "").strip()
    if not raw:
        raise RuntimeError("BITRIX24_WEBHOOK_URL is not configured")

    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "https":
        raise RuntimeError("invalid_bitrix_webhook_url")
    if not parsed.netloc or parsed.query or parsed.fragment:
        raise RuntimeError("invalid_bitrix_webhook_url")
    if not is_safe_url(raw):
        raise RuntimeError("invalid_bitrix_webhook_url")

    path = parsed.path.rstrip("/")
    match = _WEBHOOK_PATH_RE.fullmatch(path)
    if match is None:
        raise RuntimeError("invalid_bitrix_webhook_url")

    user_id, webhook_secret = match.groups()
    if not user_id.strip() or not webhook_secret.strip():
        raise RuntimeError("invalid_bitrix_webhook_url")

    return urlunsplit(("https", parsed.netloc, path, "", ""))


class Bitrix24Client:
    def __init__(self, webhook_url: str | None = None) -> None:
        self.webhook_url = _validated_webhook_base_url(webhook_url or os.getenv("BITRIX24_WEBHOOK_URL", ""))

    def call(self, method: str, **payload: Any) -> dict[str, Any]:
        if not self.webhook_url:
            raise RuntimeError("BITRIX24_WEBHOOK_URL is not configured")
        request = Request(
            url=f"{self.webhook_url.rstrip('/')}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise RuntimeError(f"bitrix_http_error:{exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(_transport_error_message(exc)) from exc
        except Exception as exc:
            raise RuntimeError(_transport_error_message(exc)) from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("invalid_json_response") from exc


def _get_client() -> Bitrix24Client:
    return Bitrix24Client()


def _string_id(value: Any, *, field: str) -> str:
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _positive_string_id(value: Any, *, field: str) -> str:
    normalized = _string_id(value, field=field)
    if not normalized.isdigit() or int(normalized) <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return normalized


def _result_id(result: Any, preferred_field: str) -> str | None:
    if isinstance(result, dict):
        candidate_fields = [preferred_field]
        if preferred_field == "task_id":
            candidate_fields.append("taskId")
        elif preferred_field == "chat_id":
            candidate_fields.append("chatId")
        for candidate_field in candidate_fields:
            preferred = result.get(candidate_field)
            if isinstance(preferred, str) and preferred.strip():
                return preferred.strip()
            if isinstance(preferred, int):
                return str(preferred)
        nested = result.get("result")
        if isinstance(nested, dict):
            return _result_id(nested, preferred_field)
        fallback = result.get("id")
        if isinstance(fallback, str) and fallback.strip():
            return fallback.strip()
        if isinstance(fallback, int):
            return str(fallback)
    return None


def _json_envelope(
    *,
    method: str,
    id_field: str | None = None,
    identifier: str | None = None,
    result: Any = None,
    error: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "success": error is None,
        "method": method,
    }
    if id_field and identifier is not None:
        payload[id_field] = identifier
    if error is None:
        payload["result"] = result
    else:
        payload["error"] = error
    return json.dumps(payload)


def _call_method(
    method: str,
    *,
    id_field: str | None = None,
    identifier: str | None = None,
    **payload: Any,
) -> str:
    try:
        result = _get_client().call(method, **payload)
    except Exception as exc:
        return _json_envelope(method=method, id_field=id_field, identifier=identifier, error=str(exc))
    derived_identifier = identifier
    if derived_identifier is None and id_field is not None:
        derived_identifier = _result_id(result, "task_id" if id_field == "task_id" else "chat_id")
    return _json_envelope(
        method=method,
        id_field=id_field,
        identifier=derived_identifier,
        result=result,
    )
