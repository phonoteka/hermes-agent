"""RED oracle: a profile whose agent.max_turns is the shipped default (null = unlimited) prepares a session."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import profile_runtime as runtime
from hermes_state import SessionDB
from tests.test_profile_agent_session_continuation import _real_agent_factory, _request

_RUNTIME = {
    "provider": "openrouter",
    "api_mode": "openai_chat",
    "base_url": "https://example.invalid/v1",
    "api_key": "test-key",
}


def _prepare_with_profile_config(tmp_path: Path, monkeypatch, config_yaml: str, **request_overrides):
    import hermes_cli.runtime_provider as provider_module

    monkeypatch.setattr(provider_module, "resolve_runtime_provider", lambda **_kwargs: dict(_RUNTIME))
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *_args, **_kwargs: None)
    request = _request(tmp_path, **{"exact_max_turns": None, **request_overrides})
    Path(request.profile_home, "config.yaml").write_text(config_yaml, encoding="utf-8")
    db = SessionDB(db_path=tmp_path / "state.db")
    provider_requests: list[dict] = []
    session = runtime.prepare_profile_agent_session(
        request, agent_factory=_real_agent_factory(db, provider_requests, []),
    )
    return session, db, provider_requests


_BASE_CONFIG = "model:\n  provider: openrouter\n  default: fixture-model\n"


@pytest.mark.parametrize("agent_section", ["", "agent:\n  max_turns: null\n", "agent:\n  max_turns: unlimited\n"])
def test_unlimited_profile_turn_limit_prepares_unlimited_session(tmp_path, monkeypatch, agent_section) -> None:
    session, db, provider_requests = _prepare_with_profile_config(
        tmp_path, monkeypatch, _BASE_CONFIG + agent_section,
    )
    try:
        assert session.snapshot.max_turns == sys.maxsize
        assert provider_requests[0]["max_iterations"] == sys.maxsize
    finally:
        runtime.close_profile_agent_session(session)
        db.close()


def test_finite_profile_turn_limit_and_exact_route_limit_are_kept(tmp_path, monkeypatch) -> None:
    session, db, provider_requests = _prepare_with_profile_config(
        tmp_path, monkeypatch, _BASE_CONFIG + "agent:\n  max_turns: 7\n",
    )
    try:
        assert session.snapshot.max_turns == 7
    finally:
        runtime.close_profile_agent_session(session)
        db.close()
    session, db, provider_requests = _prepare_with_profile_config(
        tmp_path / "exact", monkeypatch, _BASE_CONFIG, exact_max_turns=20,
    )
    try:
        assert session.snapshot.max_turns == 20
        assert provider_requests[0]["max_iterations"] == 20
    finally:
        runtime.close_profile_agent_session(session)
        db.close()


def test_garbage_profile_turn_limit_still_fails_closed(tmp_path, monkeypatch) -> None:
    with pytest.raises(runtime.ProfileAgentSessionError, match="effective max turns must be a positive integer"):
        _prepare_with_profile_config(tmp_path, monkeypatch, _BASE_CONFIG + "agent:\n  max_turns: lots\n")
