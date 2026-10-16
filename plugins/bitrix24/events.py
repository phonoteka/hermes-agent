from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

from .client import _validated_webhook_base_url
from .enrichment import Bitrix24EventSource

SUPPORTED_EVENTS = {
    "ONTASKADD",
    "ONTASKUPDATE",
    "ONTASKCOMMENTADD",
    "ONTASKCOMMENTUPDATE",
    "ONIMBOTMESSAGEADD",
    "ONIMCONNECTORMESSAGEADD",
}

_DEFAULT_BOT_MENTION = "@hermes"
_TASK_EVENTS = {"ONTASKADD", "ONTASKUPDATE"}
_COMMENT_EVENTS = {"ONTASKCOMMENTADD", "ONTASKCOMMENTUPDATE"}
_CHAT_EVENTS = {"ONIMCONNECTORMESSAGEADD", "ONIMBOTMESSAGEADD"}
_MENTION_EVENTS = _COMMENT_EVENTS | _CHAT_EVENTS
_DIALOG_ID_KEYS = ("DIALOG_ID", "DIALOGID", "dialog_id", "dialogId")

_SUPPORTED_EVENTS = SUPPORTED_EVENTS


def _configured_domain() -> str:
    webhook = os.getenv("BITRIX24_WEBHOOK_URL", "")
    try:
        validated = _validated_webhook_base_url(webhook)
    except RuntimeError:
        return ""
    return (urlsplit(validated).hostname or "").strip().lower()


def _configured_application_token() -> str:
    token = os.getenv("BITRIX24_APPLICATION_TOKEN", "")
    if not isinstance(token, str):
        return ""
    return token.strip()


def _payload_domain(payload: dict[str, Any]) -> str:
    auth = payload.get("auth")
    if not isinstance(auth, dict):
        return ""
    domain = auth.get("domain")
    if not isinstance(domain, str):
        return ""
    return domain.strip().lower()


def _payload_application_token(payload: dict[str, Any]) -> str:
    auth = payload.get("auth")
    if not isinstance(auth, dict):
        return ""
    token = auth.get("application_token")
    if not isinstance(token, str):
        return ""
    return token.strip()


def _supported_event(payload: dict[str, Any]) -> str:
    event = payload.get("event")
    if not isinstance(event, str):
        return ""
    return event.strip().upper()


def _event_id(payload: dict[str, Any]) -> str:
    event_id = payload.get("event_id")
    if not isinstance(event_id, str):
        return ""
    return event_id.strip()


def _payload_fields(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    fields = data.get("FIELDS_AFTER" if _supported_event(payload) in (_TASK_EVENTS | _COMMENT_EVENTS) else "FIELDS")
    if not isinstance(fields, dict):
        return {}
    return fields


def _optional_text(value: Any) -> str:
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        return ""
    return value.strip()


def _payload_message(payload: dict[str, Any], *, event: str) -> str:
    fields = _payload_fields(payload)
    if event in _CHAT_EVENTS:
        data = payload.get("data")
        if isinstance(data, dict):
            message = data.get("MESSAGE")
            if isinstance(message, str) and message.strip():
                return message.strip()
        message = fields.get("MESSAGE")
        return message.strip() if isinstance(message, str) else ""
    return ""


def _payload_file_ids(payload: dict[str, Any]) -> list[str]:
    fields = _payload_fields(payload)
    raw_file_ids = fields.get("FILE_IDS")
    if isinstance(raw_file_ids, (str, int)):
        raw_items = [raw_file_ids]
    elif isinstance(raw_file_ids, (list, tuple)):
        raw_items = list(raw_file_ids)
    else:
        return []

    file_ids: list[str] = []
    for item in raw_items:
        file_id = _optional_text(item)
        if file_id:
            file_ids.append(file_id)
    return file_ids


def _payload_dialog_id(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    if isinstance(data, dict):
        for key in _DIALOG_ID_KEYS:
            dialog_id = _optional_text(data.get(key))
            if dialog_id:
                return dialog_id
    fields = _payload_fields(payload)
    for key in _DIALOG_ID_KEYS:
        dialog_id = _optional_text(fields.get(key))
        if dialog_id:
            return dialog_id
    return ""


def _allowed_group_ids() -> frozenset[str]:
    """Bitrix24 workgroups whose tasks the bot serves; unset means none (fail closed)."""
    raw = os.getenv("BITRIX24_ALLOWED_GROUP_IDS", "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _target_mention() -> str:
    return os.getenv("BITRIX24_BOT_MENTION", _DEFAULT_BOT_MENTION).strip().lower()


def _targets_bot(text: str) -> bool:
    normalized_text = text.strip().lower()
    mention = _target_mention()
    return bool(normalized_text and mention and mention in normalized_text)


def _normalized_turn(*, event: str, event_id: str, object_id: str) -> dict[str, str]:
    return {
        "source": "bitrix24",
        "event": event,
        "event_id": event_id,
        "object_id": object_id,
    }


def _sanitized_payload(payload: dict[str, Any], *, event: str, event_id: str) -> dict[str, Any]:
    sanitized: dict[str, Any] = {"event": event, "event_id": event_id}
    data = payload.get("data")
    if not isinstance(data, dict):
        return sanitized

    sanitized_data: dict[str, Any] = {}
    fields_key = "FIELDS_AFTER" if event in (_TASK_EVENTS | _COMMENT_EVENTS) else "FIELDS"
    fields = data.get(fields_key)
    if isinstance(fields, dict):
        sanitized_fields = {
            key: value
            for key, value in fields.items()
            if key
            in {
                "ID",
                "TASK_ID",
                "MESSAGE_ID",
                "TITLE",
                "DESCRIPTION",
                "POST_MESSAGE",
                "MESSAGE",
                "FILE_IDS",
                "DIALOG_ID",
                "DIALOGID",
            }
        }
        if sanitized_fields:
            sanitized_data[fields_key] = sanitized_fields

    message = data.get("MESSAGE")
    if isinstance(message, str) and message.strip():
        sanitized_data["MESSAGE"] = message

    for key in _DIALOG_ID_KEYS:
        dialog_id = _optional_text(data.get(key))
        if dialog_id:
            sanitized_data[key] = dialog_id

    if sanitized_data:
        sanitized["data"] = sanitized_data
    return sanitized


def _accepted_event_response(
    *,
    payload: dict[str, Any],
    event: str,
    event_id: str,
    source_domain: str,
    object_id: str,
    targeting_mode: str,
    task_id: str = "",
    message: str = "",
    file_ids: list[str] | None = None,
    dialog_id: str = "",
) -> dict[str, Any]:
    normalized_turn = _normalized_turn(event=event, event_id=event_id, object_id=object_id)
    result = {
        "accepted": True,
        "reason": "targeted_event",
        "targeted": True,
        "deduped": False,
        "event": event,
        "event_id": event_id,
        "source_domain": source_domain,
        "ingress": "bitrix24_webhook_ingress",
        "payload": _sanitized_payload(payload, event=event, event_id=event_id),
        "targeting": {"mode": targeting_mode, "mention": _target_mention()},
        "normalized_turn": normalized_turn,
        "turns": [normalized_turn],
    }
    if task_id:
        result["task_id"] = task_id
    if message:
        result["message"] = message
    if file_ids:
        result["file_ids"] = file_ids
    if dialog_id:
        result["dialog_id"] = dialog_id
    return result


def _normalize_comment_event(
    payload: dict[str, Any], *, event: str, event_id: str, source_domain: str,
    event_source: Bitrix24EventSource, bot_user_id: str,
) -> dict[str, Any] | None:
    """Read exact task messages and apply author policy; source failures propagate."""
    fields = _payload_fields(payload)
    task_id = _optional_text(fields.get("TASK_ID"))
    message_id = _optional_text(fields.get("MESSAGE_ID"))
    if not task_id or not message_id:
        return None
    task = event_source.get_task(task_id)
    if task is None or not task.chat_id:
        return {"accepted": False, "reason": "task_not_found"}
    if task.group_id not in _allowed_group_ids():
        return {
            "accepted": False, "targeted": False, "reason": "group_not_allowed", "event": event,
            "event_id": event_id, "source_domain": source_domain, "task_id": task_id,
        }
    message = event_source.get_task_message(task.chat_id, message_id)
    if message is None:
        return {"accepted": False, "reason": "message_not_found"}
    mentioned = _targets_bot(message.text)
    rejection = {
        "accepted": False, "targeted": False, "event": event,
        "event_id": event_id, "source_domain": source_domain,
    }
    if message.author_id == "0":
        return {**rejection, "reason": "system_message"}
    if not mentioned:
        return {**rejection, "reason": "not_targeted"}
    result = _accepted_event_response(
        payload=payload, event=event, event_id=event_id, source_domain=source_domain,
        object_id=message_id, targeting_mode="bot_mention" if message.author_id == bot_user_id else "human_comment",
        task_id=task_id, message=message.text, file_ids=_payload_file_ids(payload),
    )
    result.update(task_title=task.title, message=message.text, message_id=message_id,
                  author_id=message.author_id, mentioned=mentioned)
    return result


def _normalize_comment_or_chat_event(
    payload: dict[str, Any], *, event: str, event_id: str, source_domain: str
) -> dict[str, Any] | None:
    message = _payload_message(payload, event=event)
    if not message:
        return None
    if not _targets_bot(message):
        return {
            "accepted": False,
            "reason": "not_targeted",
            "targeted": False,
            "event": event,
            "event_id": event_id,
            "source_domain": source_domain,
        }

    fields = _payload_fields(payload)
    object_id = fields.get("ID")
    task_id = _optional_text(fields.get("TASK_ID"))
    dialog_id = _payload_dialog_id(payload) if event in _CHAT_EVENTS else ""
    if not isinstance(object_id, str) or not object_id.strip():
        object_id = event_id
    return _accepted_event_response(
        payload=payload,
        event=event,
        event_id=event_id,
        source_domain=source_domain,
        object_id=object_id.strip(),
        targeting_mode="mention",
        task_id=task_id,
        message=message,
        file_ids=_payload_file_ids(payload),
        dialog_id=dialog_id,
    )


def _normalize_supported_event(
    payload: dict[str, Any], *, event: str, event_id: str, source_domain: str,
    event_source: Bitrix24EventSource | None = None, bot_user_id: str = "",
) -> dict[str, Any] | None:
    """Normalize supported events; task/comment events require an authoritative source."""
    if event in _TASK_EVENTS:
        # Task create/update is not a message: the bot answers only tagged comments.
        return {
            "accepted": False, "reason": "not_targeted", "targeted": False,
            "event": event, "event_id": event_id, "source_domain": source_domain,
        }
    if event in _COMMENT_EVENTS:
        if event_source is None:
            raise ValueError("task event source is required")
        return _normalize_comment_event(
            payload, event=event, event_id=event_id, source_domain=source_domain,
            event_source=event_source, bot_user_id=bot_user_id,
        )
    if event in _CHAT_EVENTS:
        return _normalize_comment_or_chat_event(
            payload, event=event, event_id=event_id, source_domain=source_domain,
        )
    return None
