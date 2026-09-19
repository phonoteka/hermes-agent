from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import parse_qsl


_PARSE_ERROR = "Invalid Bitrix24 webhook payload"


def parse_bitrix24_webhook_payload(raw_body: bytes, content_type: str = "") -> dict[str, Any]:
    if not isinstance(raw_body, (bytes, bytearray)):
        raise ValueError(_PARSE_ERROR)

    normalized_content_type = _normalize_content_type(content_type)
    body = bytes(raw_body)

    if normalized_content_type == "application/json":
        payload = _parse_json_payload(body)
    elif normalized_content_type == "application/x-www-form-urlencoded":
        payload = _parse_form_payload(body)
    else:
        payload = _parse_with_fallbacks(body)

    if not isinstance(payload, dict):
        raise ValueError(_PARSE_ERROR)

    event_id = _existing_event_id(payload) or _derived_event_id(payload)
    payload["event_id"] = event_id
    return payload


def _normalize_content_type(content_type: str) -> str:
    if not isinstance(content_type, str):
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def _parse_with_fallbacks(raw_body: bytes) -> dict[str, Any]:
    stripped = raw_body.lstrip()
    if stripped.startswith((b"{", b"[")):
        return _parse_json_payload(raw_body)
    try:
        return _parse_form_payload(raw_body)
    except ValueError:
        return _parse_json_payload(raw_body)


def _parse_json_payload(raw_body: bytes) -> dict[str, Any]:
    try:
        decoded = raw_body.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(_PARSE_ERROR) from exc
    if not isinstance(payload, dict):
        raise ValueError(_PARSE_ERROR)
    return payload


def _parse_form_payload(raw_body: bytes) -> dict[str, Any]:
    try:
        decoded = raw_body.decode("utf-8")
        pairs = parse_qsl(decoded, keep_blank_values=True, strict_parsing=True)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(_PARSE_ERROR) from exc

    payload: dict[str, Any] = {}
    for key, value in pairs:
        path = _parse_bracket_key(key)
        if not path:
            raise ValueError(_PARSE_ERROR)
        _assign_nested_value(payload, path, value)
    return payload


def _parse_bracket_key(key: str) -> list[str]:
    if not isinstance(key, str) or not key:
        return []

    head, sep, remainder = key.partition("[")
    if not head:
        return []
    if not sep:
        return [head]

    parts = [head]
    rest = remainder
    while rest:
        part, closing, tail = rest.partition("]")
        if not closing:
            return []
        if not part:
            return []
        parts.append(part)
        if not tail:
            break
        if not tail.startswith("["):
            return []
        rest = tail[1:]
    return parts


def _assign_nested_value(target: dict[str, Any], path: list[str], value: str) -> None:
    cursor: dict[str, Any] = target
    for segment in path[:-1]:
        current = cursor.get(segment)
        if current is None:
            next_cursor: dict[str, Any] = {}
            cursor[segment] = next_cursor
            cursor = next_cursor
            continue
        if not isinstance(current, dict):
            raise ValueError(_PARSE_ERROR)
        cursor = current

    leaf = path[-1]
    current_leaf = cursor.get(leaf)
    if current_leaf is None:
        cursor[leaf] = value
    elif isinstance(current_leaf, list):
        current_leaf.append(value)
    else:
        cursor[leaf] = [current_leaf, value]


def _existing_event_id(payload: dict[str, Any]) -> str:
    event_id = payload.get("event_id")
    if not isinstance(event_id, str):
        return ""
    return event_id.strip()


def _derived_event_id(payload: dict[str, Any]) -> str:
    fields = _payload_fields(payload)
    material = {
        "event": _string_value(payload.get("event")).upper(),
        "ts": _string_value(payload.get("ts")),
        "event_handler_id": _string_value(payload.get("event_handler_id")),
        "auth_member_id": _string_value(_nested_dict_value(payload.get("auth"), "member_id")),
        "data_fields_id": _string_value(fields.get("ID")),
        "data_fields_task_id": _string_value(fields.get("TASK_ID")),
        "data_fields_post_message": _string_value(fields.get("POST_MESSAGE")),
        "data_message": _string_value(_payload_message(payload)),
    }
    canonical = json.dumps(material, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"b24_{digest[:24]}"


def _payload_fields(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    fields = data.get("FIELDS")
    if not isinstance(fields, dict):
        return {}
    return fields


def _payload_message(payload: dict[str, Any]) -> Any:
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    if "MESSAGE" in data:
        return data.get("MESSAGE")
    fields = data.get("FIELDS")
    if not isinstance(fields, dict):
        return ""
    return fields.get("MESSAGE")


def _nested_dict_value(value: Any, key: str) -> Any:
    if not isinstance(value, dict):
        return ""
    return value.get(key)


def _string_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""
