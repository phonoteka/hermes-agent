"""Read authoritative task and message facts for authenticated webhook events."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from .client import Bitrix24Client


@dataclass(frozen=True, slots=True)
class TaskFacts:
    """Immutable task context returned by Bitrix24, with string identifiers."""

    task_id: str
    title: str
    description: str
    created_by: str
    changed_by: str
    chat_id: str
    group_id: str


@dataclass(frozen=True, slots=True)
class MessageFacts:
    """Immutable message identity, author and unmodified full text."""

    message_id: str
    author_id: str
    text: str


class Bitrix24SourceUnavailable(Exception):
    """The authoritative REST source failed; the event may be retried."""


class Bitrix24EventSource(Protocol):
    """Read event context without mutating tasks or messages."""

    def get_task(self, task_id: str) -> TaskFacts | None:
        """Return task facts or None if inaccessible; raise on source failure."""
        ...

    def get_task_message(self, chat_id: str, message_id: str) -> MessageFacts | None:
        """Return the exact message or None; raise on source failure."""
        ...


def _text(value: object) -> str:
    return str(value) if isinstance(value, (str, int)) else ""


class RestBitrix24EventSource:
    """Translate REST facts and failures into the inbound event source contract."""

    def __init__(self, client: Bitrix24Client) -> None:
        """Borrow the client for synchronous, read-only REST lookups."""
        self._client = client

    def get_task(self, task_id: str) -> TaskFacts | None:
        """Return task facts, None for absent task data, or raise on REST failure."""
        try:
            response = self._client.call(
                "tasks.task.get", taskId=task_id,
                select=["ID", "TITLE", "DESCRIPTION", "CREATED_BY", "CHANGED_BY", "CHAT_ID", "GROUP_ID"],
            )
        except Exception as exc:
            raise Bitrix24SourceUnavailable("task source unavailable") from exc
        result = response.get("result") if isinstance(response, dict) else None
        task = result.get("task") if isinstance(result, dict) else None
        if not isinstance(task, dict):
            return None
        return TaskFacts(
            _text(task.get("id")), _text(task.get("title")), _text(task.get("description")),
            _text(task.get("createdBy")), _text(task.get("changedBy")), _text(task.get("chatId")),
            _text(task.get("groupId")),
        )

    def get_task_message(self, chat_id: str, message_id: str) -> MessageFacts | None:
        """Read one message; return None on missing/mismatched ID, raise on REST failure."""
        try:
            response = self._client.call(
                "im.dialog.messages.get", DIALOG_ID=f"chat{chat_id}",
                FIRST_ID=int(message_id) - 1, LIMIT=1,
            )
        except Exception as exc:
            raise Bitrix24SourceUnavailable("message source unavailable") from exc
        result = response.get("result") if isinstance(response, dict) else None
        messages = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict):
            return None
        message = messages[0]
        if _text(message.get("id")) != message_id:
            return None
        return MessageFacts(message_id, _text(message.get("author_id")), _text(message.get("text")))


def bot_user_id_from_webhook_url(url: str) -> str:
    """Return the numeric old REST webhook user ID; raise ValueError if absent."""
    match = re.fullmatch(r"/rest/([1-9][0-9]*)/[^/]+/?", urlsplit(url).path)
    if match is None:
        raise ValueError("missing Bitrix24 webhook user ID")
    return match.group(1)
