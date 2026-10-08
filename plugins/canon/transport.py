"""Mechanical Canon argv and private-file transport through native Hermes processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
from typing import Any

from hermes_constants import get_default_hermes_root, get_hermes_home
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .background_runner import _redact_cli_text
from .schemas import TOOL_SCHEMAS

_JSON_PARAMETERS = {"request_json", "state_patch_json", "host_config", "backend"}
_BACKEND_PARAMETERS = {"backend", "checkpoint_backend", "checkpoint_db", "artifact_backend", "artifact_root", "journal_backend", "journal_db"}
_PROCESS_PARAMETERS = {"wait_mode", "timeout_seconds"}


class CanonTransportError(RuntimeError):
    """Transport failure, never a substitute for Canon semantic validation."""


def _private_root() -> Path:
    """Pre: active profile is selected. Post: creates private carrier root under it. Raises: OSError."""
    parent = Path(get_hermes_home()) / "runtime" / "canon"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(tempfile.mkdtemp(prefix="hermes-canon-", dir=parent)).resolve()


def _write_private_json(path: Path, value: dict[str, Any]) -> str:
    """Pre: path is new within owned root. Post: exact insertion-order JSON at 0600. Raises: OSError/TypeError/ValueError."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(json.dumps(value, sort_keys=False).encode("utf-8"))
    return str(path)


def _canon_command_prefix() -> list[str]:
    """Pre: Canon is installed. Post: isolated CLI uses this checkout's profile runtime. Raises: none."""
    root = Path(__file__).resolve().parents[2]
    bootstrap = (
        "import runpy,sys;"
        f"sys.path.insert(0,{str(root)!r});"
        "import profile_runtime;"
        "sys.argv[0]='canon.cli';"
        "runpy.run_module('canon.cli',run_name='__main__')"
    )
    return [sys.executable, "-I", "-c", bootstrap]


def _ensure_private_directory(path: Path) -> None:
    """Pre: path is plugin-owned. Post: directory exists with mode 0700. Raises: OSError."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _default_arguments(command: str, args: dict[str, Any]) -> dict[str, Any]:
    """Pre: command is supported. Post: adds defaults and provisions owned stores only absent explicit authority. Raises: OSError."""
    values = dict(args)
    durable = Path(get_hermes_home()) / "runtime" / "canon" / "durable"
    explicit_backend = bool(_BACKEND_PARAMETERS & values.keys())
    if command in {"run", "checkpoint", "resume", "restart"}:
        if "host_config" not in values and "durable_root" not in values and not explicit_backend:
            config: dict[str, Any] = {
                "mode": "production", "hostAdapter": "canon-hermes-profile",
                "hostConfiguration": {"profilesRoot": str(get_default_hermes_root())},
            }
            if command == "run":
                config["durableRoot"] = str(durable)
            else:
                config.update({
                    "checkpoint": {"backend": "sqlite", "db": str(durable / "checkpoint.sqlite")},
                    "artifact": {"backend": "local", "root": str(durable / "artifacts")},
                    "journal": {"backend": "sqlite", "db": str(durable / "journal.sqlite")},
                })
            _ensure_private_directory(durable)
            _ensure_private_directory(durable / "artifacts")
            values["host_config"] = config
    elif command in {"pause", "cancel", "stream", "inspect", "index"} and "host_config" not in values and "durable_root" not in values and not explicit_backend:
        _ensure_private_directory(durable)
        _ensure_private_directory(durable / "artifacts")
        defaults = {
            "checkpoint_backend": "sqlite", "checkpoint_db": str(durable / "checkpoint.sqlite"),
            "artifact_backend": "local", "artifact_root": str(durable / "artifacts"),
            "journal_backend": "sqlite", "journal_db": str(durable / "journal.sqlite"),
        }
        properties = TOOL_SCHEMAS[command]["parameters"]["properties"]
        values.update({k: v for k, v in defaults.items() if k in properties})
    return values


def _build_argv(command: str, args: dict[str, Any], root: Path) -> list[str]:
    """Pre: transport owns root. Post: flags are forwarded without semantic compatibility checks. Raises: OSError/TypeError/ValueError/KeyError."""
    values = _default_arguments(command, args)
    argv = [*_canon_command_prefix(), command]
    if command == "checkpoint" and "subcommand" in values:
        argv.append(str(values["subcommand"]))
    for key, value in values.items():
        if key in _PROCESS_PARAMETERS or key in {"workflow", "subcommand"}:
            continue
        if key in _JSON_PARAMETERS and isinstance(value, dict):
            value = _write_private_json(root / (key + ".json"), value)
        argv.extend(["--" + key.replace("_", "-"), str(value)])
    if "workflow" in values:
        argv.append(str(values["workflow"]))
    return argv


def build_argv(command: str, args: dict[str, Any]) -> list[str]:
    """Pre: command names a CLI tool. Post: argv plus profile-local 0600 carriers; caller owns their lifetime. Raises: OSError/TypeError/ValueError/KeyError."""
    root = _private_root()
    try:
        return _build_argv(command, args, root)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _process_result(raw: Any) -> dict[str, Any]:
    """Pre: native tool response. Post: decoded process metadata only. Raises: CanonTransportError on malformed transport."""
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise CanonTransportError("Native process transport returned invalid metadata")
    return value


async def handle_command(command: str, args: dict[str, Any], *, ctx: Any, task_id: str | None = None, **_: Any) -> str:
    """Pre: ctx dispatches native tools. Post: CLI stdout unchanged except secrets, or bounded error; async returns native acceptance. Raises: none; owns carrier cleanup."""
    root: Path | None = None
    result_root: Path | None = None
    native_id: str | None = None
    detached = False
    try:
        Draft202012Validator(TOOL_SCHEMAS[command]["parameters"]).validate(args)
        timeout = args.get("timeout_seconds", 600 if args.get("wait_mode") == "sync-real" else 90)
        asynchronous = args.get("wait_mode") == "async-no-wait"
        root = _private_root()
        argv = _build_argv(command, args, root)
        manifest = {"schemaVersion": "canon-plugin-background-manifest.v1", "operation": "command", "argv": argv, "timeoutSeconds": timeout, "cleanupRoot": str(root)}
        # Native log APIs drop trailing newlines and bound output. A private
        # capture carries bytes exactly; logs remain ordinary CLI stdout.
        if not asynchronous:
            result_root = _private_root()
            manifest["resultPath"] = str(result_root / "result.json")
        path = _write_private_json(root / "manifest.json", manifest)
        started = _process_result(ctx.dispatch_tool("terminal", {
            "command": shlex.join([sys.executable, "-I", str(Path(__file__).with_name("background_runner.py")), path]),
            "background": True, "notify_on_complete": asynchronous, "pty": False,
            "workdir": str(Path(__file__).resolve().parents[2]),
        }, task_id=task_id))
        native_id = started.get("session_id")
        if not native_id or started.get("exit_code") != 0:
            raise CanonTransportError("Canon process was not accepted by native transport")
        if asynchronous:
            if started.get("notify_on_complete") is not True:
                raise CanonTransportError("Native transport did not enable completion notification")
            detached = True
            return json.dumps(started)
        waited = _process_result(ctx.dispatch_tool("process_manage", {"action": "wait", "session_id": native_id, "timeout": timeout}))
        if waited.get("status") != "exited":
            raise CanonTransportError("Canon process did not complete within native wait")
        ctx.dispatch_tool("process_manage", {"action": "log", "session_id": native_id, "offset": 0, "limit": 1048576})
        capture = json.loads((result_root / "result.json").read_text(encoding="utf-8"))
        return capture["output"]
    except ValidationError:
        return json.dumps({"error": "Tool arguments do not match the closed CLI schema"})
    except Exception as exc:
        if native_id and not detached:
            try:
                ctx.dispatch_tool("process_manage", {"action": "kill", "session_id": native_id})
            except Exception:
                return json.dumps({"error": "Native process cleanup failed"})
        message = str(exc) if isinstance(exc, CanonTransportError) else "Canon process transport failed"
        return json.dumps({"error": _redact_cli_text(message)[:512]})
    finally:
        if root is not None and not detached:
            shutil.rmtree(root, ignore_errors=True)
        if result_root is not None:
            shutil.rmtree(result_root, ignore_errors=True)


__all__ = ["build_argv", "handle_command"]
