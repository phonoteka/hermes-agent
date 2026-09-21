"""Tests for agent/runtime_cwd.py — the single source of truth for the agent working directory."""

import os
from pathlib import Path

import pytest

import agent.runtime_cwd as rt
from agent.runtime_cwd import (
    clear_session_cwd,
    resolve_agent_cwd,
    resolve_context_cwd,
    set_session_cwd,
)


def _raise_oserror(*args, **kwargs):
    raise OSError("cwd gone")


class TestResolveAgentCwd:
    def test_prefers_terminal_cwd_over_getcwd(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        monkeypatch.chdir(os.path.expanduser("~"))
        assert resolve_agent_cwd() == tmp_path





    def test_propagates_oserror_from_getcwd(self, monkeypatch):
        # The fallback arm calls os.getcwd(), which can raise OSError (deleted cwd).
        # The resolver must NOT swallow it — build_environment_hints owns the
        # try/except OSError guard at the call site (prompt_builder.py:805).
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        monkeypatch.setattr(rt.os, "getcwd", _raise_oserror)
        with pytest.raises(OSError):
            resolve_agent_cwd()


class TestResolveContextCwd:
    def test_returns_dir_when_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        assert resolve_context_cwd() == tmp_path




    def test_expands_leading_tilde(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_CWD", "~")
        assert resolve_context_cwd() == Path(os.path.expanduser("~"))



class TestSessionCwdOverride:
    """The #29531 per-session arm: a contextvar cwd wins over TERMINAL_CWD so a
    multi-session gateway can pin each session to its own folder."""

    def test_session_cwd_overrides_terminal_cwd(self, monkeypatch, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        token = set_session_cwd(str(other))
        try:
            assert resolve_agent_cwd() == other
            assert resolve_context_cwd() == other
        finally:
            rt._SESSION_CWD.reset(token)


    def test_clear_session_cwd_restores_terminal_cwd(self, monkeypatch, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        token = set_session_cwd(str(other))
        try:
            clear_session_cwd()
            assert resolve_agent_cwd() == tmp_path
        finally:
            rt._SESSION_CWD.reset(token)


class TestSessionCwdLifecycle:
    def test_session_cwd_lifecycle_nested_scope_restores_exact_token(self, tmp_path):
        from agent.runtime_cwd import reset_session_cwd, session_cwd_scope

        outer = tmp_path / "outer"
        inner = tmp_path / "inner"
        outer.mkdir()
        inner.mkdir()
        outer_token = set_session_cwd(outer)
        try:
            assert resolve_context_cwd() == outer.resolve()
            with session_cwd_scope(inner) as resolved_inner:
                assert resolved_inner == inner.resolve()
                assert resolve_context_cwd() == inner.resolve()
            assert resolve_context_cwd() == outer.resolve()
            reset_session_cwd(outer_token)
            assert resolve_context_cwd() is None
        finally:
            if rt._SESSION_CWD.get() is not rt._UNSET:
                rt._SESSION_CWD.set(rt._UNSET)

    @pytest.mark.parametrize(
        "invalid_cwd",
        ["", "   ", " /tmp ", "relative/path", "/definitely/missing/session-cwd", 42],
        ids=["blank", "whitespace", "whitespace-padded", "relative", "missing-absolute", "non-path"],
    )
    def test_session_cwd_lifecycle_invalid_input_preserves_outer(self, tmp_path, invalid_cwd):
        from agent.runtime_cwd import session_cwd_scope

        outer = tmp_path / "outer"
        outer.mkdir()
        outer_token = set_session_cwd(outer)
        try:
            with pytest.raises(ValueError):
                with session_cwd_scope(invalid_cwd):
                    pass
            assert resolve_context_cwd() == outer.resolve()
        finally:
            rt._SESSION_CWD.reset(outer_token)

    def test_session_cwd_lifecycle_reset_exact_token(self, tmp_path):
        from agent.runtime_cwd import reset_session_cwd

        token = set_session_cwd(tmp_path)
        assert resolve_context_cwd() == tmp_path.resolve()
        reset_session_cwd(token)
        assert resolve_context_cwd() is None

