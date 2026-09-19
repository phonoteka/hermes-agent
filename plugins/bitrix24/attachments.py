from __future__ import annotations

from typing import Any

from tools.url_safety import is_safe_url

from .client import _get_client as _client_get
from .events import _optional_text

_ATTACHMENT_SIZE_CAP_BYTES = 10 * 1024 * 1024
_get_client = _client_get


def _coerce_attachment_size(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def _attachment_task_context(result: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {
        "source": "bitrix24",
        "event": result.get("event"),
        "event_id": result.get("event_id"),
        "source_domain": result.get("source_domain"),
        "task_id": result.get("task_id"),
        "message": result.get("message", ""),
    }
    return {key: value for key, value in context.items() if value not in {None, ""}}


def _attachment_metadata_payload(response: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    for key in ("result", "file"):
        value = response.get(key)
        if isinstance(value, dict):
            return value
    return response


def _public_attachment_record(attachment: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "file_id",
        "content_type",
        "size_bytes",
        "kind",
        "reason",
        "size_cap_bytes",
        "vision",
    }
    return {key: value for key, value in attachment.items() if key in allowed_keys}


def _classify_attachment(*, file_id: str, metadata: dict[str, Any], task_context: dict[str, Any]) -> dict[str, Any]:
    download_url = _optional_text(metadata.get("DOWNLOAD_URL"))
    content_type = _optional_text(metadata.get("CONTENT_TYPE")) or "application/octet-stream"
    size_bytes = _coerce_attachment_size(metadata.get("SIZE"))
    attachment: dict[str, Any] = {
        "file_id": file_id,
        "content_type": content_type,
        "size_bytes": size_bytes,
    }

    if not download_url.startswith("https://") or not is_safe_url(download_url):
        attachment.update({"kind": "blocked", "reason": "blocked_url"})
        return attachment

    if size_bytes is not None and size_bytes > _ATTACHMENT_SIZE_CAP_BYTES:
        attachment.update(
            {
                "kind": "blocked",
                "reason": "size_cap",
                "size_cap_bytes": _ATTACHMENT_SIZE_CAP_BYTES,
            }
        )
        return attachment

    if not content_type.lower().startswith("image/"):
        attachment.update({"kind": "file", "reason": "non_image"})
        return attachment

    attachment.update(
        {
            "kind": "image",
            "vision": {
                "requested": True,
                "tool": "vision_analyze",
                "task_context": task_context,
            },
        }
    )
    return attachment


def _materialize_attachments(result: dict[str, Any]) -> list[dict[str, Any]]:
    file_ids = result.get("file_ids")
    if not isinstance(file_ids, list) or not file_ids:
        return []

    client = _get_client()
    task_context = _attachment_task_context(result)
    attachments: list[dict[str, Any]] = []
    for file_id_value in file_ids:
        file_id = _optional_text(file_id_value)
        if not file_id:
            continue
        try:
            response = client.call("disk.file.get", id=file_id)
            metadata = _attachment_metadata_payload(response)
            attachment = _classify_attachment(
                file_id=file_id,
                metadata=metadata,
                task_context=task_context,
            )
        except Exception:
            attachment = {
                "file_id": file_id,
                "kind": "blocked",
                "reason": "metadata_unavailable",
            }
        attachments.append(_public_attachment_record(attachment))
    return attachments


def _attachment_vision_summary(attachment: dict[str, Any]) -> str:
    if attachment.get("kind") != "image":
        return ""
    file_id = _optional_text(attachment.get("file_id")) or "unknown"
    return f"Mocked vision summary for image attachment {file_id}"
