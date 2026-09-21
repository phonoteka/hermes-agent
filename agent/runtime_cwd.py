"""Single source of truth for the agent working directory.

`TERMINAL_CWD` is the runtime carrier for the configured working directory (`terminal.cwd`
is bridged to it once at gateway/cron startup; the local CLI leaves it unset and relies on
the launch dir). Reading it in one place keeps the system prompt, tool surfaces, and
context-file discovery agreeing on where the agent lives. Multi-session gateways can pin a
logical cwd via `_SESSION_CWD`.
"""

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_UNSET: Any = object()

_SESSION_CWD: ContextVar = ContextVar("HERMES_SESSION_CWD", default=_UNSET)

# The package/source root (<root>/agent/runtime_cwd.py). A backend launched from or
# self-spawned into this tree (desktop default) must never let an os.getcwd() fallback
# inject this repo's contributor AGENTS.md as project context.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _is_install_tree(p: Path) -> bool:
    """True only when ``p`` IS the package root or sits inside it — ancestors
    (a home dir containing the checkout) are legitimate workspaces."""
    try:
        p = p.resolve()
    except Exception:
        return False
    return p == _PACKAGE_ROOT or _PACKAGE_ROOT in p.parents


def set_session_cwd(cwd: str | os.PathLike[str] | None) -> Token:
    """Pin the logical cwd for the current context."""
    return _SESSION_CWD.set((os.fspath(cwd) if cwd is not None else "").strip())


def reset_session_cwd(token: Token) -> None:
    """Reset the logical cwd with the exact token returned by ``set_session_cwd``."""
    _SESSION_CWD.reset(token)


def _validate_session_cwd(cwd: str | Path) -> Path:
    if not isinstance(cwd, (str, Path)):
        raise ValueError("session cwd must be a path")
    raw = os.fspath(cwd)
    if not raw or not raw.strip() or raw != raw.strip():
        raise ValueError("session cwd must be nonblank")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("session cwd must be absolute")
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ValueError("session cwd could not be resolved") from exc
    if not resolved.is_dir():
        raise ValueError("session cwd must be an existing directory")
    return resolved


@contextmanager
def session_cwd_scope(cwd: str | Path) -> Iterator[Path]:
    """Bind one validated session cwd and restore its exact context token."""
    resolved = _validate_session_cwd(cwd)
    token = set_session_cwd(str(resolved))
    try:
        yield resolved
    finally:
        reset_session_cwd(token)


def clear_session_cwd() -> None:
    _SESSION_CWD.set("")


def scope_terminal_cwd() -> str:
    """Scope-aware TERMINAL_CWD value (may be empty) — every cwd consumer reads through this.

    Under gateway multiplexing the per-turn terminal scope carries the active profile's cwd;
    the process-global env var may hold another profile's. Only an ImportError falls back: an
    active refusal scope must raise, not silently resolve the launch profile's cwd.
    """
    try:
        from tools.terminal_scope import terminal_env
    except ImportError:
        return os.environ.get("TERMINAL_CWD", "")
    return terminal_env("TERMINAL_CWD", "")


def _existing_dir(raw: str, label: str) -> Path | None:
    p = Path(raw).expanduser()
    if p.is_dir():
        return p
    logger.warning("%s does not exist: %s", label, raw)
    return None


def _resolve_configured_cwd(*, override_is_final: bool) -> Path | None:
    """Session override, then TERMINAL_CWD; each validated as a real directory.

    ``override_is_final``: a set-but-missing session override yields None
    instead of falling through to TERMINAL_CWD.
    """
    override = _SESSION_CWD.get()
    override = "" if override is _UNSET else str(override).strip()
    if override:
        p = _existing_dir(override, "configured working directory")
        if p is not None or override_is_final:
            return p
    raw = scope_terminal_cwd().strip()
    return _existing_dir(raw, "TERMINAL_CWD") if raw else None


def resolve_agent_cwd() -> Path:
    """Configured cwd, else the launch dir (os.getcwd()'s OSError on a deleted cwd deliberately propagates)."""
    return _resolve_configured_cwd(override_is_final=False) or Path(os.getcwd())


def resolve_context_cwd() -> Path | None:
    """Configured cwd for context-file discovery, or None (build_context_files_prompt then falls back to the
    launch dir). An existing configured path is honored verbatim — including the Hermes source tree, a
    legitimate workspace when developing Hermes; fallback-directory policy lives in the caller."""
    return _resolve_configured_cwd(override_is_final=True)
