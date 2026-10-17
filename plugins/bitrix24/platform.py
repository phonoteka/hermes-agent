"""Bitrix24 gateway platform: one Hermes session per Bitrix24 task or chat dialog.

Inbound: ``POST <route_path>`` receives Bitrix24 outgoing-webhook events, authenticates and
normalizes them with the plugin's ingress handler, and dispatches accepted events with a stable
address — ``task:<task_id>`` for task comments, ``chat:<dialog_id>`` for chat messages — so every
event of one task lands in one gateway session regardless of author.

Outbound: ``send()`` posts the agent's reply back to that address (task comment or chat message),
converting the agent's Markdown into Bitrix24 BBCode. Text is not otherwise changed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import web

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

from .attachments import _materialize_attachments
from .client import Bitrix24Client, _sanitize_error_text
from .handoff import _agent_request_from_result
from .http import parse_bitrix24_webhook_payload
from .ingress import _make_webhook_ingress_handler

logger = logging.getLogger(__name__)

PLATFORM_NAME = "bitrix24"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8644
DEFAULT_ROUTE_PATH = "/bitrix24/webhook"
MAX_BODY_BYTES = 1_048_576
TASK_PREFIX = "task:"
CHAT_PREFIX = "chat:"

# Ingress rejection reason -> HTTP status (same contract as the former core webhook route).
_STATUS_BY_REASON = {
    "invalid_payload": 400,
    "unknown_payload_shape": 400,
    "invalid_webhook_url": 503,
    "webhook_auth_not_configured": 503,
    "unauthorized_application_token": 401,
    "unauthorized_source": 403,
    "unsupported_event": 200,
    "not_targeted": 200,
    "project_not_allowed": 200,
    "group_not_allowed": 200,
    "duplicate_event": 200,
    "task_not_found": 200,
    "message_not_found": 200,
    "system_message": 200,
    "source_unavailable": 502,
}
_ERROR_STATUSES = {401, 403, 502, 503}

PLATFORM_HINT = (
    "You are talking inside a Bitrix24 task (or chat). Each conversation is bound to one task and "
    "keeps its history across comments. Your normal reply is posted to that task as a comment "
    "automatically; do not also post it with bitrix24_task_comment_add. Bitrix24 shows BBCode: keep "
    "formatting simple (bold, italic, code, links). You are woken by: a new task of a served group "
    "(start its workflow as instructed), a Canon Human Gate wake for this task's workflow (post the "
    "card as your reply and wait), and comments that mention the bot. Never use clarify here: the "
    "user answers only with a new comment. Read a tagged comment in the context of this "
    "conversation: when a Canon Human Gate of this task is waiting, a comment that approves, asks "
    "for changes or stops is the user's decision - pass it to canon_checkpoint continue-paused with "
    "the stored continuePaused request (approve; revise with the user's corrections as "
    "revisionInstructions; abort); any other comment is a question or request to answer directly. "
    "When it is unclear whether a comment is a gate decision, ask in your reply instead of "
    "deciding. Read the task with bitrix24_task_comments when you need the full discussion."
)
TASK_CREATED = "task_created"
HUMAN_GATE_DELIVERY = (
    "Post the card as your reply in this Bitrix24 task session (it becomes a task comment). "
    "The decision arrives later as a comment mentioning the bot; do not use clarify."
)


def markdown_to_bbcode(text: str) -> str:
    """Convert the agent's common Markdown into Bitrix24 BBCode without changing the words.

    Covers fenced and inline code, bold, italic, ATX headings and inline links. Code content is
    protected from further conversion. Unknown markup is left as written.
    """
    protected: list[str] = []

    def _protect(value: str) -> str:
        protected.append(value)
        return f"\x00{len(protected) - 1}\x00"

    out = re.sub(
        r"```[^\n]*\n(.*?)```",
        lambda m: _protect(f"[CODE]{m.group(1).rstrip(chr(10))}[/CODE]"),
        text, flags=re.DOTALL,
    )
    out = re.sub(r"`([^`\n]+)`", lambda m: _protect(f"[CODE]{m.group(1)}[/CODE]"), out)
    out = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r"[URL=\2]\1[/URL]", out)
    out = re.sub(r"^#{1,6}\s+(.+?)\s*#*\s*$", r"[B]\1[/B]", out, flags=re.MULTILINE)
    out = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"[B]{m.group(1) or m.group(2)}[/B]", out)
    out = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"[I]\1[/I]", out)
    out = re.sub(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])", r"[I]\1[/I]", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], out)


def _address_for(result: Dict[str, Any]) -> Optional[tuple[str, str]]:
    """``(chat_id, thread_id)`` for an accepted event, or None when it has no task/dialog."""
    task_id = str(result.get("task_id") or "").strip()
    if task_id:
        return f"{TASK_PREFIX}{task_id}", task_id
    dialog_id = str(result.get("dialog_id") or "").strip()
    if dialog_id:
        return f"{CHAT_PREFIX}{dialog_id}", dialog_id
    return None


@dataclass(frozen=True, slots=True)
class TaskWorkflowLaunch:
    """Canon workflow every new task of a served group starts with.

    Invariant: both paths are absolute; the workflow file and execution root are owned by config
    ``platforms.bitrix24.extra.task_workflow`` and are not guessed by the agent.
    """

    workflow: str
    execution_root: str

    @classmethod
    def from_extra(cls, extra: Dict[str, Any]) -> Optional["TaskWorkflowLaunch"]:
        """Read ``task_workflow``; None when absent; ValueError when present but incomplete."""
        raw = extra.get("task_workflow")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("platforms.bitrix24.extra.task_workflow must be a mapping")
        workflow = str(raw.get("workflow") or "").strip()
        execution_root = str(raw.get("execution_root") or "").strip()
        if not workflow.startswith("/") or not execution_root.startswith("/"):
            raise ValueError("task_workflow needs absolute workflow and execution_root paths")
        return cls(workflow=workflow, execution_root=execution_root)

    def arguments(self, task_id: str) -> Dict[str, Any]:
        """The exact canon_run arguments that start this task's workflow."""
        return {
            "workflow": self.workflow,
            "execution_root": self.execution_root,
            "mode": "production",
            "wait_mode": "async-no-wait",
            "workflow_input_json": {"taskInput": {"bitrixTaskId": task_id}},
            "human_gate_delivery": HUMAN_GATE_DELIVERY,
        }

    def instruction(self, task_id: str, *, created: bool) -> str:
        """Launch facts for one task turn: start now for a new task, start on request otherwise."""
        call = (
            "call canon_run with exactly these arguments (do not change, shorten or add any):\n"
            f"```json\n{json.dumps(self.arguments(task_id), ensure_ascii=False, indent=2)}\n```\n"
        )
        if created:
            return (
                f"A new task was created. Start its workflow now: {call}"
                "Then reply with one short sentence that the task was taken into work. The workflow "
                "will wake this session with a card when it needs the user's decision."
            )
        return (
            "This task has a Canon workflow that analyses the task and models its solution: it "
            "extracts the facts, studies the project code (including the legacy implementation), "
            "asks the questions that remain open and, after the user's decision on its card, "
            "freezes the task specification. When the comment asks to analyse the task or model "
            "the solution - "
            "for example \"разбери\", \"разбери задачу\", \"смоделируй\", \"смоделируй "
            "решение\" or an explicit request to run the workflow - do not analyse it yourself: "
            f"{call}"
            "Then reply with one short sentence that the workflow was started. Any other question, "
            "including a question about the code, you answer yourself with your own tools. If this "
            "conversation shows that a workflow of this task is already waiting for a decision, "
            "do not start another one unless the comment explicitly asks to restart it; the "
            "comment is then the decision or a question about that card."
        )


def _render_prompt(result: Dict[str, Any], launch: Optional[TaskWorkflowLaunch] = None) -> str:
    """Render the sanitized event facts the agent needs; reply routing is implicit.

    Every task turn carries the configured workflow launch facts: a task_created event starts the
    workflow, any other task event may start it on the user's request.
    """
    facts = {
        "event": result.get("event", ""),
        "event_id": result.get("event_id", ""),
        "task_id": result.get("task_id", ""),
        "task_title": result.get("task_title", ""),
        "dialog_id": result.get("dialog_id", ""),
        "message_id": result.get("message_id", ""),
        "author_id": result.get("author_id", "") or result.get("actor_id", ""),
        "message": result.get("message", ""),
        "task_description": result.get("task_description", ""),
        "group_id": result.get("group_id", ""),
        "attachments": result.get("attachments", []),
    }
    compact = {key: value for key, value in facts.items() if value not in (None, "", [], {})}
    rendered = (
        "Bitrix24 message for you. Your reply will be posted back here automatically.\n\n"
        f"```json\n{json.dumps(compact, ensure_ascii=False, indent=2)[:4000]}\n```"
    )
    task_id = str(result.get("task_id") or "")
    if launch is not None and task_id:
        created = (result.get("targeting") or {}).get("mode") == TASK_CREATED
        rendered += "\n\n" + launch.instruction(task_id, created=created)
    return rendered


def _redact_capture_payload(payload: dict) -> dict:
    """Copy parsed data, keeping auth.domain but hiding auth, credentials and URLs."""
    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "<redacted>" if key.upper() == "AUTH" or any(
                    marker in key.upper() for marker in ("TOKEN", "SECRET", "PASSWORD", "URL")
                ) else redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    redacted = redact(payload)
    auth = payload.get("auth")
    if isinstance(auth, dict):
        redacted["auth"] = {k: deepcopy(v) if k == "domain" else "<redacted>" for k, v in auth.items()}
    return redacted


class Bitrix24Adapter(BasePlatformAdapter):
    """Gateway adapter owning the Bitrix24 inbound route and replies to tasks/dialogs.

    Invariants: an accepted event's session address depends only on its task (or dialog), never
    on the event id or author; rejected events never reach the agent; ``send`` writes only to
    ``task:``/``chat:`` addresses and never raises on REST failure.
    """

    def __init__(self, config: PlatformConfig) -> None:
        super().__init__(config, Platform(PLATFORM_NAME))
        extra = config.extra or {}
        self._host = str(extra.get("host") or DEFAULT_HOST)
        self._port = int(extra.get("port") or DEFAULT_PORT)
        path = str(extra.get("route_path") or DEFAULT_ROUTE_PATH).strip()
        self._route_path = path if path.startswith("/") else f"/{path}"
        capture = extra.get("capture_path")
        self._capture_path: Optional[Path] = Path(capture) if capture else None
        self._runner: Optional[web.AppRunner] = None
        self._task_workflow = TaskWorkflowLaunch.from_extra(extra)
        self._ingress = _make_webhook_ingress_handler(
            materialize_attachments=_materialize_attachments,
            agent_request_from_result=_agent_request_from_result,
        )

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Bind the inbound route; False (fatal, retryable) when the port cannot be bound."""
        from gateway.platforms.shared_ingress import bind_listener

        app = web.Application(client_max_size=MAX_BODY_BYTES)
        app.router.add_post(self._route_path, self._handle_request)
        app.router.add_get("/health", self._handle_health)
        try:
            self._runner = await bind_listener(self, app, self._host, self._port, self._route_path)
        except OSError as exc:
            message = f"[bitrix24] cannot listen on {self._host}:{self._port}: {exc}"
            logger.error(message)
            self._set_fatal_error("bitrix24_bind_failed", message, retryable=True)
            return False
        self._mark_connected(
            listener_base=f"http://{self._host}:{self._port}" if self._runner is not None else None)
        logger.info("[bitrix24] listening on %s:%d%s", self._host, self._port, self._route_path)
        return True

    async def disconnect(self) -> None:
        """Stop the listener and pending dispatches."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "platform": PLATFORM_NAME})

    async def _handle_request(self, request: web.Request) -> web.Response:
        response, payload = await self._process(request)
        self._capture(request, response, payload)
        return response

    async def _process(self, request: web.Request) -> tuple[web.Response, Optional[dict]]:
        try:
            raw_body = await request.read()
        except Exception:
            return web.json_response({"error": "Bad request"}, status=400), None
        try:
            payload = parse_bitrix24_webhook_payload(raw_body, request.headers.get("Content-Type", ""))
        except ValueError:
            return web.json_response({"error": "Cannot parse body"}, status=400), None
        try:
            result = json.loads(await asyncio.to_thread(self._ingress, payload))
        except Exception:
            logger.exception("[bitrix24] ingress handler failed")
            return web.json_response({"error": "Bad request"}, status=400), payload
        if not isinstance(result, dict):
            return web.json_response({"error": "Bad request"}, status=400), payload

        reason = str(result.get("reason") or "")
        if result.get("accepted") is not True:
            status = _STATUS_BY_REASON.get(reason, 400)
            body = ({"error": reason or "invalid_payload"} if status in _ERROR_STATUSES
                    else {"status": "ignored", "reason": reason or "invalid_payload"})
            return web.json_response(body, status=status), payload

        if (result.get("targeting") or {}).get("mode") == TASK_CREATED and self._task_workflow is None:
            return web.json_response({"status": "ignored", "reason": "task_workflow_not_configured"},
                                     status=200), payload
        address = _address_for(result)
        if address is None:
            return web.json_response({"status": "ignored", "reason": "no_task_or_dialog"}, status=200), payload
        chat_id, thread_id = address
        author = str(result.get("author_id") or result.get("actor_id") or "bitrix24")
        event_id = str(result.get("event_id") or "")
        source = self.build_source(
            chat_id=chat_id, chat_name=str(result.get("task_title") or chat_id), chat_type="group",
            thread_id=thread_id, user_id=author, user_name=f"bitrix24:{author}",
            message_id=str(result.get("message_id") or event_id) or None,
        )
        event = MessageEvent(
            text=_render_prompt(result, self._task_workflow), message_type=MessageType.TEXT, source=source,
            raw_message=result, message_id=str(result.get("message_id") or event_id) or None,
        )
        logger.info("[bitrix24] event=%s event_id=%s address=%s", result.get("event"), event_id, chat_id)
        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return web.json_response(
            {"status": "accepted", "event": result.get("event", ""), "event_id": event_id, "address": chat_id},
            status=202,
        ), payload

    def _capture(self, request: web.Request, response: web.Response, payload: Optional[dict]) -> None:
        """Append one redacted diagnostic line when configured; never alters the response."""
        if self._capture_path is None:
            return
        try:
            body = json.loads(response.text or "{}")
            reason = body.get("reason") or body.get("error")
            record = {
                "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "status": response.status,
                "reason": reason if isinstance(reason, str) else None,
                "content_type": request.headers.get("Content-Type", ""),
                "payload": _redact_capture_payload(payload) if payload is not None else None,
            }
            self._capture_path.parent.mkdir(parents=True, exist_ok=True)
            with self._capture_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("[bitrix24] capture failed: %s", type(exc).__name__)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Post ``content`` to ``task:<id>`` as a comment or to ``chat:<dialog>`` as a message.

        Unknown addresses fail without any REST call; REST failures return ``success=False`` with
        a redacted error instead of raising.
        """
        chat_id = str(chat_id or "")
        message = markdown_to_bbcode(content or "")
        if chat_id.startswith(TASK_PREFIX) and chat_id[len(TASK_PREFIX):].strip():
            method = "task.commentitem.add"
            payload: Dict[str, Any] = {"taskId": chat_id[len(TASK_PREFIX):].strip(),
                                       "fields": {"POST_MESSAGE": message}}
        elif chat_id.startswith(CHAT_PREFIX) and chat_id[len(CHAT_PREFIX):].strip():
            method = "im.message.add"
            payload = {"DIALOG_ID": chat_id[len(CHAT_PREFIX):].strip(), "MESSAGE": message}
        else:
            return SendResult(success=False, error=f"unsupported bitrix24 address: {chat_id}")
        try:
            response = await asyncio.to_thread(lambda: Bitrix24Client().call(method, **payload))
        except Exception as exc:
            return SendResult(success=False, error=_sanitize_error_text(exc) or type(exc).__name__)
        if not isinstance(response, dict) or response.get("error"):
            error = response.get("error_description") or response.get("error") if isinstance(response, dict) else "invalid_response"
            return SendResult(success=False, error=_sanitize_error_text(error))
        result = response.get("result")
        return SendResult(success=True, message_id=str(result) if result is not None else None)

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Bitrix24 task comments have no typing indicator."""
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Describe the address without a network call."""
        kind = "task" if str(chat_id).startswith(TASK_PREFIX) else "chat"
        return {"name": str(chat_id), "type": "group", "bitrix24_kind": kind}
