"""S2 RED tests for durable profile-agent transcript continuation."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import profile_runtime as runtime
from hermes_state import SessionDB
from run_agent import AIAgent


@contextmanager
def _null_scope(*_args, **_kwargs):
    yield None


def _request(tmp_path: Path, *, session_id: str = "canon-session-1", **overrides):
    profile = tmp_path / "profile"
    work = tmp_path / "work"
    profile.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    (profile / "config.yaml").write_text("fixture", encoding="utf-8")
    (profile / "SOUL.md").write_text("fixture identity", encoding="utf-8")
    values = dict(
        profile_home=profile,
        working_directory=work,
        session_id=session_id,
        task_id="canon-task-1",
        platform="canon",
        request_overrides={},
        enabled_toolsets=[],
        disabled_toolsets=[],
        exact_provider="openrouter",
        exact_model="fixture-model",
        exact_max_turns=3,
    )
    values.update(overrides)
    return runtime.ProfileAgentSessionRequest(**values)


def test_profile_digest_ignores_generated_python_cache_but_tracks_skill_source(tmp_path):
    profile = tmp_path / "profile"
    skill = profile / "skills" / "fixture" / "SKILL.md"
    cache = profile / "skills" / "fixture" / "scripts" / "__pycache__" / "helper.cpython-313.pyc"
    source = profile / "skills" / "fixture" / "scripts" / "helper.py"
    cache.parent.mkdir(parents=True)
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("first", encoding="utf-8")
    cache.write_bytes(b"generated")
    cache.chmod(0)
    config = {"model": {"provider": "openrouter", "default": "fixture-model"}}

    first = runtime._profile_digest(profile, config)
    skill.write_text("second", encoding="utf-8")
    assert runtime._profile_digest(profile, config) != first

    source.write_text("source", encoding="utf-8")
    source.chmod(0)
    with pytest.raises(PermissionError):
        runtime._profile_digest(profile, config)


def test_profile_digest_ignores_skill_runtime_state_outside_skill_packages(tmp_path):
    profile = tmp_path / "profile"
    skills = profile / "skills"
    skill = skills / "category" / "fixture" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("source", encoding="utf-8")
    config = {"model": {"provider": "openrouter", "default": "fixture-model"}}
    first = runtime._profile_digest(profile, config)

    (skills / ".usage.json").write_text('{"fixture": {"use_count": 1}}', encoding="utf-8")
    (skills / ".usage.json.lock").write_text("", encoding="utf-8")
    (skills / ".curator_state").write_text("{}", encoding="utf-8")
    (skills / ".archive" / "old").mkdir(parents=True)
    (skills / ".archive" / "old" / "SKILL.md").write_text("archived", encoding="utf-8")

    assert runtime._profile_digest(profile, config) == first
    (skill.parent / "references").mkdir()
    (skill.parent / "references" / "notes.md").write_text("changed", encoding="utf-8")
    assert runtime._profile_digest(profile, config) != first


def _assert_continuation_request_contract():
    names = {item.name for item in fields(runtime.ProfileAgentSessionRequest)}
    assert {
        "continuation_root_ref",
        "continuation_owner_ref",
        "agent_session_ref",
        "agent_checkpoint_ref",
    } <= names


def _install_managers(monkeypatch, config, runtime_result):
    monkeypatch.setattr(runtime, "profile_runtime_scope", _null_scope)
    import agent.runtime_cwd as runtime_cwd
    import hermes_cli.config as config_module
    import hermes_cli.runtime_provider as provider_module

    monkeypatch.setattr(runtime_cwd, "session_cwd_scope", _null_scope, raising=False)
    monkeypatch.setattr(config_module, "load_config", lambda: config)
    monkeypatch.setattr(provider_module, "resolve_runtime_provider", lambda **_kwargs: runtime_result)
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "agent.skill_commands.build_preloaded_skills_prompt",
        lambda _refs, task_id=None: ("", [], []),
    )


def _real_agent_factory(db, provider_requests, response_texts):
    def factory(**kwargs):
        provider_requests.append(dict(kwargs))
        constructor_kwargs = dict(kwargs)
        constructor_kwargs["session_db"] = kwargs.get("session_db") or db
        with patch("agent.process_bootstrap.OpenAI") as mock_openai, patch(
            "agent.context_compressor.ContextCompressor"
        ) as mock_compressor:
            mock_openai.return_value = MagicMock()
            compressor = MagicMock()
            compressor.context_length = 100000
            compressor.threshold_tokens = 50000
            compressor.protect_first_n = 2
            compressor.protect_last_n = 2
            compressor.should_compress.return_value = False
            compressor.last_prompt_tokens = 0
            mock_compressor.return_value = compressor
            agent = AIAgent(**constructor_kwargs)

        class Transport:
            def build_kwargs(self, *, model, messages, tools=None, max_tokens=None, **_kwargs):
                provider_requests[-1].setdefault("wire_messages", []).append(deepcopy(messages))
                return {"model": model, "messages": messages, "tools": tools}

            def validate_response(self, _response):
                return True

            def normalize_response(self, response, **_kwargs):
                return response.normalized_message

        transport = Transport()

        def response_call(*_args, **_kwargs):
            content = response_texts.pop(0)
            return SimpleNamespace(
                choices=[object()],
                usage=None,
                model=agent.model,
                normalized_message=SimpleNamespace(
                    content=content,
                    tool_calls=None,
                    finish_reason="stop",
                ),
            )

        agent._get_transport = lambda: transport
        agent._interruptible_api_call = response_call
        agent._cleanup_dead_connections = lambda: False
        agent._emit_status = lambda *_args, **_kwargs: None
        agent._touch_activity = lambda *_args, **_kwargs: None
        agent._sync_external_memory_for_turn = lambda **_kwargs: None
        agent._safe_print = lambda *_args, **_kwargs: None
        agent._vprint = lambda *_args, **_kwargs: None
        agent._buffer_status = lambda *_args, **_kwargs: None
        agent._clear_status_buffer = lambda *_args, **_kwargs: None
        agent._try_activate_fallback = lambda *_args, **_kwargs: False
        return agent

    return factory


def _prepare(tmp_path, monkeypatch, *, response_texts):
    db = SessionDB(db_path=tmp_path / "state.db")
    provider_requests = []
    config = {
        "model": {"provider": "openrouter", "default": "fixture-model"},
        "agent": {"max_turns": 3},
    }
    runtime_result = {
        "provider": "openrouter",
        "api_mode": "openai_chat",
        "base_url": "https://example.invalid/v1",
        "api_key": "test-key",
    }
    _install_managers(monkeypatch, config, runtime_result)
    session = runtime.prepare_profile_agent_session(
        _request(tmp_path),
        agent_factory=_real_agent_factory(db, provider_requests, response_texts),
    )
    return session, session.agent._session_db, provider_requests


def test_prepared_profile_session_binds_request_identity_to_real_session_db(tmp_path, monkeypatch):
    session, db, provider_requests = _prepare(tmp_path, monkeypatch, response_texts=["first answer"])
    try:
        assert isinstance(provider_requests[0]["session_db"], SessionDB)
        assert provider_requests[0]["session_db"] is db
        assert Path(db.db_path).resolve() == (Path(session.request.profile_home) / "state.db").resolve()
        assert provider_requests[0]["session_id"] == session.agent.session_id
        assert session.agent.session_id != session.request.session_id
        assert session.agent.session_id.startswith("canon-")
        assert session.agent._session_db is db
    finally:
        runtime.close_profile_agent_session(session)
        db.close()


def test_correction_replays_durable_transcript_after_memory_clear(tmp_path, monkeypatch):
    session, db, provider_requests = _prepare(
        tmp_path,
        monkeypatch,
        response_texts=["first answer", "corrected answer"],
    )
    try:
        runtime.run_profile_agent_session(session, "first prompt")
        first_rows = db.get_messages(session.agent.session_id)
        assert [(row["role"], row["content"]) for row in first_rows] == [
            ("user", "first prompt"),
            ("assistant", "first answer"),
        ]

        session.agent._session_messages = []
        runtime.continue_profile_agent_session(session, "correction prompt")

        second_request = provider_requests[0]["wire_messages"][1]
        replayed_messages = [
            (item["role"], item["content"])
            for item in second_request
            if item["role"] in {"user", "assistant"}
        ]
        assert replayed_messages[-3:] == [
            ("user", "first prompt"),
            ("assistant", "first answer"),
            ("user", "correction prompt"),
        ]
        second_rows = db.get_messages(session.agent.session_id)
        assert len(second_rows) > len(first_rows)
        assert second_rows[-2]["content"] == "correction prompt"
        assert second_rows[-1]["content"] == "corrected answer"
    finally:
        runtime.close_profile_agent_session(session)
        db.close()


def test_continuation_state_reports_fresh_replay_and_advancing_checkpoint(tmp_path, monkeypatch):
    session, db, _provider_requests = _prepare(
        tmp_path,
        monkeypatch,
        response_texts=["first answer", "corrected answer"],
    )
    try:
        state_before = getattr(session, "continuation_state", None)
        assert state_before is not None
        assert state_before.continuation_mode == "fresh"
        assert state_before.history_count == 0
        assert not state_before.agent_checkpoint_ref

        runtime.run_profile_agent_session(session, "first prompt")
        state_after_first = session.continuation_state
        assert state_after_first.continuation_mode == "fresh"
        assert state_after_first.history_count == 0
        assert state_after_first.agent_session_ref
        assert state_after_first.agent_checkpoint_ref

        session.agent._session_messages = []
        runtime.continue_profile_agent_session(session, "correction prompt")
        state_after_second = session.continuation_state
        assert state_after_second.continuation_mode == "replayed"
        assert state_after_second.history_count > 0
        assert state_after_second.agent_session_ref == state_after_first.agent_session_ref
        assert state_after_second.agent_checkpoint_ref != state_after_first.agent_checkpoint_ref
    finally:
        runtime.close_profile_agent_session(session)
        db.close()


def test_durable_history_read_failure_stops_before_second_provider_call(tmp_path, monkeypatch):
    session, db, provider_requests = _prepare(
        tmp_path,
        monkeypatch,
        response_texts=["first answer", "must not be called"],
    )
    try:
        runtime.run_profile_agent_session(session, "first prompt")
        session.agent._session_messages = []

        def fail_history(*_args, **_kwargs):
            raise OSError("durable transcript unavailable")

        monkeypatch.setattr(SessionDB, "get_messages_as_conversation", fail_history)
        with pytest.raises(runtime.ProfileAgentSessionContinuationUnsupported):
            runtime.continue_profile_agent_session(session, "correction prompt")
        assert len(provider_requests[0].get("wire_messages", [])) == 1
    finally:
        runtime.close_profile_agent_session(session)
        db.close()


def test_durable_history_failure_never_falls_back_to_live_agent_memory(tmp_path, monkeypatch):
    session, db, provider_requests = _prepare(
        tmp_path,
        monkeypatch,
        response_texts=["first answer", "must not be called"],
    )
    try:
        runtime.run_profile_agent_session(session, "first prompt")
        assert session.agent._session_messages

        def fail_history(*_args, **_kwargs):
            raise OSError("durable transcript unavailable")

        monkeypatch.setattr(SessionDB, "get_messages_as_conversation", fail_history)
        with pytest.raises(runtime.ProfileAgentSessionContinuationUnsupported):
            runtime.continue_profile_agent_session(session, "correction prompt")
        assert len(provider_requests[0].get("wire_messages", [])) == 1
    finally:
        runtime.close_profile_agent_session(session)
        db.close()


def test_initial_turn_requires_durable_post_turn_attestation(tmp_path, monkeypatch):
    session, db, provider_requests = _prepare(
        tmp_path,
        monkeypatch,
        response_texts=["first answer"],
    )
    try:
        def fail_history(*_args, **_kwargs):
            raise OSError("durable transcript unavailable")

        monkeypatch.setattr(SessionDB, "get_messages_as_conversation", fail_history)
        with pytest.raises(runtime.ProfileAgentSessionContinuationUnsupported):
            runtime.run_profile_agent_session(session, "first prompt")
        assert len(provider_requests[0].get("wire_messages", [])) == 1
        assert not session.continuation_state.agent_checkpoint_ref
    finally:
        runtime.close_profile_agent_session(session)
        db.close()


def _continuation_factory(tmp_path, monkeypatch, responses):
    fallback_db = SessionDB(db_path=tmp_path / "fallback.db")
    provider_requests = []
    config = {
        "model": {"provider": "openrouter", "default": "fixture-model"},
        "agent": {"max_turns": 3},
    }
    runtime_result = {
        "provider": "openrouter",
        "api_mode": "openai_chat",
        "base_url": "https://example.invalid/v1",
        "api_key": "test-key",
    }
    _install_managers(monkeypatch, config, runtime_result)
    return (
        fallback_db,
        provider_requests,
        _real_agent_factory(fallback_db, provider_requests, responses),
    )


def test_implicit_private_identity_isolated_by_canon_session(tmp_path, monkeypatch):
    fallback_db, _provider_requests, factory = _continuation_factory(
        tmp_path, monkeypatch, []
    )
    first = runtime.prepare_profile_agent_session(
        _request(tmp_path, session_id="canon-session-1"),
        agent_factory=factory,
    )
    second = runtime.prepare_profile_agent_session(
        _request(tmp_path, session_id="canon-session-2"),
        agent_factory=factory,
    )
    try:
        assert first.agent.session_id != second.agent.session_id
        assert first.continuation_state.agent_session_ref != second.continuation_state.agent_session_ref
    finally:
        runtime.close_profile_agent_session(second)
        runtime.close_profile_agent_session(first)
        fallback_db.close()


def test_replays_transcript_across_fresh_session_object(tmp_path, monkeypatch):
    _assert_continuation_request_contract()
    fallback_db, provider_requests, factory = _continuation_factory(
        tmp_path, monkeypatch, ["first answer", "corrected answer"]
    )
    first = runtime.prepare_profile_agent_session(
        _request(
            tmp_path,
            continuation_root_ref="continuation://root/fixture",
            continuation_owner_ref="phase://workflow/phase",
        ),
        agent_factory=factory,
    )
    try:
        runtime.run_profile_agent_session(first, "first prompt")
        first_facts = first.continuation_state
    finally:
        runtime.close_profile_agent_session(first)

    second = runtime.prepare_profile_agent_session(
        _request(
            tmp_path,
            continuation_root_ref="continuation://root/fixture",
            continuation_owner_ref="phase://workflow/phase",
            agent_session_ref=first_facts.agent_session_ref,
            agent_checkpoint_ref=first_facts.agent_checkpoint_ref,
        ),
        agent_factory=factory,
    )
    try:
        runtime.continue_profile_agent_session(second, "correction prompt")
        second_facts = second.continuation_state
        messages = [
            (item["role"], item["content"])
            for item in provider_requests[-1]["wire_messages"][-1]
            if item["role"] in {"user", "assistant"}
        ]
        assert messages[-3:] == [
            ("user", "first prompt"),
            ("assistant", "first answer"),
            ("user", "correction prompt"),
        ]
        assert second_facts.agent_session_ref == first_facts.agent_session_ref
        assert second_facts.agent_checkpoint_ref != first_facts.agent_checkpoint_ref
        assert second_facts.continuation_mode == "replayed"
        assert second_facts.history_count >= 2
    finally:
        runtime.close_profile_agent_session(second)
        fallback_db.close()


def test_settings_drift_fails_before_new_agent_or_provider_call(tmp_path, monkeypatch):
    _assert_continuation_request_contract()
    fallback_db, provider_requests, factory = _continuation_factory(
        tmp_path, monkeypatch, ["first answer", "must not be called"]
    )
    first = runtime.prepare_profile_agent_session(
        _request(
            tmp_path,
            continuation_root_ref="continuation://root/fixture",
            continuation_owner_ref="phase://workflow/phase",
        ),
        agent_factory=factory,
    )
    try:
        runtime.run_profile_agent_session(first, "first prompt")
        facts = first.continuation_state
    finally:
        runtime.close_profile_agent_session(first)

    try:
        with pytest.raises(runtime.ProfileAgentSessionContinuationUnsupported):
            runtime.prepare_profile_agent_session(
                _request(
                    tmp_path,
                    continuation_root_ref="continuation://root/fixture",
                    continuation_owner_ref="phase://workflow/phase",
                    agent_session_ref=facts.agent_session_ref,
                    agent_checkpoint_ref=facts.agent_checkpoint_ref,
                    exact_model="different-model",
                ),
                agent_factory=factory,
            )
        assert len(provider_requests) == 1
        assert len(provider_requests[0]["wire_messages"]) == 1
    finally:
        fallback_db.close()


def test_full_ref_continuation_cannot_run_through_fresh_api(tmp_path, monkeypatch):
    _assert_continuation_request_contract()
    fallback_db, provider_requests, factory = _continuation_factory(
        tmp_path, monkeypatch, ["first answer", "must not be called"]
    )
    first = runtime.prepare_profile_agent_session(
        _request(
            tmp_path,
            continuation_root_ref="continuation://root/fixture",
            continuation_owner_ref="phase://workflow/phase",
        ),
        agent_factory=factory,
    )
    try:
        runtime.run_profile_agent_session(first, "first prompt")
        facts = first.continuation_state
    finally:
        runtime.close_profile_agent_session(first)

    second = runtime.prepare_profile_agent_session(
        _request(
            tmp_path,
            continuation_root_ref="continuation://root/fixture",
            continuation_owner_ref="phase://workflow/phase",
            agent_session_ref=facts.agent_session_ref,
            agent_checkpoint_ref=facts.agent_checkpoint_ref,
        ),
        agent_factory=factory,
    )
    db = second.agent._session_db
    rows_before = db.get_messages(second.agent.session_id)
    wire_calls_before = sum(len(item.get("wire_messages", [])) for item in provider_requests)
    error = None
    try:
        try:
            runtime.run_profile_agent_session(second, "correction prompt")
        except runtime.ProfileAgentSessionContinuationUnsupported as exc:
            error = exc
        rows_after = db.get_messages(second.agent.session_id)
        wire_calls_after = sum(len(item.get("wire_messages", [])) for item in provider_requests)
        assert (
            isinstance(error, runtime.ProfileAgentSessionContinuationUnsupported),
            wire_calls_after == wire_calls_before,
            rows_after == rows_before,
        ) == (True, True, True)
    finally:
        runtime.close_profile_agent_session(second)
        fallback_db.close()


def test_checkpoint_is_revalidated_after_prepare_before_provider(tmp_path, monkeypatch):
    _assert_continuation_request_contract()
    fallback_db, provider_requests, factory = _continuation_factory(
        tmp_path, monkeypatch, ["first answer", "must not be called"]
    )
    first = runtime.prepare_profile_agent_session(
        _request(
            tmp_path,
            continuation_root_ref="continuation://root/fixture",
            continuation_owner_ref="phase://workflow/phase",
        ),
        agent_factory=factory,
    )
    try:
        runtime.run_profile_agent_session(first, "first prompt")
        facts = first.continuation_state
    finally:
        runtime.close_profile_agent_session(first)

    second = runtime.prepare_profile_agent_session(
        _request(
            tmp_path,
            continuation_root_ref="continuation://root/fixture",
            continuation_owner_ref="phase://workflow/phase",
            agent_session_ref=facts.agent_session_ref,
            agent_checkpoint_ref=facts.agent_checkpoint_ref,
        ),
        agent_factory=factory,
    )
    db = second.agent._session_db
    db.append_message(
        session_id=second.agent.session_id,
        role="system",
        content="concurrent durable history change",
    )
    wire_calls_before = sum(len(item.get("wire_messages", [])) for item in provider_requests)
    error = None
    try:
        try:
            runtime.continue_profile_agent_session(second, "correction prompt")
        except runtime.ProfileAgentSessionContinuationUnsupported as exc:
            error = exc
        wire_calls_after = sum(len(item.get("wire_messages", [])) for item in provider_requests)
        assert (
            isinstance(error, runtime.ProfileAgentSessionContinuationUnsupported),
            wire_calls_after == wire_calls_before,
        ) == (True, True)
    finally:
        runtime.close_profile_agent_session(second)
        fallback_db.close()


def test_output_schema_is_turn_local(tmp_path, monkeypatch):
    from agent.transports.codex import build_canon_responses_text_format

    _assert_continuation_request_contract()
    fallback_db, provider_requests, factory = _continuation_factory(
        tmp_path, monkeypatch, ["first answer", "corrected answer", "must not be called"]
    )
    schema_a = {"type": "object", "properties": {"phase_a": {"type": "string"}}, "required": ["phase_a"], "additionalProperties": False}
    schema_b = {"type": "object", "properties": {"phase_b": {"type": "integer"}}, "required": ["phase_b"], "additionalProperties": False}
    overrides_a = build_canon_responses_text_format(schema_a)
    overrides_b = build_canon_responses_text_format(schema_b)
    first = runtime.prepare_profile_agent_session(
        _request(
            tmp_path,
            continuation_root_ref="continuation://root/fixture",
            continuation_owner_ref="phase://workflow/phase",
            request_overrides=overrides_a,
        ),
        agent_factory=factory,
    )
    try:
        runtime.run_profile_agent_session(first, "first prompt")
        first_facts = first.continuation_state
        private_session_id = first.agent.session_id
    finally:
        runtime.close_profile_agent_session(first)

    second = runtime.prepare_profile_agent_session(
        _request(
            tmp_path,
            continuation_root_ref="continuation://root/fixture",
            continuation_owner_ref="phase://workflow/phase",
            agent_session_ref=first_facts.agent_session_ref,
            agent_checkpoint_ref=first_facts.agent_checkpoint_ref,
            request_overrides=overrides_b,
        ),
        agent_factory=factory,
    )
    try:
        runtime.continue_profile_agent_session(second, "correction prompt")
        second_facts = second.continuation_state
        replayed = [
            (item["role"], item["content"])
            for item in provider_requests[-1]["wire_messages"][-1]
            if item["role"] in {"user", "assistant"}
        ]
        assert replayed[-3:] == [
            ("user", "first prompt"),
            ("assistant", "first answer"),
            ("user", "correction prompt"),
        ]
        assert second_facts.agent_session_ref == first_facts.agent_session_ref
        assert second_facts.agent_checkpoint_ref != first_facts.agent_checkpoint_ref
        assert provider_requests[-1]["request_overrides"] == overrides_b
        rows_before_other_root = second.agent._session_db.get_messages(second.agent.session_id)
        calls_before_other_root = len(provider_requests)
        with pytest.raises(runtime.ProfileAgentSessionContinuationUnsupported):
            runtime.prepare_profile_agent_session(
                _request(
                    tmp_path,
                    continuation_root_ref="continuation://root/other",
                    continuation_owner_ref="phase://workflow/phase",
                    agent_session_ref=first_facts.agent_session_ref,
                    agent_checkpoint_ref=first_facts.agent_checkpoint_ref,
                    request_overrides=overrides_b,
                ),
                agent_factory=factory,
            )
        assert len(provider_requests) == calls_before_other_root
        assert second.agent._session_db.get_messages(second.agent.session_id) == rows_before_other_root
    finally:
        runtime.close_profile_agent_session(second)
        fallback_db.close()


@pytest.mark.parametrize(
    "case",
    ["timeout", "non_schema_override", "tools", "skills", "mcp", "provider", "model", "profile", "malformed_output_format", "non_canon_output_format", "extra_format_field"],
)
def test_identity_mismatch_and_malformed_output_format_fail_closed(tmp_path, monkeypatch, case):
    from agent.transports.codex import build_canon_responses_text_format
    import agent.skill_commands as skill_commands
    import hermes_cli.runtime_provider as provider_module

    _assert_continuation_request_contract()
    fallback_db, provider_requests, factory = _continuation_factory(
        tmp_path, monkeypatch, ["first answer", "must not be called"]
    )
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}
    canon_format = build_canon_responses_text_format(schema)
    if case == "extra_format_field":
        canon_format["text"]["format"]["extension"] = "not-canon"
    first = runtime.prepare_profile_agent_session(
        _request(
            tmp_path,
            continuation_root_ref="continuation://root/fixture",
            continuation_owner_ref="phase://workflow/phase",
            request_overrides=canon_format,
        ),
        agent_factory=factory,
    )
    try:
        runtime.run_profile_agent_session(first, "first prompt")
        facts = first.continuation_state
        db_path = Path(first.agent._session_db.db_path)
        private_session_id = first.agent.session_id
        rows_before = first.agent._session_db.get_messages(private_session_id)
    finally:
        runtime.close_profile_agent_session(first)

    values: dict[str, Any] = dict(
        continuation_root_ref="continuation://root/fixture",
        continuation_owner_ref="phase://workflow/phase",
        agent_session_ref=facts.agent_session_ref,
        agent_checkpoint_ref=facts.agent_checkpoint_ref,
        request_overrides=canon_format,
    )
    changes = {
        "timeout": {"request_overrides": {**canon_format, "timeout": 17}},
        "non_schema_override": {"request_overrides": {**canon_format, "temperature": 0.25}},
        "tools": {"enabled_toolsets": ["web"]},
        "skills": {"skill_refs": ["fixture"]},
        "mcp": {"mcp_server_refs": ["fixture"]},
        "provider": {"exact_provider": "different-provider"},
        "model": {"exact_model": "different-model"},
        "malformed_output_format": {"request_overrides": {"text": {"format": "not-an-object"}}},
        "non_canon_output_format": {"request_overrides": {"text": {"format": {"type": "json_schema", "name": "other", "strict": True, "schema": schema}}}},
    }
    if case == "profile":
        other_profile = tmp_path / "other-profile"
        other_profile.mkdir()
        (other_profile / "config.yaml").write_text("fixture", encoding="utf-8")
        (other_profile / "SOUL.md").write_text("different identity", encoding="utf-8")
        values["profile_home"] = other_profile
    if case == "extra_format_field":
        values["request_overrides"] = deepcopy(canon_format)
        values["request_overrides"]["text"]["format"]["schema"] = {
            "type": "object", "properties": {"other": {"type": "integer"}}
        }
    if case == "skills":
        monkeypatch.setattr(skill_commands, "build_preloaded_skills_prompt", lambda *_args, **_kwargs: ("fixture skill", ["fixture"], []))
    if case == "provider":
        monkeypatch.setattr(
            provider_module,
            "resolve_runtime_provider",
            lambda *, requested, **_kwargs: {"provider": requested, "api_mode": "openai_chat", "base_url": "https://example.invalid/v1", "api_key": "test-key"},
        )
    values.update(changes.get(case, {}))
    calls_before = len(provider_requests)
    with pytest.raises(runtime.ProfileAgentSessionContinuationUnsupported):
        runtime.prepare_profile_agent_session(_request(tmp_path, **values), agent_factory=factory)
    assert len(provider_requests) == calls_before
    db = SessionDB(db_path=db_path)
    try:
        assert db.get_messages(private_session_id) == rows_before
    finally:
        db.close()
        fallback_db.close()
