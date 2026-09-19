from __future__ import annotations

from typing import Any

from .attachments import _attachment_vision_summary
from .events import _CHAT_EVENTS, _COMMENT_EVENTS, _optional_text


def _minimized_agent_attachment(attachment: dict[str, Any]) -> dict[str, Any]:
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


def _task_comment_from_result(result: dict[str, Any]) -> dict[str, Any]:
    normalized_turn = result.get("normalized_turn")
    comment_id = ""
    if isinstance(normalized_turn, dict):
        comment_id = _optional_text(normalized_turn.get("object_id"))
    comment_id = comment_id or _optional_text(result.get("event_id"))
    comment: dict[str, Any] = {
        "id": comment_id,
        "message": _optional_text(result.get("message")),
        "source": "mocked_payload",
    }
    task_id = _optional_text(result.get("task_id"))
    if task_id:
        comment["task_id"] = task_id
    return comment


def _task_payload_from_result(result: dict[str, Any], comments: list[dict[str, Any]]) -> dict[str, Any]:
    task_id = _optional_text(result.get("task_id")) or "unknown-task"
    return {
        "id": task_id,
        "title": f"Mocked task {task_id}",
        "comments": comments,
        "source": "mocked_payload",
    }


def _completion_candidate(result: dict[str, Any]) -> dict[str, Any] | None:
    task_id = _optional_text(result.get("task_id"))
    message = _optional_text(result.get("message")).lower()
    if not task_id or not message:
        return None
    completion_phrases = (
        "mark done",
        "mark it done",
        "сделай выполненным",
        "закрой задачу",
        "заверши задачу",
    )
    if not any(phrase in message for phrase in completion_phrases):
        return None
    return {
        "tool": "bitrix24_task_complete",
        "args": {"task_id": task_id},
        "deferred": True,
        "reason": "explicit_completion_request",
    }


def _comment_reply_candidate(result: dict[str, Any]) -> dict[str, Any] | None:
    task_id = _optional_text(result.get("task_id"))
    if not task_id:
        return None
    return {
        "tool": "bitrix24_task_comment_add",
        "args": {"task_id": task_id, "message": ""},
        "deferred": True,
        "reason": "targeted_comment_reply",
        "requires_agent_message": True,
    }


def _chat_reply_candidate(result: dict[str, Any]) -> dict[str, Any] | None:
    chat_id = _optional_text(result.get("dialog_id"))
    if not chat_id:
        return None
    return {
        "tool": "bitrix24_chat_send",
        "args": {"chat_id": chat_id, "message": ""},
        "deferred": True,
        "reason": "targeted_chat_reply",
        "requires_agent_message": True,
    }


def _tool_candidates(result: dict[str, Any]) -> list[dict[str, Any]]:
    event = _optional_text(result.get("event"))
    candidates: list[dict[str, Any]] = []
    if event in _COMMENT_EVENTS:
        comment_reply = _comment_reply_candidate(result)
        if comment_reply is not None:
            candidates.append(comment_reply)
    if event in _CHAT_EVENTS:
        chat_reply = _chat_reply_candidate(result)
        if chat_reply is not None:
            candidates.append(chat_reply)
    completion = _completion_candidate(result)
    if completion is not None:
        candidates.append(completion)
    return candidates


def _agent_request_from_result(result: dict[str, Any]) -> dict[str, Any] | None:
    if result.get("accepted") is not True:
        return None
    event = _optional_text(result.get("event"))
    if event not in (_COMMENT_EVENTS | _CHAT_EVENTS):
        return None
    targeting = result.get("targeting")
    if not isinstance(targeting, dict) or targeting.get("mode") != "mention":
        return None

    attachments = result.get("attachments")
    if not isinstance(attachments, list):
        attachments = []

    agent_attachments: list[dict[str, Any]] = []
    vision_summaries: list[str] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        attachment_copy = _minimized_agent_attachment(attachment)
        vision_summary = _attachment_vision_summary(attachment_copy)
        if vision_summary:
            attachment_copy["vision_summary"] = vision_summary
            vision_summaries.append(vision_summary)
        agent_attachments.append(attachment_copy)

    comments = [_task_comment_from_result(result)]
    task = _task_payload_from_result(result, comments)
    return {
        "proof_mode": "mocked",
        "source": "bitrix24_webhook_ingress",
        "task": task,
        "comments": comments,
        "attachments": agent_attachments,
        "vision_summary": " ".join(vision_summaries).strip(),
        "tool_candidates": _tool_candidates(result),
    }
