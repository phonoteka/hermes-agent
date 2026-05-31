"""Agent-callable local launch surface for Canon current-gateway solution-modeling proof.

This tool is intentionally narrow: it can only ask the active Hermes gateway
process for one local-only Canon `solution-modeling` launch targeted at an
explicit Telegram chat/thread. It does not expose a general remote command
execution surface.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from hermes_constants import display_hermes_home, get_hermes_home
from tools.registry import registry

_REQUEST_API = "canon_gateway_local_launch.v1"
_REQUEST_ACTION = "launch_solution_modeling"
_DEFAULT_TIMEOUT_SECONDS = 90
_PACKAGED_DEFAULT_TASK_TEXT = "Model a solution and pause for human review."


CANON_GATEWAY_LAUNCH_SCHEMA = {
    "name": "canon_gateway_launch",
    "description": (
        "Ask the active Hermes gateway process to launch Canon current-gateway "
        "solution-modeling through the live /canon run handler and Telegram adapter. "
        "Local-only, profile-scoped, fail-closed. Requires explicit task text plus "
        "target Telegram chat/thread ids."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_text": {
                "type": "string",
                "description": "Explicit operator task text for Canon solution-modeling. Placeholder/default text is rejected.",
            },
            "chat_id": {
                "type": "string",
                "description": "Target Telegram chat id for review-card delivery.",
            },
            "thread_id": {
                "type": "string",
                "description": "Target Telegram topic/thread id for review-card delivery.",
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": 300,
                "description": "How long to wait for the gateway-owned launch response (default 90 seconds).",
            },
        },
        "required": ["task_text", "chat_id", "thread_id"],
        "additionalProperties": False,
    },
}


def _request_root() -> Path:
    """Return the profile-scoped request root for Canon gateway launches.

    pre: Hermes home is resolvable for the active profile.
    post: returns one path under HERMES_HOME reserved for Canon launch request/response files.
    raises: none.
    """

    return Path(get_hermes_home()) / "gateway-control" / "canon-solution-modeling-launch"


def _requests_dir() -> Path:
    """Return the pending-request directory for Canon gateway launches.

    pre: Hermes home is resolvable for the active profile.
    post: returns one child path under the Canon launch request root.
    raises: none.
    """

    return _request_root() / "requests"


def _responses_dir() -> Path:
    """Return the response directory for Canon gateway launches.

    pre: Hermes home is resolvable for the active profile.
    post: returns one child path under the Canon launch request root.
    raises: none.
    """

    return _request_root() / "responses"


def _request_file(request_id: str) -> Path:
    """Return the on-disk request-file path for one request id.

    pre: request_id is non-empty.
    post: returned path lives under the profile-scoped requests directory.
    raises: none.
    """

    return _requests_dir() / f"{request_id}.request.json"


def _response_file(request_id: str) -> Path:
    """Return the on-disk response-file path for one request id.

    pre: request_id is non-empty.
    post: returned path lives under the profile-scoped responses directory.
    raises: none.
    """

    return _responses_dir() / f"{request_id}.response.json"


def _ensure_request_dirs() -> None:
    """Create Canon launch request directories with restrictive permissions.

    pre: current process can write under HERMES_HOME.
    post: request/response directories exist; new paths are chmod 0700 on POSIX.
    raises: OSError when directory creation fails.
    """

    for directory in (_request_root(), _requests_dir(), _responses_dir()):
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write one JSON payload atomically to a local file.

    pre: payload is JSON-serializable and path parent exists.
    post: path contains the full JSON payload; partial writes are not left behind.
    raises: OSError/TypeError when serialization or rename fails.
    """

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _normalize_required_text(value: Any, field_name: str) -> str:
    """Return one stripped non-empty string field.

    pre: value is JSON-like.
    post: returns a stripped non-empty string value.
    raises: ValueError when the field is missing or blank.
    """

    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _reject_placeholder_task_text(task_text: str) -> None:
    """Reject known placeholder/default task text.

    pre: task_text is already normalized non-empty text.
    post: returns only when task_text is explicit operator content.
    raises: ValueError when task_text is obviously placeholder/default.
    """

    lowered = task_text.casefold()
    if task_text == _PACKAGED_DEFAULT_TASK_TEXT:
        raise ValueError("task_text must not equal the packaged default Canon task text")
    if "<explicit" in lowered or "placeholder" in lowered:
        raise ValueError("task_text must be explicit operator content, not a placeholder marker")


def build_gateway_local_launch_request(
    *,
    task_text: str,
    chat_id: str,
    thread_id: str,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Build one fail-closed Canon gateway-local launch request payload.

    pre: caller supplies explicit task text plus Telegram chat/thread ids.
    post: returns a JSON-serializable request payload for the active gateway watcher.
    raises: ValueError when task/chat/thread/timeout are invalid.
    """

    normalized_task = _normalize_required_text(task_text, "task_text")
    _reject_placeholder_task_text(normalized_task)
    normalized_chat = _normalize_required_text(chat_id, "chat_id")
    normalized_thread = _normalize_required_text(thread_id, "thread_id")
    normalized_timeout = int(timeout_seconds)
    if normalized_timeout < 1 or normalized_timeout > 300:
        raise ValueError("timeout_seconds must be between 1 and 300")
    request_id = f"cg-launch-{uuid.uuid4().hex[:12]}"
    return {
        "api": _REQUEST_API,
        "action": _REQUEST_ACTION,
        "requestId": request_id,
        "taskText": normalized_task,
        "target": {
            "platform": "telegram",
            "chatId": normalized_chat,
            "threadId": normalized_thread,
        },
        "timeoutSeconds": normalized_timeout,
        "requester": {
            "pid": os.getpid(),
            "cwd": os.getcwd(),
        },
        "requestedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def request_gateway_local_canon_launch(
    *,
    task_text: str,
    chat_id: str,
    thread_id: str,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Submit one Canon gateway-local launch request and wait for the gateway response.

    pre: an active Hermes gateway process for this profile is expected to watch the
         Canon launch request directory.
    post: writes one request file under HERMES_HOME and returns the gateway-owned
          response payload when it arrives before timeout.
    raises: RuntimeError when the gateway response does not arrive before timeout.
    raises: ValueError when the request payload is invalid.
    """

    request_payload = build_gateway_local_launch_request(
        task_text=task_text,
        chat_id=chat_id,
        thread_id=thread_id,
        timeout_seconds=timeout_seconds,
    )
    request_id = str(request_payload["requestId"])
    _ensure_request_dirs()
    request_path = _request_file(request_id)
    response_path = _response_file(request_id)
    if request_path.exists() or response_path.exists():
        raise RuntimeError(f"request id collision for {request_id}")
    _atomic_write_json(request_path, request_payload)

    deadline = time.monotonic() + int(request_payload["timeoutSeconds"])
    while time.monotonic() < deadline:
        if response_path.is_file():
            payload = json.loads(response_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError(f"gateway response for {request_id} is not an object")
            return payload
        time.sleep(0.25)

    raise RuntimeError(
        "Timed out waiting for active gateway response. "
        f"Expected response file {response_path}. Is the gateway running for {display_hermes_home()}?"
    )


def _check_requirements() -> bool:
    """Expose the tool only when the local filesystem-backed request root is usable.

    pre: none.
    post: returns True when HERMES_HOME is resolvable.
    raises: none.
    """

    try:
        _request_root()
    except Exception:
        return False
    return True


def _handle_tool(args: dict[str, Any], **_: Any) -> str:
    """Run one Canon gateway-local launch request as a JSON-returning Hermes tool.

    pre: args matches CANON_GATEWAY_LAUNCH_SCHEMA.
    post: returns one JSON object describing request submission and gateway-owned response.
    raises: none; failures are encoded in the JSON payload.
    """

    try:
        response = request_gateway_local_canon_launch(
            task_text=str(args.get("task_text") or ""),
            chat_id=str(args.get("chat_id") or ""),
            thread_id=str(args.get("thread_id") or ""),
            timeout_seconds=int(args.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS),
        )
        return json.dumps({"success": True, "response": response}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


registry.register(
    name="canon_gateway_launch",
    toolset="messaging",
    schema=CANON_GATEWAY_LAUNCH_SCHEMA,
    handler=_handle_tool,
    check_fn=_check_requirements,
    emoji="🚦",
)
