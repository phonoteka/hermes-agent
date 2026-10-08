#!/usr/bin/env python3
"""Shell-free, manifest-driven Canon execution; no Canon result validation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
from typing import Any

_MANIFEST_SCHEMA = "canon-plugin-background-manifest.v1"
_MANIFEST_KEYS = {"schemaVersion", "operation", "argv", "timeoutSeconds", "cleanupRoot"}
_MAX_STDOUT_BYTES = 1_048_576
_child: subprocess.Popen[bytes] | None = None


def _redact_cli_text(value: str) -> str:
    """Pre: text is CLI output. Post: credential values are hidden; other text unchanged. Raises: none."""
    text = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[REDACTED]", value)
    text = re.sub(r'(?i)("?(?:token|secret|password|api[_-]?key)"?\s*:\s*)"[^"]*"', r'\1"[REDACTED]"', text)
    return re.sub(r"(?i)((?:token|secret|password|api[_-]?key)(?:\s*[=:]\s*|\s+))[^\s,;]+", r"\1[REDACTED]", text)


def _private(path: Path, mode: int, *, directory: bool = False) -> bool:
    """Pre: path is untrusted. Post: true only for canonical private file/directory. Raises: none."""
    try:
        info = path.lstat()
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        return path.is_absolute() and path.resolve(strict=True) == path and kind(info.st_mode) and stat.S_IMODE(info.st_mode) == mode
    except (OSError, RuntimeError):
        return False


def _owned_manifest_root(path: Path) -> Path | None:
    """Pre: manifest path is untrusted. Post: only owned runtime roots can be cleaned. Raises: none."""
    root = path.parent
    if (path.name == "manifest.json" and _private(path, 0o600)
            and root.name.startswith("hermes-canon-") and root.parent.name == "canon"
            and root.parent.parent.name == "runtime" and _private(root, 0o700, directory=True)):
        return root
    return None


def _read_manifest(path: Path) -> dict[str, Any] | None:
    """Pre: private manifest is owned. Post: returns transport metadata, removing manifest. Raises: none."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not _MANIFEST_KEYS <= value.keys() or value.keys() - (_MANIFEST_KEYS | {"resultPath"}):
            return None
        if value["schemaVersion"] != _MANIFEST_SCHEMA or value["cleanupRoot"] != str(path.parent):
            return None
        if not isinstance(value["argv"], list) or not value["argv"] or any(not isinstance(v, str) or not v for v in value["argv"]):
            return None
        timeout = value["timeoutSeconds"]
        if type(timeout) is not int or not 1 <= timeout <= 600:
            return None
        if "resultPath" in value:
            result = Path(value["resultPath"])
            if result.name != "result.json" or result.parent.parent != path.parent.parent or not result.parent.name.startswith("hermes-canon-") or not _private(result.parent, 0o700, directory=True) or result.exists():
                return None
        return value
    except (OSError, ValueError, TypeError):
        return None
    finally:
        path.unlink(missing_ok=True)


def _terminate_child() -> None:
    """Pre: child may be running. Post: terminate, then kill if needed. Raises: none."""
    if _child is None or _child.poll() is not None:
        return
    try:
        os.killpg(_child.pid, signal.SIGTERM)
        _child.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(_child.pid, signal.SIGKILL)
            _child.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _signal_handler(_signum: int, _frame: Any) -> None:
    """Pre: runner receives termination. Post: child group terminated. Raises: none."""
    _terminate_child()


def _run(manifest: dict[str, Any]) -> int:
    """Pre: manifest validated. Post: successful redacted stdout only; errors in optional private capture, exit preserved. Raises: OSError on capture failure."""
    global _child
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, _signal_handler)
    output = ""
    exit_code = 125
    try:
        _child = subprocess.Popen(manifest["argv"], shell=False, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = _child.communicate(timeout=manifest["timeoutSeconds"])
            exit_code = int(_child.returncode)
            if exit_code:
                output = json.dumps({"error": _redact_cli_text(stderr.decode("utf-8", errors="replace"))[:512]})
            elif len(stdout) > _MAX_STDOUT_BYTES:
                exit_code = 125
                output = json.dumps({"error": "Canon stdout exceeds 1 MiB"})
            else:
                output = _redact_cli_text(stdout.decode("utf-8"))
        except subprocess.TimeoutExpired:
            _terminate_child()
            _child.communicate()
            exit_code = 124
            output = json.dumps({"error": "Canon process timed out"})
    except (OSError, ValueError, UnicodeDecodeError):
        output = json.dumps({"error": "Canon process could not start or return UTF-8 output"})
    finally:
        _child = None
    if "resultPath" in manifest:
        fd = os.open(manifest["resultPath"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"output": output, "exit_code": exit_code}, stream)
    # Failure details belong only to the private synchronous capture, never
    # process-completion output (which must not expose CLI stderr).
    if exit_code == 0:
        sys.stdout.write(output)
        sys.stdout.flush()
    return exit_code


def main(argv: list[str]) -> int:
    """Pre: argv names a private manifest. Post: execute and always clean owned root. Raises: none."""
    if len(argv) != 2:
        return 125
    path = Path(argv[1])
    root = _owned_manifest_root(path)
    if root is None:
        return 125
    try:
        manifest = _read_manifest(path)
        return 125 if manifest is None else _run(manifest)
    except OSError:
        return 125
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
