"""Agent-callable local transport for Canon CLI-run gateway requests.

This tool only writes a workflow-neutral request envelope for the active Hermes
gateway watcher. It does not construct workflow-specific inputs or inspect
internal request-authority files.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from hermes_constants import display_hermes_home, get_hermes_home
from tools.registry import registry

_REQUEST_API = "canon_gateway_cli_run.v1"
_REQUEST_ACTION = "run"
_DEFAULT_TIMEOUT_SECONDS = 90
_MAX_TIMEOUT_SECONDS = 600
_WAIT_MODE_SYNC_SHORT = "sync-short"
_WAIT_MODE_SYNC_REAL = "sync-real"
_WAIT_MODE_ASYNC_NO_WAIT = "async-no-wait"
_DEFAULT_WAIT_MODE = _WAIT_MODE_SYNC_SHORT
_REAL_WAIT_DEFAULT_TIMEOUT_SECONDS = 600
_SYNC_WAIT_MODES = frozenset({_WAIT_MODE_SYNC_SHORT, _WAIT_MODE_SYNC_REAL})
_ALLOWED_WAIT_MODES = (_WAIT_MODE_SYNC_SHORT, _WAIT_MODE_SYNC_REAL, _WAIT_MODE_ASYNC_NO_WAIT)
_SAFE_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ALLOWED_ARGUMENTS = frozenset(
    {"workflow_name", "chat_id", "thread_id", "inputs", "timeout_seconds", "wait_mode"}
)


CANON_GATEWAY_LAUNCH_SCHEMA = {
    "name": "canon_gateway_launch",
    "description": (
        "Ask the active Hermes gateway process to run a Canon workflow through the "
        "workflow-neutral local CLI-run transport. Local-only, profile-scoped, "
        "fail-closed. Requires explicit workflow plus input object; chat_id defaults to the "
        "current Telegram origin when omitted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workflow_name": {
                "type": "string",
                "description": "Non-empty Canon workflow selector forwarded unchanged to the local CLI-run transport.",
            },
            "chat_id": {
                "type": "string",
                "description": "Optional target Telegram chat id for delivery. Defaults to the current Telegram origin chat when omitted.",
            },
            "thread_id": {
                "type": "string",
                "description": "Optional target Telegram topic/thread id for delivery. When chat_id is omitted, defaults to the current Telegram origin thread if present.",
            },
            "inputs": {
                "type": "object",
                "description": "Workflow-level public input object forwarded unchanged to Canon.",
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": 600,
                "description": (
                    "How long to wait for the gateway-owned launch response. When omitted, "
                    "sync-short defaults to 90 seconds and sync-real defaults to 600 seconds."
                ),
            },
            "wait_mode": {
                "type": "string",
                "enum": ["sync-short", "sync-real", "async-no-wait"],
                "description": (
                    "Explicit launch wait contract: short synchronous smoke wait, real synchronous wait, "
                    "or async/no-wait tracking-only submission."
                ),
            },
        },
        "required": ["workflow_name", "inputs"],
        "additionalProperties": False,
    },
}


def _request_root() -> Path:
    """Return the profile-scoped request root for Canon CLI-run envelopes.

    pre: Hermes home is resolvable for the active profile.
    post: returns one path under HERMES_HOME reserved for Canon CLI-run request/response files.
    raises: none.
    """

    return Path(get_hermes_home()) / "gateway-control" / "canon-cli-run"


def _requests_dir() -> Path:
    """Return the pending-request directory for Canon CLI-run envelopes.

    pre: Hermes home is resolvable for the active profile.
    post: returns one child path under the Canon CLI-run request root.
    raises: none.
    """

    return _request_root() / "requests"


def _responses_dir() -> Path:
    """Return the response directory for Canon CLI-run envelopes.

    pre: Hermes home is resolvable for the active profile.
    post: returns one child path under the Canon CLI-run request root.
    raises: none.
    """

    return _request_root() / "responses"


def _request_file(request_id: str) -> Path:
    """Return the on-disk request-file path for one request id.

    pre: request_id is non-empty.
    post: returned path lives under the profile-scoped requests directory.
    raises: none.
    """

    safe_request_id = _validate_request_id(request_id)
    return _requests_dir() / f"{safe_request_id}.request.json"


def _response_file(request_id: str) -> Path:
    """Return the on-disk response-file path for one request id.

    pre: request_id is non-empty.
    post: returned path lives under the profile-scoped responses directory.
    raises: none.
    """

    safe_request_id = _validate_request_id(request_id)
    return _responses_dir() / f"{safe_request_id}.response.json"


def _validate_request_id(request_id: str, *, field_name: str = "requestId") -> str:
    """Return one filesystem-safe Canon gateway request id.

    pre: request_id is any JSON-derived value already converted to text.
    post: returns a non-empty simple file stem containing only ASCII letters, digits, `_`, or `-`.
    raises: ValueError when the id is blank or could alter the response/request path.
    """

    normalized = str(request_id or "").strip()
    if not _SAFE_REQUEST_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid {field_name}: must be a simple file stem")
    return normalized


def _ensure_request_dirs() -> None:
    """Create Canon CLI-run request directories with restrictive permissions.

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


def _normalize_optional_text(value: Any) -> str | None:
    """Return one optional stripped string field.

    pre: value is JSON-like.
    post: returns None when value is absent/blank, otherwise a stripped string.
    raises: none.
    """

    normalized = str(value or "").strip()
    return normalized or None


def _session_origin_target() -> dict[str, str] | None:
    """Return the active Telegram origin target when session context provides one.

    pre: none.
    post: returns None when the current session has no Telegram origin authority.
    post: returned mapping contains chat_id and optional thread_id only from session context.
    raises: none.
    """

    from gateway.session_context import get_session_env

    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip()
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "").strip()
    if platform != "telegram" or not chat_id:
        return None
    thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "").strip() or None
    target = {
        "platform": platform,
        "chat_id": chat_id,
    }
    if thread_id is not None:
        target["thread_id"] = thread_id
    return target


def _normalize_inputs(inputs: Any) -> dict[str, Any]:
    """Validate the public workflow-level inputs object.

    pre: inputs is JSON-like.
    post: returns the original mapping when it is a JSON object.
    raises: ValueError when inputs is missing or not an object.
    """

    if inputs is None:
        raise ValueError("inputs is required")
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    return inputs


def _normalize_wait_mode(wait_mode: Any) -> str:
    """Return one explicit public wait mode for Canon gateway launch requests.

    pre: wait_mode is JSON-like and may be absent.
    post: returns the default short synchronous mode when absent/blank.
    post: returns only one value from the public wait-mode contract.
    raises: ValueError when the caller supplies an unsupported wait mode.
    """

    normalized = str(wait_mode or "").strip() or _DEFAULT_WAIT_MODE
    if normalized not in _ALLOWED_WAIT_MODES:
        allowed = ", ".join(_ALLOWED_WAIT_MODES)
        raise ValueError(f"wait_mode must be one of: {allowed}")
    return normalized


def _normalize_timeout_seconds(timeout_seconds: Any, *, wait_mode: str) -> int:
    """Return the effective wait timeout for one validated launch request.

    pre: wait_mode is one supported public wait mode; timeout_seconds may be absent.
    post: omitted `sync-real` timeout defaults to 600 seconds; all other omitted modes default to 90 seconds.
    post: explicit timeout values are preserved when they are within the public 1..600 range.
    raises: ValueError when the effective timeout is outside the public range.
    """

    if timeout_seconds is None:
        normalized_timeout = (
            _REAL_WAIT_DEFAULT_TIMEOUT_SECONDS if wait_mode == _WAIT_MODE_SYNC_REAL else _DEFAULT_TIMEOUT_SECONDS
        )
    else:
        normalized_timeout = int(timeout_seconds)
    if normalized_timeout < 1 or normalized_timeout > _MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be between 1 and {_MAX_TIMEOUT_SECONDS}")
    return normalized_timeout


def _reject_unexpected_args(args: dict[str, Any]) -> None:
    """Fail closed when callers bypass schema validation with unsupported fields.

    pre: args is the raw tool argument mapping.
    post: returns only when every supplied field is part of the public tool contract.
    raises: ValueError when unsupported fields are present.
    """

    unexpected_fields = sorted(name for name in args if name not in _ALLOWED_ARGUMENTS)
    if unexpected_fields:
        formatted = ", ".join(unexpected_fields)
        raise ValueError(f"unsupported legacy fields: {formatted}")


def build_gateway_local_launch_request(
    *,
    workflow_name: str,
    chat_id: str | None = None,
    thread_id: str | None = None,
    inputs: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
    wait_mode: str = _DEFAULT_WAIT_MODE,
) -> dict[str, Any]:
    """Build one fail-closed Canon gateway-local CLI-run request payload.

    pre: caller supplies explicit workflow plus workflow-level inputs.
    post: returns a JSON-serializable transport payload for the active gateway watcher.
    post: explicit target chat/thread wins; otherwise the active Telegram origin target is used.
    raises: ValueError when workflow/chat/inputs/timeout/wait-mode are invalid.
    """

    normalized_workflow = _normalize_required_text(workflow_name, "workflow_name")
    normalized_chat = _normalize_optional_text(chat_id)
    normalized_thread = _normalize_optional_text(thread_id)
    if normalized_chat is None:
        origin_target = _session_origin_target()
        if origin_target is None:
            raise ValueError("chat_id is required")
        normalized_chat = _normalize_required_text(origin_target.get("chat_id"), "chat_id")
        if normalized_thread is None:
            normalized_thread = _normalize_optional_text(origin_target.get("thread_id"))
    normalized_inputs = _normalize_inputs(inputs)
    normalized_wait_mode = _normalize_wait_mode(wait_mode)
    normalized_timeout = _normalize_timeout_seconds(timeout_seconds, wait_mode=normalized_wait_mode)
    request_id = f"cg-launch-{uuid.uuid4().hex[:12]}"
    target = {
        "platform": "telegram",
        "chatId": normalized_chat,
    }
    if normalized_thread is not None:
        target["threadId"] = normalized_thread
    return {
        "api": _REQUEST_API,
        "action": _REQUEST_ACTION,
        "requestId": request_id,
        "workflow": normalized_workflow,
        "target": target,
        "inputs": normalized_inputs,
        "timeoutSeconds": normalized_timeout,
        "waitMode": normalized_wait_mode,
        "requester": {
            "pid": os.getpid(),
            "cwd": os.getcwd(),
        },
        "requestedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def request_gateway_local_canon_launch(
    *,
    workflow_name: str,
    chat_id: str | None = None,
    thread_id: str | None = None,
    inputs: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
    wait_mode: str = _DEFAULT_WAIT_MODE,
) -> dict[str, Any]:
    """Submit one Canon gateway-local CLI-run request and wait for the gateway response.

    pre: an active Hermes gateway process for this profile is expected to watch the
         Canon CLI-run request directory.
    post: writes one request file under HERMES_HOME and returns either tracking handles
          for async/no-wait mode or the gateway-owned response payload for synchronous modes.
    raises: RuntimeError when the gateway response does not arrive before timeout.
    raises: ValueError when the request payload is invalid.
    """

    request_payload = build_gateway_local_launch_request(
        workflow_name=workflow_name,
        chat_id=chat_id,
        thread_id=thread_id,
        inputs=inputs,
        timeout_seconds=timeout_seconds,
        wait_mode=wait_mode,
    )
    request_id = str(request_payload["requestId"])
    _ensure_request_dirs()
    request_path = _request_file(request_id)
    response_path = _response_file(request_id)
    if request_path.exists() or response_path.exists():
        raise RuntimeError(f"request id collision for {request_id}")
    _atomic_write_json(request_path, request_payload)

    if str(request_payload.get("waitMode") or "") == _WAIT_MODE_ASYNC_NO_WAIT:
        return {
            "accepted": True,
            "requestId": request_id,
            "requestPath": str(request_path),
            "responsePath": str(response_path),
            "waitMode": _WAIT_MODE_ASYNC_NO_WAIT,
            "timeoutSeconds": int(request_payload["timeoutSeconds"]),
        }

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
    """Run one Canon gateway-local CLI-run request as a JSON-returning Hermes tool.

    pre: args matches CANON_GATEWAY_LAUNCH_SCHEMA or is a close variant from a caller.
    post: returns one JSON object describing request submission and gateway-owned response.
    raises: none; failures are encoded in the JSON payload.
    """

    try:
        _reject_unexpected_args(args)
        response = request_gateway_local_canon_launch(
            workflow_name=str(args.get("workflow_name") or ""),
            chat_id=(None if args.get("chat_id") is None else str(args.get("chat_id") or "")),
            thread_id=(None if args.get("thread_id") is None else str(args.get("thread_id") or "")),
            inputs=args.get("inputs"),
            timeout_seconds=(None if args.get("timeout_seconds") is None else int(args.get("timeout_seconds"))),
            wait_mode=str(args.get("wait_mode") or _DEFAULT_WAIT_MODE),
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
