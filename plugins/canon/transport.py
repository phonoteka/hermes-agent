"""Ordinary Hermes transport for one Canon launch tool.

This module owns only argument/framing/carrier transport. Canon owns workflow
semantics, while Hermes terminal and the native process registry own process
tracking and completion delivery.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import sys
import tempfile
from typing import Any, Literal

from hermes_constants import get_default_hermes_root, get_hermes_home
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from .background_runner import _valid_failed_result, _valid_result
from .schemas import (
    CANON_GATEWAY_CHECKPOINT_SCHEMA,
    CANON_GATEWAY_LAUNCH_SCHEMA,
    CANON_WORKFLOW_CONTROL_SCHEMA,
    CANON_WORKFLOW_OBSERVE_SCHEMA,
)

_WAIT_MODES = frozenset({"sync-short", "sync-real", "async-no-wait"})
_ALLOWED_ARGUMENTS = frozenset({"workflow_name", "workflow_input", "execution_root", "timeout_seconds", "wait_mode"})
_MAX_DIAGNOSTIC_CHARS = 512
_MAX_CLI_JSON_BYTES = 1_048_576


class CanonTransportError(RuntimeError):
    """Bounded, fail-closed transport error."""


class LaunchArguments(BaseModel):
    """Pydantic mirror of the closed public launch schema."""

    model_config = ConfigDict(extra="forbid", strict=True)

    workflow_name: StrictStr = Field(min_length=1)
    workflow_input: dict[str, Any] = Field(min_length=1)
    execution_root: StrictStr = Field(min_length=1)
    timeout_seconds: StrictInt | None = Field(default=None, ge=1, le=600)
    wait_mode: Literal["sync-short", "sync-real", "async-no-wait"] = "sync-short"


@dataclass(frozen=True)
class _PreparedLaunch:
    command_prefix: list[str]
    workflow_path: Path
    execution_root: Path
    request: dict[str, Any]
    wait_mode: str
    timeout_seconds: int


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _decode_json(raw: bytes | str) -> Any | None:
    if not raw or len(raw.encode("utf-8") if isinstance(raw, str) else raw) > _MAX_CLI_JSON_BYTES:
        return None
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        _ensure_finite_json(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, CanonTransportError):
        return None
    return value


def _ensure_finite_json(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise CanonTransportError("launch input contains a non-finite JSON number")
    if isinstance(value, dict):
        for child in value.values():
            _ensure_finite_json(child)
    elif isinstance(value, list):
        for child in value:
            _ensure_finite_json(child)


def _diagnostic(exc: BaseException) -> str:
    text = str(exc) if isinstance(exc, CanonTransportError) else "Canon launch transport failed"
    return text[:_MAX_DIAGNOSTIC_CHARS]


def _canon_command_prefix() -> list[str]:
    """Use the current Hermes interpreter in isolated Canon module mode."""
    return [sys.executable, "-I", "-m", "canon.cli"]


def _validate_args(raw: Any) -> LaunchArguments:
    if not isinstance(raw, dict):
        raise CanonTransportError("tool arguments must be an object")
    unexpected = sorted(set(raw) - _ALLOWED_ARGUMENTS)
    if unexpected:
        raise CanonTransportError(f"unsupported tool arguments: {', '.join(unexpected)}")
    try:
        parsed = LaunchArguments.model_validate(raw)
    except ValidationError as exc:
        raise CanonTransportError("launch arguments do not match the closed schema") from exc
    _ensure_finite_json(parsed.workflow_input)
    return parsed


def _require_workflow_file(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise CanonTransportError("workflow_name must be an absolute existing workflow file")
    return path.resolve(strict=True)


def _require_execution_root(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise CanonTransportError("execution_root must be an absolute existing directory")
    return path.resolve(strict=True)


def _load_packaged_request(workflow_path: Path) -> dict[str, Any]:
    request_path = workflow_path.with_name("run-request.json")
    if not request_path.is_file():
        raise CanonTransportError("selected workflow has no adjacent run-request.json authority")
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CanonTransportError("adjacent run-request authority is not valid JSON framing") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("runConfiguration"), dict):
        raise CanonTransportError("adjacent run-request authority must contain an object runConfiguration")
    _ensure_finite_json(payload)
    return payload


def build_run_request(*, workflow_path: Path, workflow_input: dict[str, Any], execution_root: Path) -> dict[str, Any]:
    """Project transport inputs without minting Canon run identity."""
    if not isinstance(workflow_input, dict) or not workflow_input:
        raise CanonTransportError("workflow_input must be a non-empty object")
    _ensure_finite_json(workflow_input)
    request = deepcopy(_load_packaged_request(workflow_path))
    request["workflowInput"] = deepcopy(workflow_input)
    config = deepcopy(request["runConfiguration"])
    config["executionRoot"] = str(execution_root)
    request["runConfiguration"] = config
    request.pop("runId", None)
    request.pop("threadId", None)
    return request


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o600)
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise OSError("carrier permissions are not private")
    except (OSError, TypeError, ValueError) as exc:
        raise CanonTransportError("could not write private Canon launch authority") from exc


def _ensure_private_directory(path: Path) -> Path:
    try:
        try:
            info = path.lstat()
        except FileNotFoundError:
            path.mkdir(mode=0o700)
            info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise CanonTransportError("private Canon runtime path has an unsafe collision")
        os.chmod(path, 0o700)
        resolved = path.resolve(strict=True)
        if resolved != path or stat.S_IMODE(path.stat().st_mode) != 0o700:
            raise CanonTransportError("private Canon runtime path is not private")
        return resolved
    except CanonTransportError:
        raise
    except (OSError, RuntimeError) as exc:
        raise CanonTransportError("could not create private Canon runtime directory") from exc


def _profile_runtime_parent() -> Path:
    try:
        home = Path(get_hermes_home()).expanduser().resolve(strict=False)
        if not home.is_absolute():
            raise CanonTransportError("Hermes profile home must be absolute")
        home_existed = home.exists()
        home.mkdir(parents=True, exist_ok=True)
        home_info = home.lstat()
        if stat.S_ISLNK(home_info.st_mode) or not stat.S_ISDIR(home_info.st_mode):
            raise CanonTransportError("Hermes profile home has an unsafe collision")
        if not home_existed:
            os.chmod(home, 0o700)
        runtime = home / "runtime"
        runtime.mkdir(mode=0o700, exist_ok=True)
        runtime_info = runtime.lstat()
        if stat.S_ISLNK(runtime_info.st_mode) or not stat.S_ISDIR(runtime_info.st_mode):
            raise CanonTransportError("Hermes runtime path has an unsafe collision")
        return _ensure_private_directory(runtime / "canon")
    except CanonTransportError:
        raise
    except (OSError, RuntimeError) as exc:
        raise CanonTransportError("could not create private Canon runtime parent") from exc


def _profile_durable_root() -> Path:
    return _ensure_private_directory(_profile_runtime_parent() / "durable")


def _create_private_root(execution_root: Path) -> Path:
    resolved_execution = execution_root.resolve(strict=True)
    root: Path | None = None
    try:
        root = Path(tempfile.mkdtemp(prefix="hermes-canon-", dir=str(_profile_runtime_parent()))).resolve(strict=True)
        if root == resolved_execution or root.is_relative_to(resolved_execution):
            raise CanonTransportError("private Canon runtime is not outside execution_root")
        os.chmod(root, 0o700)
        return root
    except CanonTransportError:
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)
        raise
    except (OSError, RuntimeError) as exc:
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)
        raise CanonTransportError("could not create private Canon runtime") from exc


def _build_backend_config(durable_root: Path) -> dict[str, Any]:
    try:
        default_root = Path(get_default_hermes_root()).expanduser()
        if not default_root.is_absolute():
            raise CanonTransportError("canonical Hermes default root must be absolute")
        default_root = default_root.resolve(strict=True)
    except CanonTransportError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise CanonTransportError("could not resolve canonical Hermes default root") from exc
    if not default_root.is_dir():
        raise CanonTransportError("canonical Hermes default root must be an existing directory")
    return {
        "mode": "production",
        "hostAdapter": "canon-hermes-profile",
        "hostConfiguration": {"profilesRoot": str(default_root)},
        "durableRoot": str(durable_root),
    }


def _build_argv(command_prefix: list[str], workflow_path: Path, request_path: Path, config_path: Path) -> list[str]:
    return [
        *command_prefix, "run", "--mode", "production", "--host-config", str(config_path),
        "--request-json", str(request_path), str(workflow_path),
    ]


def _runner_path() -> Path:
    try:
        path = Path(__file__).with_name("background_runner.py").resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CanonTransportError("Canon background runner is unavailable") from exc
    if not path.is_file():
        raise CanonTransportError("Canon background runner is unavailable")
    return path


def _write_manifest(root: Path, argv: list[str], timeout: int, *, operation: str = "launch") -> Path:
    path = root / "manifest.json"
    _write_private_json(path, {
        "schemaVersion": "canon-plugin-background-manifest.v1",
        "operation": operation,
        "argv": argv,
        "timeoutSeconds": timeout,
        "cleanupRoot": str(root),
    })
    return path


def _prepare(args: Any) -> _PreparedLaunch:
    parsed = _validate_args(args)
    workflow_path = _require_workflow_file(parsed.workflow_name)
    execution_root = _require_execution_root(parsed.execution_root)
    timeout = parsed.timeout_seconds if parsed.timeout_seconds is not None else (600 if parsed.wait_mode == "sync-real" else 90)
    request = build_run_request(workflow_path=workflow_path, workflow_input=parsed.workflow_input, execution_root=execution_root)
    return _PreparedLaunch(_canon_command_prefix(), workflow_path, execution_root, request, parsed.wait_mode, timeout)


def _parse_object(raw: Any, label: str) -> dict[str, Any]:
    value = _decode_json(raw) if isinstance(raw, (bytes, str)) else raw
    if not isinstance(value, dict):
        raise CanonTransportError(f"{label} returned malformed or non-object JSON")
    return value


def _native_session(result: dict[str, Any]) -> str | None:
    value = result.get("session_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _kill_native(ctx: Any, native_id: str) -> bool:
    try:
        raw = ctx.dispatch_tool("process", {"action": "kill", "session_id": native_id})
        result = _parse_object(raw, "native process cleanup")
    except Exception:
        return False
    return result.get("status") in {"killed", "already_exited", "not_found"}


def _dispatch_terminal(
    ctx: Any,
    command: str,
    root: Path,
    *,
    notify: bool,
    task_id: str | None,
) -> dict[str, Any]:
    if ctx is None or not callable(getattr(ctx, "dispatch_tool", None)):
        raise CanonTransportError("native terminal transport requires plugin context")
    try:
        raw = ctx.dispatch_tool("terminal", {
            "command": command,
            "background": True,
            "notify_on_complete": notify,
            "workdir": str(root),
            "pty": False,
        }, task_id=task_id)
        return _parse_object(raw, "native terminal")
    except CanonTransportError:
        raise
    except Exception as exc:
        raise CanonTransportError("native terminal dispatch failed") from exc


def _dispatch_wait(ctx: Any, native_id: str, timeout: int) -> dict[str, Any]:
    try:
        raw = ctx.dispatch_tool("process", {"action": "wait", "session_id": native_id, "timeout": timeout})
        result = _parse_object(raw, "native process wait")
        if result.get("status") != "exited":
            return result
        raw_log = ctx.dispatch_tool(
            "process",
            {"action": "log", "session_id": native_id, "offset": 0, "limit": 2},
        )
        log = _parse_object(raw_log, "native process log")
        if log.get("status") != "exited" or log.get("total_lines") != 1:
            raise CanonTransportError("Canon process log framing is invalid")
        result["output"] = log.get("output")
        return result
    except CanonTransportError:
        raise
    except Exception as exc:
        raise CanonTransportError("native process wait failed") from exc


def _prepare_background(prepared: _PreparedLaunch) -> tuple[Path, str]:
    root = _create_private_root(prepared.execution_root)
    try:
        request_path = root / "run-request.json"
        config_path = root / "backend-config.json"
        _write_private_json(request_path, prepared.request)
        _write_private_json(config_path, _build_backend_config(_profile_durable_root()))
        argv = _build_argv(prepared.command_prefix, prepared.workflow_path, request_path, config_path)
        manifest_path = _write_manifest(root, argv, prepared.timeout_seconds)
        command = shlex.join([sys.executable, "-I", str(_runner_path()), str(manifest_path)])
        return root, command
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _parse_runner_result(wait_result: dict[str, Any]) -> dict[str, Any]:
    if wait_result.get("status") != "exited":
        raise CanonTransportError("Canon process did not complete")
    output = wait_result.get("output")
    if wait_result.get("exit_code") != 0:
        result = _valid_failed_result(output) if isinstance(output, (str, bytes)) else None
        if result is None:
            raise CanonTransportError("Canon process failed")
        return result
    result = _valid_result(output) if isinstance(output, (str, bytes)) else None
    if result is None:
        raise CanonTransportError("Canon stdout framing is invalid")
    return result


async def handle_tool(
    args: dict[str, Any],
    *,
    ctx: Any = None,
    task_id: str | None = None,
    **_: Any,
) -> str:
    """Submit the launch through terminal and observe it through process."""
    root: Path | None = None
    native_id: str | None = None
    handed_off = False
    cleanup_confirmed = False
    try:
        prepared = _prepare(args)
        root, command = _prepare_background(prepared)
        terminal_result = _dispatch_terminal(
            ctx,
            command,
            prepared.execution_root,
            notify=prepared.wait_mode == "async-no-wait",
            task_id=task_id,
        )
        native_id = _native_session(terminal_result)
        if native_id is None or terminal_result.get("exit_code") != 0:
            raise CanonTransportError("native terminal did not accept a tracked process")
        if prepared.wait_mode == "async-no-wait":
            if terminal_result.get("notify_on_complete") is not True:
                cleanup_confirmed = _kill_native(ctx, native_id)
                if not cleanup_confirmed:
                    raise CanonTransportError("native process cleanup was not confirmed")
                raise CanonTransportError("native terminal notification is unsupported")
            handed_off = True
            return json.dumps({
                "accepted": True,
                "processSessionId": native_id,
                "waitMode": prepared.wait_mode,
            }, ensure_ascii=False, separators=(",", ":"))
        wait_result = _dispatch_wait(ctx, native_id, prepared.timeout_seconds)
        if wait_result.get("status") in {"timeout", "interrupted"}:
            if not _kill_native(ctx, native_id):
                raise CanonTransportError("native process cleanup was not confirmed")
            cleanup_confirmed = True
            raise CanonTransportError("Canon process wait timed out or was cancelled")
        result = _parse_runner_result(wait_result)
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    except Exception as exc:
        if native_id is not None and not cleanup_confirmed and not _kill_native(ctx, native_id):
            exc = CanonTransportError("native process cleanup was not confirmed")
        return json.dumps({"error": _diagnostic(exc)}, ensure_ascii=False, separators=(",", ":"))
    finally:
        if root is not None and not handed_off:
            shutil.rmtree(root, ignore_errors=True)


_CHECKPOINT_MODES = {
    "history": "history",
    "inspect": "inspect",
    "continue_paused": "continue-paused",
    "replay_from_checkpoint": "replay",
    "replay_from_node": "replay-node",
    "fork_from_checkpoint": "fork",
    "fork_from_node": "fork-node",
}
_CHECKPOINT_ALLOWED = frozenset(CANON_GATEWAY_CHECKPOINT_SCHEMA["parameters"]["properties"])
_CONTROL_ALLOWED = frozenset(CANON_WORKFLOW_CONTROL_SCHEMA["parameters"]["properties"])
_OBSERVE_ALLOWED = frozenset(CANON_WORKFLOW_OBSERVE_SCHEMA["parameters"]["properties"])
def _closed_args(raw: Any, allowed: frozenset[str], required: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CanonTransportError("tool arguments must be an object")
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise CanonTransportError(f"unsupported tool arguments: {', '.join(unexpected)}")
    missing = [name for name in required if name not in raw]
    if missing:
        raise CanonTransportError(f"missing required tool arguments: {', '.join(missing)}")
    return raw


def _text(args: dict[str, Any], name: str, *, required: bool = False) -> str | None:
    value = args.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CanonTransportError(f"{name} must be a non-empty string")
    return value.strip()


def _json_object(args: dict[str, Any], name: str, *, required: bool = False) -> dict[str, Any] | None:
    value = args.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, dict) or (required and not value):
        raise CanonTransportError(f"{name} must be a non-empty object")
    _ensure_finite_json(value)
    return value


def _exact_public_text(args: dict[str, Any], name: str, *, required: bool = False) -> str | None:
    """Read an identity field without trimming or otherwise rewriting it."""
    value = args.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CanonTransportError(f"{name} must be a non-empty string")
    return value


def _checkpoint_safe_mode(args: dict[str, Any]) -> str | None:
    primary = args.get("safe_mode")
    alias = args.get("safeMode")
    if primary is not None and alias is not None:
        if not isinstance(primary, str) or not isinstance(alias, str) or primary != alias:
            raise CanonTransportError("safe_mode conflicts with safeMode")
    value = primary if primary is not None else alias
    if value is None:
        return None
    if value != "allow_replay_side_effects":
        raise CanonTransportError("safe_mode must be allow_replay_side_effects")
    return value


def _validate_continue_request(
    request: Any,
    *,
    mode: str,
    run_id: str,
    thread_id: str,
    workflow_name: str,
) -> dict[str, Any]:
    """Bind the opaque Canon request to public transport identity without rewriting it."""
    if not isinstance(request, dict):
        raise CanonTransportError("continue_paused payload must be a JSON object")
    packaged_request = _load_packaged_request(Path(workflow_name))
    expected_workflow_ref = packaged_request.get("workflowRef")
    if not isinstance(expected_workflow_ref, str) or not expected_workflow_ref.strip():
        raise CanonTransportError("adjacent run-request authority must contain a non-empty workflowRef")
    identity = {
        "schemaVersion": "checkpoint-control-request.schema.v1",
        "action": "checkpointControl",
        "mode": mode,
        "runId": run_id,
        "threadId": thread_id,
        "workflowRef": expected_workflow_ref,
    }
    missing = sorted(set(identity) - set(request))
    if missing:
        raise CanonTransportError("continue_paused payload is missing transport identity: " + ", ".join(missing))
    if any(request[field] != expected for field, expected in identity.items()):
        raise CanonTransportError("continue_paused payload identity does not match public checkpoint arguments")
    _ensure_finite_json(request)
    return deepcopy(request)


def _checkpoint_request_carrier(args: dict[str, Any], *, workflow: str, root: Path) -> dict[str, Any]:
    """Build the private request carrier without becoming a checkpoint semantic owner."""
    mode = _exact_public_text(args, "mode", required=True)
    run_id = _exact_public_text(args, "run_id", required=True)
    thread_id = _exact_public_text(args, "thread_id", required=True)
    assert mode and run_id and thread_id
    if mode == "continue_paused":
        if any(args.get(name) is not None for name in ("checkpoint_id", "node_id", "state_patch", "checkpoint_selector", "lineage_mode", "safe_mode", "safeMode")):
            raise CanonTransportError("continue_paused identity and decision must come only from payload")
        request = _validate_continue_request(
            args.get("payload"), mode=mode, run_id=run_id, thread_id=thread_id, workflow_name=workflow,
        )
    else:
        if args.get("payload") is not None:
            raise CanonTransportError("payload is only valid for continue_paused")
        selector = _json_object(args, "checkpoint_selector")
        if selector is not None:
            unknown = sorted(set(selector) - {"checkpointId", "matchIndex"})
            if unknown:
                raise CanonTransportError("checkpoint_selector has unsupported fields: " + ", ".join(unknown))
            if "checkpointId" in selector and (
                not isinstance(selector["checkpointId"], str) or not selector["checkpointId"].strip()
            ):
                raise CanonTransportError("checkpoint_selector.checkpointId must be a non-empty string")
            match_index = selector.get("matchIndex")
            if match_index is not None and (isinstance(match_index, bool) or not isinstance(match_index, int) or match_index < 0):
                raise CanonTransportError("checkpoint_selector.matchIndex must be a non-negative integer")
        lineage = args.get("lineage_mode")
        if lineage is not None and (not isinstance(lineage, str) or lineage not in {"fork", "same_run_replay"}):
            raise CanonTransportError("lineage_mode must be one of: fork, same_run_replay")
        safe = _checkpoint_safe_mode(args)
        request = {}
        if selector is not None:
            request["checkpointSelector"] = deepcopy(selector)
        if lineage is not None:
            request["lineageMode"] = lineage
        if safe is not None:
            request["safeMode"] = safe
    path = root / "checkpoint-request.json"
    _write_private_json(path, request)
    return request


def _checkpoint_config_carrier(durable_root: Path, root: Path) -> Path:
    path = root / "backend-config.json"
    _write_private_json(path, _build_checkpoint_backend_config(durable_root))
    return path


def _create_checkpoint_operation_root(durable_root: Path) -> Path:
    return _create_private_root(durable_root)


def _cli_backend_args(root: Path) -> list[str]:
    return [
        "--mode", "production",
        "--checkpoint-backend", "sqlite", "--checkpoint-db", str(root / "checkpoint.sqlite"),
        "--artifact-backend", "local", "--artifact-root", str(root / "artifacts"),
        "--journal-backend", "sqlite", "--journal-db", str(root / "journal.sqlite"),
    ]


def _build_checkpoint_backend_config(durable_root: Path) -> dict[str, Any]:
    """Build the checkpoint CLI config from the same host authority as launch."""
    launch_config = _build_backend_config(durable_root)
    return {
        "mode": launch_config["mode"],
        "hostAdapter": launch_config["hostAdapter"],
        "hostConfiguration": deepcopy(launch_config["hostConfiguration"]),
        "checkpoint": {"backend": "sqlite", "db": str(durable_root / "checkpoint.sqlite")},
        "artifact": {"backend": "local", "root": str(durable_root / "artifacts")},
        "journal": {"backend": "sqlite", "db": str(durable_root / "journal.sqlite")},
    }


def _build_restart_argv(command_prefix: list[str], config_path: Path, checkpoint_id: str) -> list[str]:
    return [
        *command_prefix, "restart", "--mode", "production", "--host-config", str(config_path),
        "--checkpoint-id", checkpoint_id,
    ]


def _prepare_restart(checkpoint_id: str, durable_root: Path) -> tuple[Path, str]:
    root = _create_private_root(durable_root)
    try:
        config_path = root / "backend-config.json"
        _write_private_json(config_path, _build_checkpoint_backend_config(durable_root))
        manifest_path = _write_manifest(
            root,
            _build_restart_argv(_canon_command_prefix(), config_path, checkpoint_id),
            90,
            operation="restart",
        )
        command = shlex.join([sys.executable, "-I", str(_runner_path()), str(manifest_path)])
        return root, command
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _redact_cli_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[REDACTED]", text)
    text = re.sub(
        r'(?i)("?(?:token|secret|password|api[_-]?key)"?\s*:\s*)"[^"]*"',
        r'\1"[REDACTED]"',
        text,
    )
    text = re.sub(
        r"(?i)((?:token|secret|password|api[_-]?key)(?:\s*[=:]\s*|\s+))[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )
    return text[:_MAX_DIAGNOSTIC_CHARS]


def _parse_process(raw: Any, label: str) -> dict[str, Any]:
    result = _parse_object(raw, label)
    return result


def _run_cli(ctx: Any, argv: list[str], *, root: Path, timeout: int, task_id: str | None = None) -> dict[str, Any]:
    if ctx is None or not callable(getattr(ctx, "dispatch_tool", None)):
        raise CanonTransportError("Canon CLI transport requires plugin context")
    runner_root: Path | None = None
    try:
        runner_root = _create_private_root(root)
        manifest_path = _write_manifest(runner_root, argv, timeout, operation="command")
        command = shlex.join([sys.executable, "-I", str(_runner_path()), str(manifest_path)])
        started = _parse_process(ctx.dispatch_tool("terminal", {
            "command": command,
            "background": True,
            "notify_on_complete": False,
            "workdir": str(root),
            "pty": False,
        }, task_id=task_id), "Canon CLI terminal")
        native_id = _native_session(started)
        if native_id is None or started.get("exit_code") != 0:
            raise CanonTransportError("Canon CLI process was not accepted by native transport")
        waited = _parse_process(ctx.dispatch_tool("process", {
            "action": "wait", "session_id": native_id, "timeout": timeout,
        }), "Canon CLI process wait")
        if waited.get("status") != "exited":
            _kill_native(ctx, native_id)
            raise CanonTransportError("Canon CLI process did not complete")
        logged = _parse_process(ctx.dispatch_tool("process", {
            "action": "log", "session_id": native_id, "offset": 0, "limit": 4096,
        }), "Canon CLI process log")
        if logged.get("status") != "exited" or (
            logged.get("total_lines") is not None and logged.get("total_lines") != 1
        ):
            raise CanonTransportError("Canon CLI process log framing is invalid")
        output = logged.get("output", waited.get("output", ""))
        if waited.get("exit_code") != 0:
            raise CanonTransportError(_redact_cli_text(output) or "Canon CLI command failed")
        if not isinstance(output, str) or not output.strip():
            raise CanonTransportError("Canon CLI returned empty output")
        value = _decode_json(output)
        if not isinstance(value, (dict, list)):
            raise CanonTransportError("Canon CLI returned malformed, oversized, or non-finite JSON")
        return {"result": value}
    except CanonTransportError:
        raise
    except Exception as exc:
        raise CanonTransportError("Canon CLI process transport failed") from exc
    finally:
        if runner_root is not None:
            shutil.rmtree(runner_root, ignore_errors=True)


def _checkpoint_argv(args: dict[str, Any], root: Path, *, durable_root: Path | None = None) -> list[str]:
    """Build parser-valid checkpoint argv and its operation-private carriers."""
    _closed_args(args, _CHECKPOINT_ALLOWED, ("workflow_name", "mode", "run_id", "thread_id"))
    workflow = _exact_public_text(args, "workflow_name", required=True)
    mode = _exact_public_text(args, "mode", required=True)
    run_id = _exact_public_text(args, "run_id", required=True)
    thread_id = _exact_public_text(args, "thread_id", required=True)
    assert workflow and mode and run_id and thread_id
    if mode not in _CHECKPOINT_MODES:
        raise CanonTransportError("mode is not a supported Canon checkpoint command")
    checkpoint_id = _text(args, "checkpoint_id")
    node_id = _text(args, "node_id")
    selector = _json_object(args, "checkpoint_selector")
    if selector and selector.get("checkpointId") and checkpoint_id and selector["checkpointId"] != checkpoint_id:
        raise CanonTransportError("checkpoint_selector conflicts with checkpoint_id")
    reason = _text(args, "reason")
    safe = _checkpoint_safe_mode(args)
    expected = _text(args, "expected_gateway_git_head") or _text(args, "expectedGatewayGitHead")
    if expected:
        raise CanonTransportError("expected gateway git head has no Canon CLI equivalent")
    wait_mode = args.get("wait_mode")
    if mode == "continue_paused":
        if wait_mode not in (None, "async-no-wait"):
            raise CanonTransportError("continue_paused requires async-no-wait")
    elif wait_mode not in (None, "sync-short", "sync-real"):
        raise CanonTransportError("async-no-wait has no checkpoint CLI equivalent")
    if mode.startswith("replay_") or mode.startswith("fork_"):
        if not reason:
            raise CanonTransportError(f"{mode} requires reason")
        if not safe:
            raise CanonTransportError(f"{mode} requires safe_mode acknowledgement")
    if safe and mode.startswith("replay_") and args.get("lineage_mode") not in (None, "same_run_replay"):
        raise CanonTransportError("lineage_mode conflicts with checkpoint mode")
    if safe and mode.startswith("fork_") and args.get("lineage_mode") not in (None, "fork"):
        raise CanonTransportError("lineage_mode conflicts with checkpoint mode")
    if mode == "inspect" and not checkpoint_id and not node_id:
        raise CanonTransportError("inspect requires checkpoint_id or node_id")
    if mode in {"replay_from_checkpoint", "fork_from_checkpoint"} and not checkpoint_id:
        raise CanonTransportError(f"{mode} requires checkpoint_id")
    if mode in {"replay_from_node", "fork_from_node"} and not node_id:
        raise CanonTransportError(f"{mode} requires node_id")
    state_patch = _json_object(args, "state_patch")
    if state_patch is not None and not mode.startswith("fork_"):
        raise CanonTransportError("state_patch is only expressible by Canon fork commands")
    operation_root = root
    durable = durable_root or root
    _checkpoint_request_carrier(args, workflow=workflow, root=operation_root)
    config_path = _checkpoint_config_carrier(durable, operation_root)
    argv = [
        *_canon_command_prefix(), "checkpoint", _CHECKPOINT_MODES[mode],
        "--host-config", str(config_path), "--request-json", str(operation_root / "checkpoint-request.json"),
        "--run-id", run_id, "--thread-id", thread_id,
    ]
    if mode != "continue_paused":
        if reason:
            argv += ["--reason", reason]
        if checkpoint_id:
            argv += ["--checkpoint-id", checkpoint_id]
        if node_id:
            argv += ["--node-id", node_id]
        if state_patch is not None:
            path = operation_root / "state-patch.json"
            _write_private_json(path, state_patch)
            argv += ["--state-patch-json", str(path)]
    argv += [workflow]
    return argv


async def handle_checkpoint(args: dict[str, Any], *, ctx: Any = None, task_id: str | None = None, **_: Any) -> str:
    root: Path | None = None
    native_id: str | None = None
    handed_off = False
    cleanup_confirmed = False
    try:
        durable_root = _profile_durable_root()
        root = _create_checkpoint_operation_root(durable_root)
        timeout = args.get("timeout_seconds") or 90
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 600:
            raise CanonTransportError("timeout_seconds must be an integer between 1 and 600")
        mode = args.get("mode")
        if mode == "continue_paused" and args.get("wait_mode") in (None, "async-no-wait"):
            manifest_path = _write_manifest(
                root,
                _checkpoint_argv(args, root, durable_root=durable_root),
                timeout,
                operation="command",
            )
            command = shlex.join([sys.executable, "-I", str(_runner_path()), str(manifest_path)])
            terminal_result = _dispatch_terminal(ctx, command, root, notify=True, task_id=task_id)
            native_id = _native_session(terminal_result)
            if native_id is None or terminal_result.get("exit_code") != 0:
                raise CanonTransportError("native terminal did not accept a tracked checkpoint process")
            if terminal_result.get("notify_on_complete") is not True:
                cleanup_confirmed = _kill_native(ctx, native_id)
                if not cleanup_confirmed:
                    raise CanonTransportError("native process cleanup was not confirmed")
                raise CanonTransportError("native terminal notification is unsupported")
            handed_off = True
            return json.dumps({
                "accepted": True,
                "processSessionId": native_id,
                "waitMode": "async-no-wait",
            }, ensure_ascii=False, separators=(",", ":"))
        result = _run_cli(
            ctx,
            _checkpoint_argv(args, root, durable_root=durable_root),
            root=root,
            timeout=timeout,
            task_id=task_id,
        )
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    except Exception as exc:
        if native_id is not None and not cleanup_confirmed and not _kill_native(ctx, native_id):
            exc = CanonTransportError("native process cleanup was not confirmed")
        return json.dumps({"success": False, "error": _diagnostic(exc)}, ensure_ascii=False, separators=(",", ":"))
    finally:
        if root is not None and not handed_off:
            shutil.rmtree(root, ignore_errors=True)


def _control_fields(
    args: dict[str, Any],
) -> tuple[str, str | None, str | None, str | None, str | None]:
    _closed_args(args, _CONTROL_ALLOWED, ("action",))
    action = _exact_public_text(args, "action", required=True)
    run_id = _exact_public_text(args, "run_id")
    thread_id = _exact_public_text(args, "thread_id")
    checkpoint_id = _exact_public_text(args, "checkpoint_id")
    reason = _text(args, "reason")
    assert action
    if action not in {"pause", "cancel", "restart"}:
        raise CanonTransportError("action must be one of: pause, cancel, restart")
    if action == "restart":
        if not checkpoint_id:
            raise CanonTransportError("restart requires checkpoint_id")
        if any(args.get(name) is not None for name in ("run_id", "thread_id", "reason")):
            raise CanonTransportError("restart accepts only action and checkpoint_id")
    else:
        if not run_id or not thread_id:
            raise CanonTransportError(f"{action} requires run_id and thread_id")
        if args.get("checkpoint_id") is not None:
            raise CanonTransportError(f"checkpoint_id is not valid for {action}")
    return action, run_id, thread_id, checkpoint_id, reason


def _control_argv(args: dict[str, Any], root: Path) -> list[str]:
    action, run_id, thread_id, checkpoint_id, reason = _control_fields(args)
    argv = [*_canon_command_prefix(), action, *_cli_backend_args(root)]
    if run_id:
        argv += ["--run-id", run_id]
    if thread_id:
        argv += ["--thread-id", thread_id]
    if checkpoint_id:
        argv += ["--checkpoint-id", checkpoint_id]
    if reason:
        argv += ["--reason", reason]
    return argv


async def handle_control(args: dict[str, Any], *, ctx: Any = None, task_id: str | None = None, **_: Any) -> str:
    root: Path | None = None
    native_id: str | None = None
    handed_off = False
    private_root = False
    cleanup_confirmed = False
    try:
        action, _run_id, _thread_id, checkpoint_id, _reason = _control_fields(args)
        durable_root = _profile_durable_root()
        if action == "restart":
            assert checkpoint_id is not None
            root, command = _prepare_restart(checkpoint_id, durable_root)
            private_root = True
            terminal_result = _dispatch_terminal(
                ctx, command, root, notify=True, task_id=task_id,
            )
            native_id = _native_session(terminal_result)
            if native_id is None or terminal_result.get("exit_code") != 0:
                raise CanonTransportError("native terminal did not accept a tracked restart process")
            if terminal_result.get("notify_on_complete") is not True:
                cleanup_confirmed = _kill_native(ctx, native_id)
                if not cleanup_confirmed:
                    raise CanonTransportError("native process cleanup was not confirmed")
                raise CanonTransportError("native terminal notification is unsupported")
            handed_off = True
            return json.dumps({
                "accepted": True,
                "action": "restart",
                "processSessionId": native_id,
                "checkpointId": checkpoint_id,
            }, ensure_ascii=False, separators=(",", ":"))
        root = durable_root
        result = _run_cli(ctx, _control_argv(args, root), root=root, timeout=90, task_id=task_id)
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    except Exception as exc:
        if native_id is not None and not cleanup_confirmed and not _kill_native(ctx, native_id):
            exc = CanonTransportError("native process cleanup was not confirmed")
        return json.dumps({"success": False, "error": _diagnostic(exc)}, ensure_ascii=False, separators=(",", ":"))
    finally:
        if root is not None and private_root and not handed_off:
            shutil.rmtree(root, ignore_errors=True)


def _observe_argv(args: dict[str, Any], root: Path) -> list[str]:
    _closed_args(args, _OBSERVE_ALLOWED, ("surface",))
    surface = _text(args, "surface", required=True)
    if surface not in {"latest", "list", "full", "inspect", "report", "timeline", "artifacts", "events"}:
        raise CanonTransportError("surface is not a supported Canon observability surface")
    run_id = _exact_public_text(args, "run_id")
    after_sequence = args.get("after_sequence")
    limit = args.get("limit")
    offset = args.get("offset")
    for name, value in (("after_sequence", after_sequence), ("limit", limit), ("offset", offset)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise CanonTransportError(f"{name} must be a non-negative integer")

    if surface in {"latest", "list"}:
        if run_id is not None or after_sequence is not None:
            raise CanonTransportError(f"surface '{surface}' does not accept run_id or after_sequence")
        if surface == "latest" and limit not in {None, 1}:
            raise CanonTransportError("surface 'latest' accepts only limit=1")
        command = "index"
    else:
        if run_id is None:
            raise CanonTransportError(f"run_id is required for surface '{surface}'")
        if surface in {"full", "inspect", "report", "artifacts"}:
            if after_sequence is not None or limit is not None or offset is not None:
                raise CanonTransportError(f"surface '{surface}' does not accept pagination")
        else:
            if offset is not None:
                raise CanonTransportError(f"surface '{surface}' does not accept offset")
        command = "inspect"

    argv = [*_canon_command_prefix(), command]
    argv += [
        "--mode", "production",
        "--artifact-backend", "local",
        "--artifact-root", str(root / "artifacts"),
        "--journal-backend", "sqlite",
        "--journal-db", str(root / "journal.sqlite"),
    ]
    if command == "index":
        if surface == "latest":
            argv += ["--limit", "1"]
        elif limit is not None:
            argv += ["--limit", str(limit)]
        if offset is not None:
            argv += ["--offset", str(offset)]
    else:
        argv += ["--surface", surface, "--run-id", run_id]
        if after_sequence is not None:
            argv += ["--after-sequence", str(after_sequence)]
        if limit is not None:
            argv += ["--limit", str(limit)]
    return argv


async def handle_observe(args: dict[str, Any], *, ctx: Any = None, task_id: str | None = None, **_: Any) -> str:
    try:
        root = _profile_durable_root()
        for name in ("after_sequence", "limit", "offset"):
            value = args.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise CanonTransportError(f"{name} must be a non-negative integer")
        result = _run_cli(ctx, _observe_argv(args, root), root=root, timeout=90, task_id=task_id)
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    except Exception as exc:
        return json.dumps({"success": False, "error": _diagnostic(exc)}, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "CANON_GATEWAY_CHECKPOINT_SCHEMA",
    "CANON_GATEWAY_LAUNCH_SCHEMA",
    "CANON_WORKFLOW_CONTROL_SCHEMA",
    "CANON_WORKFLOW_OBSERVE_SCHEMA",
    "CanonTransportError",
    "LaunchArguments",
    "build_run_request",
    "handle_tool",
    "handle_checkpoint",
    "handle_control",
    "handle_observe",
    "_build_argv",
    "_build_backend_config",
    "_create_private_root",
]
