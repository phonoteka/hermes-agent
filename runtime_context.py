"""Thread-safe runtime context seams for per-turn working-directory authority.

This module is the explicit Hermes-side seam for runtime-only cwd overrides.
Tools and prompt construction must prefer this seam over the process cwd so a
single conversation turn can bind relative-path behavior without mutating the
whole Python process.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

_RUNTIME_CWD: ContextVar[str | None] = ContextVar("runtime_cwd", default=None)


def _normalize_runtime_cwd(value: str | None) -> str | None:
    """Normalize explicit cwd authority to one absolute path or ``None``.

    pre: value is ``None`` or a string-like path supplied by trusted runtime code.
    post: returns ``None`` for empty values, otherwise an absolute expanded path.
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return os.path.abspath(os.path.expanduser(text))


def get_runtime_cwd(default: str | None = None) -> str | None:
    """Return the active runtime cwd, falling back to TERMINAL_CWD then default.

    The runtime contextvar has highest precedence. If unset, Hermes preserves
    legacy environment-based authority by consulting ``TERMINAL_CWD`` before the
    caller-provided default.
    """

    scoped = _RUNTIME_CWD.get()
    if scoped:
        return scoped

    env_cwd = _normalize_runtime_cwd(os.environ.get("TERMINAL_CWD"))
    if env_cwd:
        return env_cwd

    return default


@contextmanager
def scoped_runtime_cwd(path: str | None) -> Iterator[None]:
    """Bind a runtime cwd override for the current logical execution scope.

    pre: path is ``None``/empty or a trusted path string.
    post: nested scopes restore the previous runtime cwd exactly on exit.
    """

    normalized = _normalize_runtime_cwd(path)
    token: Token[str | None] = _RUNTIME_CWD.set(normalized)
    try:
        yield
    finally:
        _RUNTIME_CWD.reset(token)
