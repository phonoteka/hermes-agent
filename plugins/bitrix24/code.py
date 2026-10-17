"""Read-only, tracked-source access for authenticated Bitrix24 task sessions.

Only configured repositories and selected source directories/extensions can be inspected.
No shell, arbitrary filesystem path, untracked file, or write operation is exposed.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from tools.approval_context import get_current_session_key

_SOURCE_DIRS = {
    "backend": frozenset({"application", "admin", "tools", "scripts", "js", "phonoteka"}),
    "editor": frozenset({"src", "tests", "scripts"}),
}
_SOURCE_SUFFIXES = frozenset({".php", ".py", ".js", ".jsx", ".ts", ".tsx", ".vue"})
_TASK_SESSION = re.compile(r"agent:[A-Za-z0-9_.~-]+:bitrix24:group:task:([1-9][0-9]*):\1\Z")
_MAX_FILE_BYTES = 1_048_576
_MAX_LINES = 100
_MAX_MATCHES = 25


def _load_extra() -> dict[str, Any]:
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    platforms = config.get("platforms") if isinstance(config, dict) else None
    bitrix = platforms.get("bitrix24") if isinstance(platforms, dict) else None
    extra = bitrix.get("extra") if isinstance(bitrix, dict) else None
    return extra if isinstance(extra, dict) else {}


def _allowed_root(name: Any) -> Path:
    """Require an explicitly configured Git root; missing authority rejects the call."""
    if not isinstance(name, str) or name not in _SOURCE_DIRS:
        raise ValueError("unknown code root")
    roots = _load_extra().get("code_roots")
    configured = roots.get(name) if isinstance(roots, dict) else None
    if not isinstance(configured, str) or not Path(configured).is_absolute():
        raise ValueError("code root is not configured")
    root = Path(configured).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("code root is not a directory")
    resolved = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    if resolved.returncode or Path(resolved.stdout.strip()).resolve() != root:
        raise ValueError("code root must be a Git repository top level")
    return root


def _source_path(root: Path, name: str, value: Any) -> Path:
    """Require a tracked regular source file, without any symlink or parent traversal."""
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("invalid source path")
    parts = PurePosixPath(value).parts
    if (PurePosixPath(value).is_absolute() or len(parts) < 2 or parts[0] not in _SOURCE_DIRS[name]
            or any(p in {"", ".", ".."} or p.startswith(".") for p in parts)
            or any(p.lower() in {"config", "secrets", "credentials", "private"} for p in parts[:-1])
            or parts[-1].lower() in {"config.php", "database.php", "secrets.py", "credentials.py"}
            or PurePosixPath(value).suffix.lower() not in _SOURCE_SUFFIXES):
        raise ValueError("path is outside the allowed source directories")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlinked source path is not allowed")
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError("source file does not exist inside the code root")
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("source file exceeds the read limit")
    return path


def _tracked_paths(root: Path) -> set[str]:
    found = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached"],
        capture_output=True, timeout=10, check=False,
    )
    if found.returncode:
        raise ValueError("tracked source index is unavailable")
    return {p.decode("utf-8") for p in found.stdout.split(b"\0") if p}


def _task_root(name: Any) -> tuple[str, Path]:
    if not _TASK_SESSION.fullmatch(get_current_session_key(default="")):
        raise ValueError("code access is limited to Bitrix24 task sessions")
    if not isinstance(name, str):
        raise ValueError("unknown code root")
    return name, _allowed_root(name)


def _result(action: str, *, result: dict[str, Any] | None = None, error: str | None = None) -> str:
    return json.dumps(
        {"ok": error is None, "action": action, **({"error": error} if error else {"result": result})},
        ensure_ascii=False,
    )


def _handle_bitrix24_code_read(args: dict[str, Any] | None = None, **_: Any) -> str:
    """Return numbered lines from one tracked file, capped in size and line count."""
    data = args if isinstance(args, dict) else {}
    try:
        name, root = _task_root(data.get("root"))
        path = _source_path(root, name, data.get("path"))
        relative = path.relative_to(root).as_posix()
        if relative not in _tracked_paths(root):
            raise ValueError("source file is not tracked")
        first, count = data.get("start_line", 1), data.get("line_count", _MAX_LINES)
        if type(first) is not int or first < 1 or type(count) is not int or not 1 <= count <= _MAX_LINES:
            raise ValueError("invalid line range")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if any("\x00" in line for line in lines):
            raise ValueError("binary source file is not readable")
        return _result("read", result={
            "root": name, "path": relative, "lines": [
                {"line": n, "text": lines[n - 1][:600]}
                for n in range(first, min(len(lines), first + count - 1) + 1)
            ],
        })
    except (ValueError, OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        return _result("read", error=str(exc))


def _handle_bitrix24_code_search(args: dict[str, Any] | None = None, **_: Any) -> str:
    """Literal case-insensitive search over the configured repository's tracked code."""
    data = args if isinstance(args, dict) else {}
    try:
        name, root = _task_root(data.get("root"))
        query = data.get("query")
        if not isinstance(query, str) or not 2 <= len(query) <= 120 or "\x00" in query or "\n" in query:
            raise ValueError("search query must contain 2-120 characters on one line")
        matches: list[dict[str, Any]] = []
        for relative in sorted(_tracked_paths(root)):
            try:
                path = _source_path(root, name, relative)
            except (ValueError, OSError):
                continue
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line_number, text in enumerate(stream, start=1):
                    if query.casefold() in text.casefold():
                        matches.append({"path": relative, "line": line_number, "text": text.rstrip("\n")[:600]})
                        if len(matches) == _MAX_MATCHES:
                            return _result("search", result={"root": name, "matches": matches})
        return _result("search", result={"root": name, "matches": matches})
    except (ValueError, OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        return _result("search", error=str(exc))
