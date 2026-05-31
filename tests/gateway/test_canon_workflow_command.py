"""Gateway Canon command tests for live current-gateway /canon run wiring."""
"""Gateway Canon command tests for live current-gateway /canon run wiring."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import json
import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionEntry, SessionSource, build_session_key
from tools.canon_workflow_command import handle_gateway_canon_command, handle_gateway_canon_command_live


def _make_source(*, thread_id: str | None = None) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="-100123456",
        user_name="operator",
        chat_type="group",
        thread_id=thread_id,
    )


def _make_event(text: str, *, thread_id: str | None = None, message_id: str = "m1") -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(thread_id=thread_id), message_id=message_id)


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    observed: dict[str, object] = {}

    async def _send_canon_review_prompt(chat_id: str, message: str, run_id: str, metadata=None):
        """Record real TelegramAdapter-compatible review prompt arguments.

        pre: caller uses TelegramAdapter.send_canon_review_prompt signature.
        post: returns SendResult with a message id and stores call metadata for assertions.
        raises: TypeError when the gateway passes obsolete text/thread_id keyword arguments.
        """

        observed["review_prompt"] = {
            "chat_id": chat_id,
            "message": message,
            "run_id": run_id,
            "metadata": metadata,
        }
        return SendResult(success=True, message_id="review-msg-1")

    adapter = SimpleNamespace(send=AsyncMock(), send_canon_review_prompt=_send_canon_review_prompt)
    runner._test_observed = observed
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]), loaded_hooks=False)

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner


@pytest.mark.asyncio
async def test_canon_run_requires_task_text_fail_closed():
    result = await handle_gateway_canon_command_live(
        _make_event("/canon run solution-modeling"),
        send_review_prompt=AsyncMock(return_value={"message_id": "m-review"}),
    )
    assert "failed closed" in result.lower()
    assert "requires explicit task text" in result.lower()


@pytest.mark.asyncio
async def test_canon_run_live_path_uses_canon_module_workflow_resolution_without_config_repo_root(monkeypatch):
    import integrations.hermes.canon_hermes.current_gateway_runner as cgr

    observed: dict[str, object] = {}

    class _ConfigWithoutRepoRoot:
        root = "unused"

    def _fake_runtime_pause(*, workflow_path, request, config):
        observed["workflow_path"] = workflow_path
        observed["request"] = request
        observed["config"] = config
        assert not hasattr(config, "repo_root")
        return {"status": "paused", "resumeHandle": {"nodeId": "review", "checkpointId": "ck-1"}}

    declared_callbacks = [
        {"label": "Approve", "callbackData": '{"gateId":"ck-1","action":"approve"}'},
        {"label": "Revise", "callbackData": '{"gateId":"ck-1","action":"revise"}'},
        {"label": "Reject", "callbackData": '{"gateId":"ck-1","action":"reject"}'},
    ]

    def _fake_pause_review(**kwargs):
        observed["pause_kwargs"] = kwargs
        delivery = kwargs["sender"](
            {
                "message": "review payload",
                "callbacks": declared_callbacks,
                "gateIdentity": {
                    "id": "ck-1",
                    "runId": "sm-live-test",
                    "threadId": "telegram:-100123456:777",
                    "workflowRef": "src/canon_workflows/packs/solution_modeling_pack/workflow.json",
                    "nodeId": "review",
                },
                "downloadableArtifacts": [{"path": "/tmp/sm-live-test.json", "fileName": "sm-live-test.json"}],
            }
        )
        observed["delivery"] = delivery
        return {"status": "awaiting-human-review"}

    def _fake_build_runner_config(**kwargs):
        observed["phase_backend_client"] = kwargs.get("phase_backend_client")
        return _ConfigWithoutRepoRoot()

    monkeypatch.setattr(cgr, "build_current_gateway_runner_config", _fake_build_runner_config)
    monkeypatch.setattr(cgr, "_run_current_gateway_runtime_pause", _fake_runtime_pause)
    monkeypatch.setattr(cgr, "pause_current_gateway_for_human_review", _fake_pause_review)

    sender = AsyncMock(return_value={"message_id": "review-msg-42"})
    result = await handle_gateway_canon_command_live(
        _make_event(
            "/canon run solution-modeling Compare blue and green with three bullet criteria.",
            thread_id="777",
            message_id="origin-9",
        ),
        send_review_prompt=sender,
    )

    assert "live run" in result
    assert "awaiting-human-review" in result
    assert observed["workflow_path"].endswith("src/canon_workflows/packs/solution_modeling_pack/workflow.json")
    inputs = observed["request"]["inputs"]
    assert inputs["brief"]["task"] == "Compare blue and green with three bullet criteria."
    assert inputs["review"] == {}
    assert observed["phase_backend_client"] is not None
    assert callable(getattr(observed["phase_backend_client"], "start_scoped_session", None))
    assert observed["pause_kwargs"]["node_id"] == "review"
    assert observed["delivery"] == {
        "messageId": "review-msg-42",
        "message_id": "review-msg-42",
        "chatId": "-100123456",
        "threadId": "777",
    }
    sender.assert_awaited_once_with(
        chat_id="-100123456",
        thread_id="777",
        run_id=ANY,
        text="review payload",
        callbacks=declared_callbacks,
        gate_identity={
            "id": "ck-1",
            "runId": "sm-live-test",
            "threadId": "telegram:-100123456:777",
            "workflowRef": "src/canon_workflows/packs/solution_modeling_pack/workflow.json",
            "nodeId": "review",
        },
        downloadable_artifacts=[{"path": "/tmp/sm-live-test.json", "fileName": "sm-live-test.json"}],
    )


def test_solution_modeling_phase_session_uses_route_model_and_skill_tools(monkeypatch):
    """Live solution-modeling phases must run with skill access and the Canon-selected model route.

    pre: Canon phase backend projection carries modelRoute from workflow routing authority.
    post: gateway phase session calls oneshot agent with openai-codex/gpt-5.5 and skills/file/terminal
          toolsets, so mandatorySkills can actually be loaded by the model.
    raises: AssertionError while phase execution still uses the ambient gateway model with no tools.
    """

    import hermes_cli.oneshot as oneshot
    from tools.canon_workflow_command import _GatewayHermesScopedPhaseSession

    captured = {}

    def fake_run_agent(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return '{"ok": true}'

    monkeypatch.setattr(oneshot, "_run_agent", fake_run_agent)
    session = _GatewayHermesScopedPhaseSession(
        envelope_projection={
            "modelRoute": {"provider": "openai-codex", "model": "gpt-5.5"},
            "hermesProfileId": "default",
        },
        artifacts_dir=None,
    )

    result = session.run_phase(
        {
            "runId": "run-skill-route",
            "phaseId": "model_solution",
            "objective": "Produce model package.",
            "inputs": {"mandatorySkills": ["solution-modeling-packages", "writing-plans"]},
            "outputSchema": {"schema": {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}},
        }
    )

    assert result == {
        "status": "succeeded",
        "output": {"ok": True},
        "agentSessionRef": "hermes-current-gateway:run-skill-route:model_solution:1",
    }
    assert captured["kwargs"]["provider"] == "openai-codex"
    assert captured["kwargs"]["model"] == "gpt-5.5"
    assert captured["kwargs"]["toolsets"] == ["skills", "file", "terminal"]
    assert captured["kwargs"]["use_config_toolsets"] is False
    assert "use skill_view to load every skill named in inputs.mandatorySkills" in captured["prompt"]


@pytest.mark.asyncio
async def test_canon_command_rejects_dry_run_or_direct_helper_proof(monkeypatch):
    import integrations.hermes.canon_hermes.current_gateway as current_gateway
    import integrations.hermes.canon_hermes.current_gateway_runner as cgr

    class _ConfigWithoutRepoRoot:
        root = "unused"

    monkeypatch.setattr(cgr, "build_current_gateway_runner_config", lambda **_kwargs: _ConfigWithoutRepoRoot())
    monkeypatch.setattr(cgr, "_resolve_request_workflow_path", lambda _request: Path("workflow.json"))
    monkeypatch.setattr(
        cgr,
        "_run_current_gateway_runtime_pause",
        lambda *, workflow_path, request, config: {"status": "paused", "resumeHandle": {"nodeId": "review", "checkpointId": "ck-1"}},
    )
    monkeypatch.setattr(
        cgr,
        "pause_current_gateway_for_human_review",
        lambda **kwargs: {"status": "dry-run"},
    )
    monkeypatch.setattr(
        current_gateway,
        "build_current_gateway_request",
        lambda **kwargs: {
            "workflowRef": kwargs["workflow_ref"],
            "runId": kwargs["run_id"],
            "threadId": "telegram:-100123456:777",
            "inputs": kwargs["inputs"],
        },
    )

    result = await handle_gateway_canon_command_live(
        _make_event(
            "/canon run solution-modeling Compare blue and green with three bullet criteria.",
            thread_id="777",
            message_id="origin-9",
        ),
        send_review_prompt=AsyncMock(return_value={"message_id": "review-msg-42"}),
    )

    assert "failed closed" in result
    assert "unexpected live run status: dry-run" in result


@pytest.mark.asyncio
async def test_gateway_canon_run_uses_live_handler(monkeypatch):
    import gateway.run as gateway_run
    import tools.canon_workflow_command as canon_cmd

    observed: dict[str, object] = {}

    async def _fake_live_handler(event, *, send_review_prompt):
        observed["event"] = event
        payload = await send_review_prompt(
            chat_id="-100123456",
            thread_id="777",
            run_id="sm-123",
            text="review text",
            callbacks=[
                {"label": "Approve", "callbackData": '{"gateId":"gate-r05","action":"approve"}'},
                {"label": "Revise", "callbackData": '{"gateId":"gate-r05","action":"revise"}'},
                {"label": "Reject", "callbackData": '{"gateId":"gate-r05","action":"reject"}'},
            ],
            gate_identity={
                "id": "gate-r05",
                "runId": "sm-123",
                "threadId": "telegram:-100123456:777",
                "workflowRef": "workflow.json",
                "nodeId": "review",
            },
        )
        observed["payload"] = payload
        return "live-run-ok"

    runner = _make_runner()
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(canon_cmd, "handle_gateway_canon_command_live", _fake_live_handler)

    result = await runner._handle_message(
        _make_event("/canon run solution-modeling real task for live path", thread_id="777", message_id="origin-9")
    )

    assert result == "live-run-ok"
    assert observed["event"].message_id == "origin-9"
    assert observed["payload"]["message_id"] == "review-msg-1"
    assert observed["payload"]["chatId"] == "-100123456"
    assert observed["payload"]["threadId"] == "777"
    assert runner._test_observed["review_prompt"] == {
        "chat_id": "-100123456",
        "message": "review text",
        "run_id": "sm-123",
        "metadata": {
            "thread_id": "777",
            "callbacks": [
                {"label": "Approve", "callbackData": '{"gateId":"gate-r05","action":"approve"}'},
                {"label": "Revise", "callbackData": '{"gateId":"gate-r05","action":"revise"}'},
                {"label": "Reject", "callbackData": '{"gateId":"gate-r05","action":"reject"}'},
            ],
            "gate_identity": {
                "id": "gate-r05",
                "runId": "sm-123",
                "threadId": "telegram:-100123456:777",
                "workflowRef": "workflow.json",
                "nodeId": "review",
            },
        },
    }


def test_canon_latest_reads_durable_summary_and_marks_blocked_as_non_production(monkeypatch):
    import tools.canon_workflow_command as canon_cmd

    observed: dict[str, object] = {}

    def _fake_load_latest_summary(*, source):
        observed["source"] = source
        return {"runId": "run-42", "status": "blocked-runtime-host-unavailable"}

    monkeypatch.setattr(canon_cmd, "_load_latest_summary", _fake_load_latest_summary)

    result = handle_gateway_canon_command(_make_event("/canon latest"))

    assert "run-42" in result
    assert "blocked-runtime-host-unavailable" in result
    assert "non-production" in result
    assert observed.get("source") is not None


def test_canon_inspect_reads_durable_summary_and_marks_completed_as_production(monkeypatch):
    import tools.canon_workflow_command as canon_cmd

    observed: dict[str, object] = {}

    def _fake_load_inspect_summary(*, run_id: str, source):
        observed["run_id"] = run_id
        observed["source"] = source
        return {"runId": run_id, "status": "completed"}

    monkeypatch.setattr(canon_cmd, "_load_inspect_summary", _fake_load_inspect_summary)

    result = handle_gateway_canon_command(_make_event("/canon inspect run-prod-7"))

    assert "run-prod-7" in result
    assert "completed" in result
    assert "production" in result
    assert observed.get("run_id") == "run-prod-7"
    assert observed.get("source") is not None


@pytest.mark.asyncio
async def test_gateway_local_launch_request_executes_live_handler_and_preserves_thread(monkeypatch, tmp_path: Path):
    import gateway.run as gateway_run

    runner = _make_runner()
    observed: dict[str, object] = {}

    async def _fake_dispatch(event, *, delivery_observer=None):
        observed["event"] = event
        if delivery_observer is not None:
            delivery_observer.update(
                {
                    "chat_id": str(event.source.chat_id),
                    "thread_id": str(event.source.thread_id),
                    "run_id": "sm-777",
                    "message_id": "review-msg-55",
                }
            )
        return "`/canon run` live run `sm-777` status `awaiting-human-review` (chat_id=-100123456, message_thread_id=777)."

    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    runner._dispatch_live_canon_command = _fake_dispatch

    payload = {
        "api": "canon_gateway_local_launch.v1",
        "action": "launch_solution_modeling",
        "requestId": "cg-launch-test-1",
        "taskText": "Build a tiny JSON checklist for comparing blue and green.",
        "target": {"platform": "telegram", "chatId": "-100123456", "threadId": "777"},
    }

    result = await runner._execute_canon_gateway_launch_request(payload)

    assert result["ok"] is True
    assert result["run"] == {
        "runId": "sm-777",
        "status": "awaiting-human-review",
        "artifactRoot": ".agent/live-solution-modeling/sm-777",
    }
    assert result["delivery"] == {
        "messageId": "review-msg-55",
        "chatId": "-100123456",
        "threadId": "777",
    }
    assert result["command"] == "/canon run solution-modeling Build a tiny JSON checklist for comparing blue and green."
    event = observed["event"]
    assert event.source.thread_id == "777"
    assert event.source.user_id == "canon-local-launch:cg-launch-test-1"
    assert event.message_id == "canon-local-launch:cg-launch-test-1"


@pytest.mark.asyncio
async def test_gateway_local_launch_request_rejects_packaged_default_task(monkeypatch):
    import gateway.run as gateway_run

    runner = _make_runner()
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    with pytest.raises(ValueError, match="packaged default"):
        await runner._execute_canon_gateway_launch_request(
            {
                "api": "canon_gateway_local_launch.v1",
                "action": "launch_solution_modeling",
                "requestId": "cg-launch-test-2",
                "taskText": "Model a solution and pause for human review.",
                "target": {"platform": "telegram", "chatId": "-100123456", "threadId": "777"},
            }
        )


@pytest.mark.asyncio
async def test_gateway_local_launch_request_file_writes_response(monkeypatch, tmp_path: Path):
    import gateway.run as gateway_run
    import tools.canon_gateway_launch as launch_tool

    runner = _make_runner()
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(launch_tool, "_response_file", lambda request_id: tmp_path / f"{request_id}.response.json")

    async def _fake_execute(payload):
        return {"ok": True, "requestId": payload["requestId"], "gateway": {"pid": 1, "cwd": "/tmp"}}

    runner._execute_canon_gateway_launch_request = _fake_execute
    request_path = tmp_path / "cg-launch-test-3.processing.json"
    request_path.write_text(
        json.dumps(
            {
                "api": "canon_gateway_local_launch.v1",
                "action": "launch_solution_modeling",
                "requestId": "cg-launch-test-3",
                "taskText": "Compare blue and green in two bullets.",
                "target": {"platform": "telegram", "chatId": "-100123456", "threadId": "777"},
            }
        ),
        encoding="utf-8",
    )

    await runner._process_canon_gateway_launch_request(request_path)

    response_path = tmp_path / "cg-launch-test-3.response.json"
    assert not request_path.exists()
    assert response_path.exists()
    response = json.loads(response_path.read_text(encoding="utf-8"))
    assert response["ok"] is True
    assert response["requestId"] == "cg-launch-test-3"


def test_canon_command_requires_task_topic_and_generic_workflow_facade():
    """AC-R09-001 RED: /canon must route through generic workflow facade, not hardcode solution-modeling.

    The adapter contract (docs/03-hermes-adapter-contract.md line 24) declares that
    the public workflow facade exposes startWorkflow/pauseWorkflow/resumeWorkflow/
    cancelWorkflow/inspectWorkflow/restartWorkflowFromCheckpoint/streamWorkflowEvents,
    and /canon must be a workflow-selection wrapper over that generic facade, not
    hardcode solution-modeling as the sole production path.

    This test falsifies that contract by asserting:
    1. A generic workflow facade module exists at integrations/hermes/canon_hermes/workflow_facade.py.
    2. The /canon run handler routes through the generic facade rather than
       hardcoding solution-modeling as the only allowed workflow.
    3. A non-solution-modeling workflow can be selected through the generic facade path.

    RED reason: No generic workflow facade module exists; _handle_live_run hardcodes
    `workflow != "solution-modeling"` -> ValueError at line 128-129.
    """
    # 1. Generic workflow facade must exist and expose the adapter contract command set.
    from integrations.hermes.canon_hermes.workflow_facade import (
        startWorkflow,
        inspectWorkflow,
        resumeWorkflow,
        cancelWorkflow,
    )

    # 2. The /canon run handler must accept workflows beyond solution-modeling
    #    through the generic facade rather than hardcoding it as the sole path.
    #    Use the sync deterministic handler for a non-solution-modeling workflow.
    event = _make_event("/canon run other-workflow analyze the data")
    result = handle_gateway_canon_command(event)
    # Must NOT reject non-solution-modeling workflows when they go through the
    # generic facade; the facade selects the workflow, not the command parser.
    assert "only" not in result.lower() or "generic" in result.lower(), (
        f"/canon run must accept workflows through generic facade, not hardcode " f"sole-production-path rejection: {result}"
    )

    # 3. The live async handler must also route through the generic facade,
    #    not hardcode solution-modeling as the only production gateway path.
    #    This is the critical gap: _handle_live_run line 128-129 raises ValueError
    #    when workflow != "solution-modeling".
    import tools.canon_workflow_command as canon_cmd

    # Inspect the live handler's code path: if it still hardcodes
    # solution-modeling as the sole production workflow, the RED is confirmed.
    import inspect as _inspect
    live_source = _inspect.getsource(canon_cmd._handle_live_run)
    # The generic facade path must replace the hardcoded workflow check.
    # RED: currently the source contains the hardcode.
    assert 'solution-modeling' not in live_source or 'generic' in live_source.lower(), (
        "_handle_live_run must route through generic workflow facade instead of " "hardcoding solution-modeling as the sole production workflow"
    )


@pytest.mark.asyncio
async def test_quick_exec_cannot_bypass_canon_command_gates():
    """AC-R09-002 RED: quick-exec / alias-expanded commands cannot bypass /canon command gates.

    The gateway alias expansion (run.py ~L6267-6288) rewrites event.text before
    command dispatch. A quick-exec alias targeting /canon must still be subject to
    the /canon command's task/topic/permission/capability gate requirements.

    The critical gap: between alias expansion and /canon dispatch in
    _handle_message, there is no /canon-specific pre-dispatch gate that checks
    the expanded text against /canon's required task, topic, and capability
    contract. The generic _check_slash_access only checks user-level command
    permissions, not /canon-specific gates (task presence, topic identity,
    capability/scope authorization for the target workflow).

    This test falsifies that bypass prevention by:
    1. Simulating a quick-exec alias that constructs a /canon run command
       and verifying that the gateway runner applies /canon-specific gates
       before dispatching to the live handler.
    2. Asserting the existence of a /canon gate function that validates
       task/topic/capability requirements independent of the handler's
       internal checks — so that even if the handler's checks were weakened,
       the gateway-level gate would still enforce them.

    RED reason: No /canon-specific pre-dispatch gate exists in GatewayRunner.
    The runner dispatches directly from alias expansion to _handle_canon_command
    without a gate that validates /canon's task/topic/capability contract.
    """
    import gateway.run as gateway_run

    runner = _make_runner()

    # 1. The gateway runner must have a /canon-specific gate function
    #    that validates task/topic/capability requirements before
    #    dispatching to the live handler. This gate must be callable
    #    independently of _handle_canon_command so that it also applies
    #    to alias-expanded /canon invocations.
    gate_fn = getattr(runner, "_validate_canon_command_gates", None)
    assert callable(gate_fn), (
        "GatewayRunner must expose _validate_canon_command_gates for /canon-specific "
        "pre-dispatch validation of task/topic/capability requirements; "
        "no such gate exists currently"
    )

    # 2. The gate must reject a /canon invocation that lacks task text,
    #    even when the event text was constructed by alias expansion.
    event_no_task = _make_event("/canon run solution-modeling")
    gate_result = gate_fn(event_no_task)
    assert gate_result is not None, (
        "_validate_canon_command_gates must reject /canon run without task text"
    )

    # 3. The gate must reject a /canon invocation that lacks required
    #    topic/thread identity, even when the command text looks valid.
    source_no_thread = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="-100123456",
        user_name="operator",
        chat_type="group",
        thread_id=None,
    )
    event_no_thread = MessageEvent(
        text="/canon run solution-modeling analyze the output",
        source=source_no_thread,
        message_id="m-no-thread",
    )
    gate_result_thread = gate_fn(event_no_thread)
    assert gate_result_thread is not None, (
        "_validate_canon_command_gates must reject /canon run without topic/thread identity"
    )

def _full_solution_model_package(run_id: str) -> dict[str, object]:
    """Return a full solution-modeling modelPackage for artifact writer tests.

    pre: run_id is a non-empty Canon run id.
    post: returned payload includes freeze-ready model fields and handoff refs under current-gateway/run.
    """

    prefix = f"current-gateway/{run_id}"
    return {
        "approvalSummary": {
            "title": "Book notes model",
            "summary": "Build a local-first book notes tool for one user.",
            "recommendedApproach": "Use a local web UI backed by SQLite and Markdown export.",
            "scopeSummary": "In scope: books, notes, tags, search, export. Out of scope: sync, OCR, social features.",
            "keyDecisions": "Local web UI, SQLite source of truth, Markdown export, manual metadata in v1.",
            "weightedAlternatives": "Local web UI scored higher than CLI on review/edit flow despite slower initial build.",
            "keyRisks": "Scope creep and local data loss; mitigate with v1 limits and explicit backup/export.",
            "freezeNotice": "Approve freezes this model package and its decisions for downstream specs.",
        },
        "problemStatement": "Readers need a fast way to capture, find, and export notes while reading books.",
        "goals": ["Capture notes quickly", "Browse and search notes", "Export portable Markdown"],
        "nonGoals": ["Cloud sync", "OCR", "Multi-user sharing"],
        "users": [{"name": "Solo reader", "need": "Capture and revisit book notes"}],
        "useCases": [{"name": "Capture note", "steps": ["Select book", "Write note", "Save with tags"], "successCriteria": ["Note is searchable"]}],
        "domainModel": [{"entity": "Book", "fields": ["title", "author"]}, {"entity": "Note", "fields": ["bookId", "body", "tags"]}],
        "requirements": [{"id": "REQ-1", "text": "User can create and edit notes", "priority": "must"}],
        "acceptanceCriteria": [{"id": "AC-1", "text": "A saved note appears in search results"}],
        "constraints": ["Offline-only", "Single-user", "Local storage"],
        "implicitDecisions": ["SQLite is the write authority", "Markdown is generated export"],
        "decisionMatrix": [{"dimension": "Interface", "options": ["CLI", "Web UI"], "selected": "Web UI", "rationale": "Better for browsing/editing."}],
        "weighedAlternatives": [
            {
                "decision": "Primary interface",
                "criteria": [{"name": "Capture speed", "weight": 0.3}, {"name": "Review/edit usability", "weight": 0.7}],
                "options": [
                    {"option": "CLI", "scores": [{"criterion": "Capture speed", "score": 5, "rationale": "Fast append"}, {"criterion": "Review/edit usability", "score": 2, "rationale": "Poor browsing"}], "totalScore": 2.9, "tradeoffs": "Fast but weak review."},
                    {"option": "Web UI", "scores": [{"criterion": "Capture speed", "score": 4, "rationale": "Good form UX"}, {"criterion": "Review/edit usability", "score": 5, "rationale": "Lists/search/forms"}], "totalScore": 4.7, "tradeoffs": "Slightly more build work."},
                ],
                "selected": "Web UI",
                "rationale": "Higher weighted fit for capture plus review workflow.",
            }
        ],
        "risks": [{"risk": "Scope creep", "impact": "Delayed v1", "mitigation": "Freeze v1 scope"}],
        "openQuestions": [{"question": "Need import later?", "defaultDecision": "Defer", "impact": "May affect future roadmap"}],
        "freeze": {"version": "0.1", "status": "draft-for-approval", "approvalMeaning": "Approve freezes model for spec generation"},
        "specPackageHandoff": {
            "modelPackageRef": f"{prefix}/model-package",
            "architectureRef": f"{prefix}/architecture",
            "specRef": f"{prefix}/spec",
            "workingLogRef": f"{prefix}/working-log",
            "chatSummaryRef": f"{prefix}/chat-summary",
        },
    }


def test_solution_modeling_handoff_writes_distinct_human_readable_artifacts(tmp_path: Path) -> None:
    """Gateway phase backend must persist real spec artifacts, not cloned modelPackage payloads.

    pre: a solution-modeling phase output declares all handoff refs under one current-gateway run.
    post: each declared ref is readable, has a distinct artifact kind, and exposes human-readable markdown.
    raises: AssertionError when architecture/spec/log/summary are aliases of the same JSON body.
    """

    import tools.canon_workflow_command as canon_cmd
    from canon.durability import LocalArtifactBackend

    run_id = "sm-artifacts-red"
    session = canon_cmd._GatewayHermesScopedPhaseSession(
        envelope_projection={"runId": run_id},
        artifacts_dir=tmp_path / "artifacts",
    )
    output = {"modelPackage": _full_solution_model_package(run_id)}

    refs = session._persist_solution_modeling_handoff(
        output,
        envelope={"runId": run_id, "threadId": "telegram:test", "workflowRef": "workflow.json", "phaseId": "model_solution"},
    )

    backend = LocalArtifactBackend(tmp_path / "artifacts")
    assert set(refs) == set(output["modelPackage"]["specPackageHandoff"].values())
    payloads = {ref: backend.read(ref) for ref in refs}
    kinds = {payload.get("artifactKind") for payload in payloads.values()}
    assert {"model-package", "architecture", "spec", "working-log", "chat-summary"}.issubset(kinds)
    markdown_by_ref = {ref: payload.get("contentMarkdown") for ref, payload in payloads.items()}
    assert all(isinstance(markdown, str) and len(markdown) > 80 for markdown in markdown_by_ref.values())
    assert len(set(markdown_by_ref.values())) == len(markdown_by_ref)
    assert "Acceptance Criteria" in payloads[f"current-gateway/{run_id}/spec"]["contentMarkdown"]
    assert "Weighted Alternatives" in payloads[f"current-gateway/{run_id}/model-package"]["contentMarkdown"]
