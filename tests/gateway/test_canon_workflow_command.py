"""Gateway Canon command tests for live current-gateway /canon run wiring."""
from datetime import datetime
import importlib
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import json
import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionEntry, SessionSource, build_session_key
from tools.canon_workflow_command import (
    handle_gateway_canon_command,
    handle_gateway_canon_command_live,
    parse_gateway_run_command,
)


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


def test_parse_gateway_run_command_accepts_inputs_json_object():
    parsed = parse_gateway_run_command(
        ["run", "third-workflow", "--inputs-json", '{"request":"opaque","limit":3}']
    )

    assert parsed == {
        "workflow": "third-workflow",
        "inputs": {"request": "opaque", "limit": 3},
    }


def test_parse_gateway_run_command_rejects_removed_request_json_flag():
    with pytest.raises(ValueError, match="--inputs-json"):
        parse_gateway_run_command(["run", "third-workflow", "--request-json", "/tmp/request.json"])


@pytest.mark.parametrize(
    ("raw_inputs", "expected_message"),
    [
        ("{", "valid JSON"),
        ("[]", "object"),
        ('"hello"', "object"),
    ],
)
def test_parse_gateway_run_command_rejects_malformed_or_non_object_inputs(raw_inputs: str, expected_message: str):
    with pytest.raises(ValueError, match=expected_message):
        parse_gateway_run_command(["run", "third-workflow", "--inputs-json", raw_inputs])


def test_canon_run_requires_inputs_json_fail_closed():
    result = handle_gateway_canon_command(_make_event("/canon run third-workflow task text"))

    assert "--inputs-json" in result
    assert "requires" in result.lower()


def test_canon_run_preview_accepts_raw_inputs_json_from_telegram_text():
    result = handle_gateway_canon_command(
        _make_event(
            '/canon run third-workflow --inputs-json {"request":"Compare blue and green with three bullet criteria."}',
            thread_id="777",
        )
    )

    assert "accepted" in result
    assert '"request": "Compare blue and green with three bullet criteria."' in result


@pytest.mark.asyncio
async def test_canon_run_live_path_uses_inputs_object_via_public_workflow_facade(monkeypatch):
    """Live `/canon run` must project an inputs object through the public workflow facade.

    pre: the public parser already validated `--inputs-json` as one JSON object.
    post: live execution generates runId in Hermes, forwards `inputs` without `requestJson`, privately
          requests `hostMode=live`, and routes through the current-gateway public workflow facade instead
          of canon.cli shared CLI.
    raises: AssertionError while `/canon run` still closes through canon.cli.run_cli_workflow_command.
    """

    import canon.cli as canon_cli
    from integrations.hermes.canon_hermes import workflow_facade
    import tools.canon_workflow_command as canon_cmd

    observed: dict[str, object] = {}

    def _reject_shared_cli(*args, **kwargs):
        raise AssertionError("live /canon run must not use canon.cli.run_cli_workflow_command")

    def _fake_start_workflow(payload, stores=None):
        observed["payload"] = payload
        observed["stores"] = stores
        delivery = stores["review_sender"](
            {
                "target": "telegram:-100123456:777",
                "runId": "run-gateway-1",
                "message": "Review this run",
                "callbacks": [{"label": "Approve", "actionId": "approve"}],
                "gateIdentity": {"id": "gate-1"},
                "downloadableArtifacts": [],
            }
        )
        return {
            "status": "awaiting-human-review",
            "runId": "run-gateway-1",
            "checkpoint": {"checkpointId": "current-gateway:run-gateway-1"},
            "delivery": {"kind": "sent", **delivery},
        }

    monkeypatch.setattr(canon_cli, "run_cli_workflow_command", _reject_shared_cli)
    monkeypatch.setattr(workflow_facade, "startWorkflow", _fake_start_workflow)

    sender = AsyncMock(return_value={"message_id": "unused"})
    result = await handle_gateway_canon_command_live(
        _make_event(
            '/canon run autonomous-development-pack --inputs-json {"request":"Compare blue and green with three bullet criteria."}',
            thread_id="777",
            message_id="origin-9",
        ),
        send_review_prompt=sender,
    )

    assert "run-gateway-1" in result
    assert "awaiting-human-review" in result
    payload = observed["payload"]
    assert payload["workflowId"] == "autonomous-development-pack"
    assert isinstance(payload["runId"], str) and payload["runId"]
    assert payload["inputs"] == {"request": "Compare blue and green with three bullet criteria."}
    assert payload["hostMode"] == "live"
    assert "requestJson" not in payload
    assert "requestAuthority" not in payload
    stores = observed["stores"]
    assert stores["root"] == str(canon_cmd._canon_gateway_durable_root())
    assert hasattr(stores["phase_backend_client"], "start_scoped_session")
    assert hasattr(stores["tool_host"], "call_tool")
    sender.assert_awaited_once()
    assert sender.await_args.kwargs == {
        "chat_id": "-100123456",
        "thread_id": "777",
        "run_id": "run-gateway-1",
        "text": "Review this run",
        "callbacks": [{"label": "Approve", "actionId": "approve"}],
        "gate_identity": {"id": "gate-1"},
        "downloadable_artifacts": [],
    }



def test_canon_facade_rejects_request_owned_gateway_source_provenance() -> None:
    """Canon facade must fail closed when requestAuthority tries to own durable origin provenance.

    pre: current-gateway already owns runtimeContext.gateway and runtimeContext.gateway_source.
    post: requestAuthority.runtimeContext.gateway_source is rejected before it can contaminate durable
          origin evidence on the Canon facade seam.
    raises: AssertionError while the facade still merges request-owned gateway_source provenance.
    """

    facade_source = Path(
        "/home/hermes/workspaces/hermes-main/projects/canon/integrations/hermes/canon_hermes/workflow_facade.py"
    ).read_text(encoding="utf-8")

    assert 'if "gateway_source" in runtime_context' in facade_source
    assert "requestAuthority.runtimeContext.gateway_source is forbidden" in facade_source


def test_production_paths_do_not_contain_legacy_canon_run_hardcodes():
    repo_root = Path(__file__).resolve().parents[2]
    legacy_patterns = [
        "launch_solution_modeling",
        "AUTODEV_LOCAL_RUN_REQUEST",
        "AUTONOMOUS_DEVELOPMENT_WORKFLOW_REF",
        'workflow_name == "autonomous-development"',
        'normalized_workflow == "autonomous-development"',
    ]

    scanned_files = list((repo_root / "tools").glob("*.py")) + list((repo_root / "gateway").rglob("*.py"))
    hits: list[str] = []
    for path in scanned_files:
        text = path.read_text(encoding="utf-8")
        for pattern in legacy_patterns:
            if pattern in text:
                hits.append(f"{path.relative_to(repo_root)}::{pattern}")

    assert hits == []


def test_solution_modeling_phase_session_uses_profile_with_route_and_toolset_overrides(monkeypatch, tmp_path: Path):
    """Live Canon phases must run through the selected Hermes profile plus explicit overrides.

    pre: Canon phase backend projection carries hermesProfileId, modelRoute, and allowedScopes.toolsetRefs.
    post: gateway phase session calls oneshot agent with that profile, applies provider/model route overrides,
          applies explicit toolset overrides, and returns session/checkpoint/cursor authority refs.
    raises: AssertionError when phase execution bypasses Hermes profile semantics or hardcodes tools.
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
            "allowedScopes": {
                "toolsetRefs": ["skills", "file", "terminal"],
                "skillRefs": ["solution-modeling-packages"],
            },
            "executionContext": {"workingDirectory": str(tmp_path)},
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
        "agentCheckpointRef": "hermes-current-gateway:checkpoint:run-skill-route:model_solution:1",
        "eventCursorRefs": [
            "hermes-current-gateway:run-skill-route:model_solution:1:events:run-skill-route:model_solution:cursor:1"
        ],
    }
    assert captured["kwargs"]["profile"] == "default"
    assert captured["kwargs"]["provider"] == "openai-codex"
    assert captured["kwargs"]["model"] == "gpt-5.5"
    assert captured["kwargs"]["toolsets"] == ["skills", "file", "terminal"]
    assert captured["kwargs"]["skills"] == ["solution-modeling-packages"]
    assert captured["kwargs"]["use_config_toolsets"] is False
    assert "use skill_view to load every skill named in inputs.mandatorySkills" in captured["prompt"]


def test_phase_session_working_directory_authority_forwards_projection_workdir_to_oneshot(monkeypatch, tmp_path: Path):
    """Repo-mutating Canon phases must execute relative tool calls in the explicit projected workdir.

    pre: Canon phase projection carries executionContext.workingDirectory for a proof-loop worker phase.
    post: the scoped Hermes oneshot receives that absolute workingDirectory authority and the
          prompt restates the same execution directory so relative file/terminal tools bind there.
    raises: AssertionError while the gateway phase backend still lets oneshot default to gateway cwd.
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
            "modelRoute": {"provider": "openai-codex", "model": "gpt-5.4"},
            "allowedScopes": {"toolsetRefs": ["skills", "file", "terminal"]},
            "executionContext": {"workingDirectory": str(tmp_path)},
        },
        artifacts_dir=None,
    )

    result = session.run_phase(
        {
            "runId": "run-target-repo-workdir",
            "phaseId": "proof_loop_build_fix",
            "objective": "Implement the current proof-loop slice.",
            "inputs": {},
            "outputSchema": {"schema": {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}},
        }
    )

    assert result["status"] == "succeeded"
    assert captured["kwargs"]["workdir"] == str(tmp_path)
    assert f"Execution working directory: {tmp_path}" in captured["prompt"]


def test_phase_session_working_directory_authority_rejects_missing_projection_before_agent(monkeypatch, tmp_path: Path):
    """Repo-backed phases must fail closed when explicit workdir authority is missing.

    pre: phase inputs still carry legacy sliceState.runtime.repoRoot but the projection omits executionContext.
    post: run_phase fails closed before starting Hermes oneshot, proving generic inputs no longer count as authority.
    """

    import hermes_cli.oneshot as oneshot
    from tools.canon_workflow_command import _GatewayHermesScopedPhaseSession

    called = False

    def fake_run_agent(prompt, **kwargs):
        nonlocal called
        called = True
        return '{"ok": true}'

    monkeypatch.setattr(oneshot, "_run_agent", fake_run_agent)
    session = _GatewayHermesScopedPhaseSession(
        envelope_projection={
            "modelRoute": {"provider": "openai-codex", "model": "gpt-5.4"},
            "allowedScopes": {"toolsetRefs": ["skills", "file", "terminal"]},
        },
        artifacts_dir=None,
    )

    with pytest.raises(RuntimeError, match="missing required executionContext\\.workingDirectory authority"):
        session.run_phase(
            {
                "runId": "run-missing-projection-workdir",
                "phaseId": "proof_loop_build_fix",
                "objective": "Implement the current proof-loop slice.",
                "inputs": {"sliceState": {"runtime": {"repoRoot": str(tmp_path)}}},
                "outputSchema": {"schema": {"type": "object"}},
            }
        )

    assert called is False


def test_phase_session_working_directory_authority_rejects_relative_or_missing_dir_before_agent(monkeypatch):
    """Explicit projected workdir authority must already be safe before oneshot starts.

    pre: the projection carries a relative or nonexistent executionContext.workingDirectory value.
    post: run_phase fails closed before starting Hermes oneshot.
    """

    import hermes_cli.oneshot as oneshot
    from tools.canon_workflow_command import _GatewayHermesScopedPhaseSession

    called = False

    def fake_run_agent(prompt, **kwargs):
        nonlocal called
        called = True
        return '{"ok": true}'

    monkeypatch.setattr(oneshot, "_run_agent", fake_run_agent)

    for bad_workdir in ("relative/path", "/definitely/missing/hermes-working-directory"):
        session = _GatewayHermesScopedPhaseSession(
            envelope_projection={
                "modelRoute": {"provider": "openai-codex", "model": "gpt-5.4"},
                "allowedScopes": {"toolsetRefs": ["skills", "file", "terminal"]},
                "executionContext": {"workingDirectory": bad_workdir},
            },
            artifacts_dir=None,
        )

        with pytest.raises(
            RuntimeError,
            match="executionContext\\.workingDirectory must be an absolute existing directory",
        ):
            session.run_phase(
                {
                    "runId": f"run-bad-projection-workdir-{bad_workdir!r}",
                    "phaseId": "proof_loop_build_fix",
                    "objective": "Implement the current proof-loop slice.",
                    "inputs": {},
                    "outputSchema": {"schema": {"type": "object"}},
                }
            )

    assert called is False


def test_solution_modeling_phase_session_fails_closed_without_toolset_authority(monkeypatch, tmp_path: Path):
    """Tool-requiring phases must fail closed when projection omits explicit toolset authority.

    pre: phase inputs require mandatory skills but envelope projection has no allowedScopes.toolsetRefs.
    post: run_phase raises RuntimeError before calling oneshot _run_agent.
    raises: AssertionError if hidden default toolsets are used.
    """

    import hermes_cli.oneshot as oneshot
    from tools.canon_workflow_command import _GatewayHermesScopedPhaseSession

    called = {"run_agent": False}

    def fake_run_agent(prompt, **kwargs):
        called["run_agent"] = True
        return '{"ok": true}'

    monkeypatch.setattr(oneshot, "_run_agent", fake_run_agent)
    session = _GatewayHermesScopedPhaseSession(
        envelope_projection={
            "modelRoute": {"provider": "openai-codex", "model": "gpt-5.5"},
            "executionContext": {"workingDirectory": str(tmp_path)},
        },
        artifacts_dir=None,
    )

    with pytest.raises(RuntimeError, match="missing required allowedScopes.toolsetRefs authority"):
        session.run_phase(
            {
                "runId": "run-missing-toolset-authority",
                "phaseId": "model_solution",
                "objective": "Produce model package.",
                "inputs": {"mandatorySkills": ["solution-modeling-packages"]},
                "outputSchema": {"schema": {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}},
            }
        )

    assert called["run_agent"] is False


def test_solution_modeling_pack_runtime_facade_stores_do_not_inject_schema_authority_by_selector(
    monkeypatch, tmp_path: Path
):
    """Launcher-owned runtime stores must stay workflow-neutral.

    pre: workflow selector and requestAuthority.workflowRef both look like solution-modeling aliases.
    post: _build_runtime_facade_stores keeps only launcher-owned private runtime authorities and does
          not inject phase_profile_map or external_schema_resources from workflow identity strings.
    raises: AssertionError while the launcher still derives schema authority from selector/ref text.
    """

    import tools.canon_workflow_command as canon_cmd

    stores = canon_cmd._build_runtime_facade_stores(
        workflow="solution-modeling-pack",
        gateway_root=tmp_path,
        request_authority={
            "workflowRef": "src/canon_workflows/packs/solution_modeling_pack/workflow.json",
        },
    )

    assert set(stores) == {"root", "phase_backend_client", "tool_host"}


def test_phase_without_tool_requirement_runs_with_no_explicit_toolsets(monkeypatch, tmp_path: Path):
    """Phases with no explicit tool requirement may execute with no authorized toolsets.

    pre: projection omits allowedScopes.toolsetRefs and inputs do not declare mandatory skills.
    post: _run_agent is invoked with toolsets=None and use_config_toolsets=False.
    raises: AssertionError when hidden defaults are injected.
    """

    import hermes_cli.oneshot as oneshot
    from tools.canon_workflow_command import _GatewayHermesScopedPhaseSession

    captured = {}

    def fake_run_agent(prompt, **kwargs):
        captured["kwargs"] = kwargs
        return '{"ok": true}'

    monkeypatch.setattr(oneshot, "_run_agent", fake_run_agent)
    session = _GatewayHermesScopedPhaseSession(
        envelope_projection={
            "modelRoute": {"provider": "openai-codex", "model": "gpt-5.5"},
            "executionContext": {"workingDirectory": str(tmp_path)},
        },
        artifacts_dir=None,
    )

    result = session.run_phase(
        {
            "runId": "run-no-tool-requirement",
            "phaseId": "finalize_solution",
            "objective": "Finalize freeze metadata.",
            "inputs": {},
            "outputSchema": {"schema": {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}},
        }
    )

    assert result["status"] == "succeeded"
    assert captured["kwargs"]["toolsets"] is None
    assert captured["kwargs"]["use_config_toolsets"] is False


def test_phase_model_route_requires_provider_model_pair(monkeypatch, tmp_path: Path):
    """A partial Canon modelRoute must not be merged silently with Hermes profile defaults.

    pre: projection carries provider without model under modelRoute.
    post: phase execution fails closed before starting the Hermes agent.
    """

    import hermes_cli.oneshot as oneshot
    from tools.canon_workflow_command import _GatewayHermesScopedPhaseSession

    called = False

    def fake_run_agent(prompt, **kwargs):
        nonlocal called
        called = True
        return '{"ok": true}'

    monkeypatch.setattr(oneshot, "_run_agent", fake_run_agent)
    session = _GatewayHermesScopedPhaseSession(
        envelope_projection={
            "modelRoute": {"provider": "openai-codex"},
            "executionContext": {"workingDirectory": str(tmp_path)},
            "hermesProfileId": "proofloopworker",
        },
        artifacts_dir=None,
    )

    with pytest.raises(RuntimeError, match="modelRoute.*provider.*model"):
        session.run_phase(
            {
                "runId": "run-partial-route",
                "phaseId": "proof_loop_build_fix",
                "objective": "Execute build fix.",
                "inputs": {},
                "outputSchema": {"schema": {"type": "object"}},
            }
        )

    assert called is False


def test_phase_with_profile_and_no_toolset_override_uses_profile_toolsets(monkeypatch, tmp_path: Path):
    """A phase-scoped Hermes profile supplies default tools unless Canon sends explicit overrides.

    pre: projection contains hermesProfileId but omits allowedScopes.toolsetRefs.
    post: _run_agent receives profile=<id>, toolsets=None, and use_config_toolsets=True so Hermes loads
          that profile's configured provider/model/tools/skills, subject to later explicit overrides.
    """

    import hermes_cli.oneshot as oneshot
    from tools.canon_workflow_command import _GatewayHermesScopedPhaseSession

    captured = {}

    def fake_run_agent(prompt, **kwargs):
        captured["kwargs"] = kwargs
        return '{"ok": true}'

    monkeypatch.setattr(oneshot, "_run_agent", fake_run_agent)
    session = _GatewayHermesScopedPhaseSession(
        envelope_projection={
            "executionContext": {"workingDirectory": str(tmp_path)},
            "hermesProfileId": "proofloopworker",
        },
        artifacts_dir=None,
    )

    result = session.run_phase(
        {
            "runId": "run-profile-defaults",
            "phaseId": "proof_loop_build_fix",
            "objective": "Execute build fix.",
            "inputs": {},
            "outputSchema": {"schema": {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}},
        }
    )

    assert result["status"] == "succeeded"
    assert captured["kwargs"]["profile"] == "proofloopworker"
    assert captured["kwargs"]["model"] is None
    assert captured["kwargs"]["provider"] is None
    assert captured["kwargs"]["toolsets"] is None
    assert captured["kwargs"]["use_config_toolsets"] is True


def test_phase_with_profile_and_tool_use_false_disables_profile_toolsets(monkeypatch, tmp_path: Path):
    """Explicit Canon toolUse=false must suppress Hermes profile default tools.

    pre: projection carries hermesProfileId plus a complete modelRoute with toolUse set to False.
    post: _run_agent receives profile/provider/model, toolsets=[], and use_config_toolsets=False.
    post: successful phase result keeps the usual succeeded/output/session/checkpoint/cursor schema.
    """

    import hermes_cli.oneshot as oneshot
    from tools.canon_workflow_command import _GatewayHermesScopedPhaseSession

    captured = {}

    def fake_run_agent(prompt, **kwargs):
        captured["kwargs"] = kwargs
        return '{"ok": true}'

    monkeypatch.setattr(oneshot, "_run_agent", fake_run_agent)
    session = _GatewayHermesScopedPhaseSession(
        envelope_projection={
            "executionContext": {"workingDirectory": str(tmp_path)},
            "hermesProfileId": "proofloopworker",
            "modelRoute": {
                "provider": "openai-codex",
                "model": "gpt-5.3-codex",
                "toolUse": False,
                "structuredOutput": True,
            },
        },
        artifacts_dir=None,
    )

    result = session.run_phase(
        {
            "runId": "run-profile-tooluse-false",
            "phaseId": "proof_loop_red_prep",
            "objective": "Return a schema-shaped JSON object without tools.",
            "inputs": {},
            "outputSchema": {"schema": {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}},
        }
    )

    assert result["status"] == "succeeded"
    assert result["output"] == {"ok": True}
    assert result["agentSessionRef"] == "hermes-current-gateway:run-profile-tooluse-false:proof_loop_red_prep:1"
    assert result["agentCheckpointRef"] == "hermes-current-gateway:checkpoint:run-profile-tooluse-false:proof_loop_red_prep:1"
    assert result["eventCursorRefs"] == [
        "hermes-current-gateway:run-profile-tooluse-false:proof_loop_red_prep:1:events:run-profile-tooluse-false:proof_loop_red_prep:curs"
    ]
    assert captured["kwargs"]["profile"] == "proofloopworker"
    assert captured["kwargs"]["provider"] == "openai-codex"
    assert captured["kwargs"]["model"] == "gpt-5.3-codex"
    assert captured["kwargs"]["toolsets"] == []
    assert captured["kwargs"]["use_config_toolsets"] is False


def test_phase_session_preserves_plain_text_output_for_canon_validation(monkeypatch, tmp_path: Path):
    """Non-JSON model text must flow through as succeeded raw output with continuation refs.

    pre: Hermes oneshot returns plain text instead of a JSON object.
    post: run_phase does not raise the local JSON-object parse RuntimeError.
    post: the raw text and backend refs are preserved for Canon validation/retry handling.
    """

    import hermes_cli.oneshot as oneshot
    from tools.canon_workflow_command import _GatewayHermesScopedPhaseSession

    def fake_run_agent(prompt, **kwargs):
        return "not json"

    monkeypatch.setattr(oneshot, "_run_agent", fake_run_agent)
    session = _GatewayHermesScopedPhaseSession(
        envelope_projection={
            "executionContext": {"workingDirectory": str(tmp_path)},
            "modelRoute": {"provider": "openai-codex", "model": "gpt-5.4"},
        },
        artifacts_dir=None,
    )

    result = session.run_phase(
        {
            "runId": "run-invalid-json-text",
            "phaseId": "proof_loop_red_prep",
            "objective": "Return phase output.",
            "inputs": {},
            "outputSchema": {"schema": {"type": "object"}},
        }
    )

    assert result == {
        "status": "succeeded",
        "output": "not json",
        "agentSessionRef": "hermes-current-gateway:run-invalid-json-text:proof_loop_red_prep:1",
        "agentCheckpointRef": "hermes-current-gateway:checkpoint:run-invalid-json-text:proof_loop_red_prep:1",
        "eventCursorRefs": [
            "hermes-current-gateway:run-invalid-json-text:proof_loop_red_prep:1:events:run-invalid-json-text:proof_loop_red_prep:cursor:1"
        ],
    }


@pytest.mark.parametrize("raw_output", ['[1, 2, 3]', '"hello"', '42', 'true', 'null'])
def test_phase_session_preserves_json_non_object_output_for_canon_validation(
    monkeypatch, tmp_path: Path, raw_output: str
):
    """JSON scalars/arrays must stay raw so Canon validation, not Hermes parsing, classifies them."""

    import hermes_cli.oneshot as oneshot
    from tools.canon_workflow_command import _GatewayHermesScopedPhaseSession

    def fake_run_agent(prompt, **kwargs):
        return raw_output

    monkeypatch.setattr(oneshot, "_run_agent", fake_run_agent)
    session = _GatewayHermesScopedPhaseSession(
        envelope_projection={
            "executionContext": {"workingDirectory": str(tmp_path)},
            "modelRoute": {"provider": "openai-codex", "model": "gpt-5.4"},
        },
        artifacts_dir=None,
    )

    result = session.run_phase(
        {
            "runId": "run-invalid-json-shape",
            "phaseId": "proof_loop_red_prep",
            "objective": "Return phase output.",
            "inputs": {},
            "outputSchema": {"schema": {"type": "object"}},
        }
    )

    assert result["status"] == "succeeded"
    assert result["output"] == raw_output
    assert result["agentSessionRef"] == "hermes-current-gateway:run-invalid-json-shape:proof_loop_red_prep:1"
    assert result["agentCheckpointRef"] == "hermes-current-gateway:checkpoint:run-invalid-json-shape:proof_loop_red_prep:1"
    assert result["eventCursorRefs"] == [
        "hermes-current-gateway:run-invalid-json-shape:proof_loop_red_prep:1:events:run-invalid-json-shape:proof_loop_red_prep:cursor:1"
    ]


@pytest.mark.asyncio
async def test_canon_command_rejects_dry_run_or_direct_helper_proof(monkeypatch):
    sender = AsyncMock(return_value={"message_id": "unused"})

    result = await handle_gateway_canon_command_live(
        _make_event(
            "/canon run solution-modeling Compare blue and green with three bullet criteria.",
            thread_id="777",
            message_id="origin-9",
        ),
        send_review_prompt=sender,
    )

    assert "failed closed" in result
    assert "--inputs-json <json-object>" in result
    assert "free-form task text is not accepted" in result
    sender.assert_not_awaited()


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
        _make_event(
            '/canon run solution-modeling --inputs-json {"request":"real task for live path"}',
            thread_id="777",
            message_id="origin-9",
        )
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
async def test_gateway_local_launch_request_accepts_request_json_only_and_preserves_request_authority(
    monkeypatch,
    tmp_path: Path,
):
    """Gateway launcher must accept Canon CLI requestJson-only envelopes.

    pre: the incoming transport envelope carries exactly one requestJson path authority and the
         loaded file is a run-request.schema.v2 object with request-owned runId/inputs.
    post: the watcher loads that file, forwards matching public runId/inputs plus preserved
          private requestAuthority into startWorkflow, and returns the loaded inputs in the
          gateway-owned response envelope.
    raises: AssertionError while requestJson-only Canon CLI envelopes are still rejected.
    """

    import gateway.run as gateway_run
    from integrations.hermes.canon_hermes import workflow_facade

    runner = _make_runner()
    observed: dict[str, object] = {}
    request_json = tmp_path / "request.json"
    request_json.write_text(
        json.dumps(
            {
                "schemaVersion": "run-request.schema.v2",
                "workflowRef": "src/canon_workflows/packs/third_workflow/workflow.json",
                "runId": "run-third-workflow-1",
                "inputs": {"request": "Compare blue and green with three bullet criteria."},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def _fake_start_workflow(payload, stores=None):
        observed["payload"] = payload
        observed["stores"] = stores
        delivery = stores["review_sender"](
            {
                "target": "telegram:-100123456:777",
                "runId": "run-third-workflow-1",
                "message": "Review launch",
                "callbacks": [{"label": "Approve", "actionId": "approve"}],
                "gateIdentity": {"id": "gate-launch-1"},
                "downloadableArtifacts": [],
            }
        )
        return {
            "status": "awaiting-human-review",
            "runId": "run-third-workflow-1",
            "checkpoint": {"checkpointId": "current-gateway:run-third-workflow-1"},
            "delivery": {"kind": "sent", **delivery},
        }

    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: str(tmp_path / "hermes-home"))
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(workflow_facade, "startWorkflow", _fake_start_workflow)

    payload = {
        "api": "canon_gateway_cli_run.v1",
        "action": "run",
        "requestId": "cg-launch-request-json-only",
        "workflow": "third-workflow",
        "requestJson": str(request_json),
        "target": {"platform": "telegram", "chatId": "-100123456", "threadId": "777"},
    }

    result = await runner._execute_canon_gateway_launch_request(payload)

    assert result["ok"] is True
    assert result["workflow"] == "third-workflow"
    assert result["inputs"] == {"request": "Compare blue and green with three bullet criteria."}
    start_payload = observed["payload"]
    assert start_payload["workflowId"] == "third-workflow"
    assert start_payload["runId"] == "run-third-workflow-1"
    assert start_payload["inputs"] == {"request": "Compare blue and green with three bullet criteria."}
    assert start_payload["requestAuthority"] == {
        "schemaVersion": "run-request.schema.v2",
        "workflowRef": "src/canon_workflows/packs/third_workflow/workflow.json",
        "runId": "run-third-workflow-1",
        "inputs": {"request": "Compare blue and green with three bullet criteria."},
    }
    stores = observed["stores"]
    assert stores["root"] == str(tmp_path / "hermes-home" / "canon-current-gateway")
    assert hasattr(stores["phase_backend_client"], "start_scoped_session")
    assert hasattr(stores["tool_host"], "call_tool")
    assert callable(stores["review_sender"])
    assert runner._test_observed["review_prompt"] == {
        "chat_id": "-100123456",
        "message": "Review launch",
        "run_id": "run-third-workflow-1",
        "metadata": {
            "thread_id": "777",
            "callbacks": [{"label": "Approve", "actionId": "approve"}],
            "gate_identity": {"id": "gate-launch-1"},
            "downloadableArtifacts": [],
        },
    }
    assert result["run"]["runId"] == "run-third-workflow-1"
    assert result["run"]["status"] == "awaiting-human-review"
    assert result["run"]["checkpoint"] == {"checkpointId": "current-gateway:run-third-workflow-1"}
    assert result["run"]["delivery"] == {
        "kind": "sent",
        "messageId": "review-msg-1",
        "chatId": "-100123456",
        "threadId": "777",
    }


@pytest.mark.asyncio
async def test_gateway_local_launch_request_uses_gateway_owned_executor_and_preserves_runtime_request_authority(
    monkeypatch,
    tmp_path: Path,
):
    """Gateway launcher must load request_json into one current-gateway-owned execution seam.

    pre: the public launch tool already wrote only workflow/target/requestJson path authority.
    post: request_json is loaded exactly once at runtime, startWorkflow receives the preserved
          runId/inputs authority, and awaiting-human-review success includes checkpoint/delivery evidence.
    raises: AssertionError while the launcher still executes canon.cli.run_cli_workflow_command.
    """

    import canon.cli as canon_cli
    import gateway.run as gateway_run
    from integrations.hermes.canon_hermes import workflow_facade

    runner = _make_runner()
    observed: dict[str, object] = {}


    def _reject_shared_cli(*args, **kwargs):
        raise AssertionError("gateway launcher must not use canon.cli.run_cli_workflow_command")

    def _fake_start_workflow(payload, stores=None):
        observed["payload"] = payload
        observed["stores"] = stores
        delivery = stores["review_sender"](
            {
                "target": "telegram:-100123456:777",
                "runId": "run-third-workflow-1",
                "message": "Review launch",
                "callbacks": [{"label": "Approve", "actionId": "approve"}],
                "gateIdentity": {"id": "gate-launch-1"},
                "downloadableArtifacts": [],
            }
        )
        return {
            "status": "awaiting-human-review",
            "runId": "run-third-workflow-1",
            "checkpoint": {"checkpointId": "current-gateway:run-third-workflow-1"},
            "delivery": {"kind": "sent", **delivery},
        }

    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: str(tmp_path / "hermes-home"))
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(canon_cli, "run_cli_workflow_command", _reject_shared_cli)
    monkeypatch.setattr(workflow_facade, "startWorkflow", _fake_start_workflow)

    payload = {
        "api": "canon_gateway_cli_run.v1",
        "action": "run",
        "requestId": "cg-launch-test-1",
        "workflow": "third-workflow",
        "inputs": {"request": "Compare blue and green with three bullet criteria."},
        "target": {"platform": "telegram", "chatId": "-100123456", "threadId": "777"},
    }

    result = await runner._execute_canon_gateway_launch_request(payload)

    assert result["ok"] is True
    assert result["workflow"] == "third-workflow"
    assert result["inputs"] == {"request": "Compare blue and green with three bullet criteria."}
    start_payload = observed["payload"]
    assert start_payload["workflowId"] == "third-workflow"
    assert isinstance(start_payload["runId"], str) and start_payload["runId"]
    assert start_payload["inputs"] == {"request": "Compare blue and green with three bullet criteria."}
    assert "requestJson" not in start_payload
    assert "requestAuthority" not in start_payload
    stores = observed["stores"]
    assert stores["root"] == str(tmp_path / "hermes-home" / "canon-current-gateway")
    assert hasattr(stores["phase_backend_client"], "start_scoped_session")
    assert hasattr(stores["tool_host"], "call_tool")
    assert callable(stores["review_sender"])
    assert runner._test_observed["review_prompt"] == {
        "chat_id": "-100123456",
        "message": "Review launch",
        "run_id": "run-third-workflow-1",
        "metadata": {
            "thread_id": "777",
            "callbacks": [{"label": "Approve", "actionId": "approve"}],
            "gate_identity": {"id": "gate-launch-1"},
            "downloadableArtifacts": [],
        },
    }
    assert result["run"]["runId"] == "run-third-workflow-1"
    assert result["run"]["status"] == "awaiting-human-review"
    assert result["run"]["checkpoint"] == {"checkpointId": "current-gateway:run-third-workflow-1"}
    assert result["run"]["delivery"] == {
        "kind": "sent",
        "messageId": "review-msg-1",
        "chatId": "-100123456",
        "threadId": "777",
    }


@pytest.mark.asyncio
async def test_gateway_local_launch_request_rejects_both_inputs_and_request_json(monkeypatch, tmp_path: Path):
    import gateway.run as gateway_run

    runner = _make_runner()
    request_json = tmp_path / "request.json"
    request_json.write_text(
        json.dumps(
            {
                "schemaVersion": "run-request.schema.v2",
                "workflowRef": "src/canon_workflows/packs/third_workflow/workflow.json",
                "runId": "run-third-workflow-1",
                "inputs": {"request": "from request json"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: str(tmp_path / "hermes-home"))
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    payload = {
        "api": "canon_gateway_cli_run.v1",
        "action": "run",
        "requestId": "cg-launch-both-authorities",
        "workflow": "third-workflow",
        "inputs": {"request": "from inline payload"},
        "requestJson": str(request_json),
        "target": {"platform": "telegram", "chatId": "-100123456", "threadId": "777"},
    }

    with pytest.raises(ValueError, match="exactly one"):
        await runner._execute_canon_gateway_launch_request(payload)


@pytest.mark.asyncio
async def test_gateway_local_launch_request_rejects_missing_inputs_and_request_json(monkeypatch, tmp_path: Path):
    import gateway.run as gateway_run

    runner = _make_runner()

    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: str(tmp_path / "hermes-home"))
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    payload = {
        "api": "canon_gateway_cli_run.v1",
        "action": "run",
        "requestId": "cg-launch-missing-authority",
        "workflow": "third-workflow",
        "target": {"platform": "telegram", "chatId": "-100123456", "threadId": "777"},
    }

    with pytest.raises(ValueError, match="exactly one"):
        await runner._execute_canon_gateway_launch_request(payload)


@pytest.mark.asyncio
async def test_gateway_local_launch_request_awaiting_review_without_checkpoint_and_delivery_fails_closed(
    monkeypatch,
    tmp_path: Path,
):
    """Awaiting-human-review must not count as success without checkpoint and delivery evidence.

    pre: the launcher receives a runtime result claiming awaiting-human-review but with no persisted
          checkpoint or delivery facts.
    post: gateway wrapper returns ok=false and does not bless the launch as review-ready.
    raises: AssertionError while status text alone can still produce ok=true.
    """

    import gateway.run as gateway_run
    from integrations.hermes.canon_hermes import workflow_facade

    runner = _make_runner()
    request_json = tmp_path / "request.json"
    request_json.write_text('{"runId":"run-third-workflow-1","inputs":{}}\n', encoding="utf-8")

    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: str(tmp_path / "hermes-home"))
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(
        workflow_facade,
        "startWorkflow",
        lambda payload, stores=None: {
            "status": "awaiting-human-review",
            "result": {"status": "awaiting-human-review", "message": "queued for review"},
            "artifacts": {},
            "runId": "run-third-workflow-1",
        },
    )

    payload = {
        "api": "canon_gateway_cli_run.v1",
        "action": "run",
        "requestId": "cg-launch-awaiting-review-without-evidence",
        "workflow": "third-workflow",
        "inputs": {},
        "target": {"platform": "telegram", "chatId": "-100123456", "threadId": "777"},
    }

    result = await runner._execute_canon_gateway_launch_request(payload)

    assert result["ok"] is False
    assert result["run"]["runId"] == "run-third-workflow-1"
    assert result["run"]["status"] == "awaiting-human-review"
    assert result["run"]["result"] == {"status": "awaiting-human-review", "message": "queued for review"}


@pytest.mark.asyncio
async def test_gateway_local_launch_request_false_green_result_fails_closed(monkeypatch, tmp_path: Path):
    import gateway.run as gateway_run
    from integrations.hermes.canon_hermes import workflow_facade

    runner = _make_runner()
    request_json = tmp_path / "request.json"
    request_json.write_text('{"runId":"sm-260602210652","inputs":{}}\n', encoding="utf-8")

    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(
        workflow_facade,
        "startWorkflow",
        lambda payload, stores=None: {
            "status": "completed",
            "result": {
                "status": "blocked_preconditions",
                "reason": "modelPackage.targetRepo must resolve to an existing directory",
            },
            "artifacts": {},
            "runId": "sm-260602210652",
        },
    )

    payload = {
        "api": "canon_gateway_cli_run.v1",
        "action": "run",
        "requestId": "cg-launch-false-green",
        "workflow": "third-workflow",
        "inputs": {},
        "target": {"platform": "telegram", "chatId": "-100123456", "threadId": "777"},
    }

    result = await runner._execute_canon_gateway_launch_request(payload)

    assert result["ok"] is False
    assert result["run"]["runId"] == "sm-260602210652"
    assert result["run"]["status"] == "completed"
    assert result["run"]["result"]["status"] == "blocked_preconditions"
    assert result["run"]["reason"] == "modelPackage.targetRepo must resolve to an existing directory"


@pytest.mark.asyncio
async def test_gateway_local_launch_request_rejects_non_object_inputs(monkeypatch, tmp_path: Path):
    import gateway.run as gateway_run

    runner = _make_runner()

    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: str(tmp_path / "hermes-home"))
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    payload = {
        "api": "canon_gateway_cli_run.v1",
        "action": "run",
        "requestId": "cg-launch-non-object-inputs",
        "workflow": "third-workflow",
        "inputs": [],
        "target": {"platform": "telegram", "chatId": "-100123456", "threadId": "777"},
    }

    with pytest.raises(ValueError, match="inputs must be an object"):
        await runner._execute_canon_gateway_launch_request(payload)


@pytest.mark.asyncio
async def test_gateway_local_launch_request_rejects_legacy_api_and_action(monkeypatch):
    import gateway.run as gateway_run

    runner = _make_runner()
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    with pytest.raises(ValueError, match="unsupported api"):
        await runner._execute_canon_gateway_launch_request(
            {
                "api": "canon_gateway_local_launch.v1",
                "action": "run",
                "requestId": "cg-launch-test-2",
                "workflow": "third-workflow",
                "target": {"platform": "telegram", "chatId": "-100123456", "threadId": "777"},
            }
        )

    with pytest.raises(ValueError, match="unsupported action"):
        await runner._execute_canon_gateway_launch_request(
            {
                "api": "canon_gateway_cli_run.v1",
                "action": "launch_solution_modeling",
                "requestId": "cg-launch-test-3",
                "workflow": "third-workflow",
                "target": {"platform": "telegram", "chatId": "-100123456", "threadId": "777"},
            }
        )


@pytest.mark.asyncio
async def test_gateway_local_launch_request_accepts_no_thread_target(monkeypatch, tmp_path: Path):
    import gateway.run as gateway_run
    from integrations.hermes.canon_hermes import workflow_facade

    runner = _make_runner()

    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: str(tmp_path / "hermes-home"))
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(
        workflow_facade,
        "startWorkflow",
        lambda payload, stores=None: {
            "status": "awaiting-human-review",
            "runId": "run-third-workflow-1",
            "checkpoint": {"checkpointId": "current-gateway:run-third-workflow-1"},
            "delivery": {"kind": "sent", **stores["review_sender"]({
                "target": "telegram:-100123456",
                "runId": "run-third-workflow-1",
                "message": "queued for review",
                "callbacks": [],
                "gateIdentity": {"id": "gate-launch-1"},
                "downloadableArtifacts": [],
            })},
        },
    )

    result = await runner._execute_canon_gateway_launch_request(
        {
            "api": "canon_gateway_cli_run.v1",
            "action": "run",
            "requestId": "cg-launch-no-thread",
            "workflow": "third-workflow",
            "inputs": {"request": "DM launch"},
            "target": {"platform": "telegram", "chatId": "-100123456"},
        }
    )

    assert result["ok"] is True
    assert result["target"] == {"platform": "telegram", "chatId": "-100123456"}
    assert result["run"]["delivery"] == {
        "kind": "sent",
        "messageId": "review-msg-1",
        "chatId": "-100123456",
    }
    assert runner._test_observed["review_prompt"] == {
        "chat_id": "-100123456",
        "message": "queued for review",
        "run_id": "run-third-workflow-1",
        "metadata": {
            "callbacks": [],
            "gate_identity": {"id": "gate-launch-1"},
            "downloadableArtifacts": [],
        },
    }


@pytest.mark.asyncio
async def test_gateway_local_launch_request_persists_origin_evidence_for_chat_only_target(monkeypatch, tmp_path: Path):
    import gateway.run as gateway_run
    from canon.journal import SqliteExecutionJournal
    from integrations.hermes.canon_hermes import workflow_facade

    runner = _make_runner()

    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: str(tmp_path / "hermes-home"))
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(
        workflow_facade,
        "startWorkflow",
        lambda payload, stores=None: {
            "status": "awaiting-human-review",
            "runId": "run-chat-only-1",
            "checkpoint": {"checkpointId": "current-gateway:run-chat-only-1"},
            "delivery": {
                "kind": "sent",
                **stores["review_sender"](
                    {
                        "target": "telegram:-100123456",
                        "runId": "run-chat-only-1",
                        "message": "queued for review",
                        "callbacks": [],
                        "gateIdentity": {"id": "gate-launch-chat-only"},
                        "downloadableArtifacts": [],
                    }
                ),
            },
        },
    )

    result = await runner._execute_canon_gateway_launch_request(
        {
            "api": "canon_gateway_cli_run.v1",
            "action": "run",
            "requestId": "cg-launch-chat-only",
            "workflow": "third-workflow",
            "inputs": {"request": "DM launch"},
            "target": {"platform": "telegram", "chatId": "-100123456"},
        }
    )

    assert result["ok"] is True
    rows = SqliteExecutionJournal(tmp_path / "hermes-home" / "canon-current-gateway" / "journal.sqlite3").list_run(
        "run-chat-only-1"
    )
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["runtimeContext"]["gateway_source"] == "telegram:-100123456"
    assert payload["runtimeContext"]["gateway"] == {
        "platform": "telegram",
        "chatId": "-100123456",
    }
    assert "threadId" not in payload["runtimeContext"]["gateway"]


@pytest.mark.asyncio
async def test_gateway_local_launch_request_persists_origin_evidence_for_thread_target(monkeypatch, tmp_path: Path):
    import gateway.run as gateway_run
    from canon.journal import SqliteExecutionJournal
    from integrations.hermes.canon_hermes import workflow_facade

    runner = _make_runner()

    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: str(tmp_path / "hermes-home"))
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(
        workflow_facade,
        "startWorkflow",
        lambda payload, stores=None: {
            "status": "awaiting-human-review",
            "runId": "run-thread-1",
            "checkpoint": {"checkpointId": "current-gateway:run-thread-1"},
            "delivery": {
                "kind": "sent",
                **stores["review_sender"](
                    {
                        "target": "telegram:-100123456:777",
                        "runId": "run-thread-1",
                        "message": "queued for review",
                        "callbacks": [],
                        "gateIdentity": {"id": "gate-launch-thread"},
                        "downloadableArtifacts": [],
                    }
                ),
            },
        },
    )

    result = await runner._execute_canon_gateway_launch_request(
        {
            "api": "canon_gateway_cli_run.v1",
            "action": "run",
            "requestId": "cg-launch-thread",
            "workflow": "third-workflow",
            "inputs": {"request": "Topic launch"},
            "target": {"platform": "telegram", "chatId": "-100123456", "threadId": "777"},
        }
    )

    assert result["ok"] is True
    rows = SqliteExecutionJournal(tmp_path / "hermes-home" / "canon-current-gateway" / "journal.sqlite3").list_run(
        "run-thread-1"
    )
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["runtimeContext"]["gateway_source"] == "telegram:-100123456:777"
    assert payload["runtimeContext"]["gateway"] == {
        "platform": "telegram",
        "chatId": "-100123456",
        "threadId": "777",
    }


@pytest.mark.asyncio
async def test_gateway_local_launch_then_observe_same_temp_durable_root_closes_false_green_contract(
    monkeypatch,
    tmp_path: Path,
):
    """One temp durable root must keep launch truth and follow-up observe truth aligned.

    pre: both a blocked launch and an awaiting-human-review launch execute through the gateway-owned
         workflow facade seam against the same temp current-gateway durable root.
    post: blocked durable truth returns ok=false and writes no accepted-origin row, while the accepted
          durable truth returns ok=true and is immediately rediscoverable via origin-scoped latest/list
          plus run-scoped full observe reads on that same root.
    raises: AssertionError when wrapper success can disagree with durable Canon status or follow-up
            observe reads cannot rediscover the accepted run.
    """

    import gateway.run as gateway_run
    import integrations.hermes.canon_hermes.current_gateway_runner as runner_config
    from canon.journal import SqliteExecutionJournal
    from integrations.hermes.canon_hermes import workflow_facade

    observe_tool = importlib.import_module("tools.canon_workflow_observe_tool")
    runner = _make_runner()
    hermes_home = tmp_path / "hermes-home"
    durable_root = hermes_home / "canon-current-gateway"

    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(
        runner_config,
        "build_current_gateway_runner_config",
        lambda **_: SimpleNamespace(
            journal_path=durable_root / "journal.sqlite3",
            artifacts_dir=durable_root / "artifacts",
        ),
    )

    observed_roots: list[str] = []

    def _fake_start_workflow(payload, stores=None):
        observed_roots.append(str(stores["root"]))
        if payload["inputs"].get("mode") == "blocked":
            return {
                "status": "completed",
                "result": {
                    "status": "blocked_preconditions",
                    "reason": "workspace root must already exist",
                },
                "artifacts": {},
                "runId": "run-blocked-e2e-1",
            }

        return {
            "status": "awaiting-human-review",
            "runId": "run-awaiting-e2e-1",
            "checkpoint": {"checkpointId": "current-gateway:run-awaiting-e2e-1"},
            "delivery": {
                "kind": "sent",
                **stores["review_sender"](
                    {
                        "target": "telegram:-100123456:777",
                        "runId": "run-awaiting-e2e-1",
                        "message": "queued for review",
                        "callbacks": [],
                        "gateIdentity": {"id": "gate-launch-e2e"},
                        "downloadableArtifacts": [],
                    }
                ),
            },
            "result": {"status": "awaiting-human-review", "message": "queued for review"},
            "artifacts": {},
        }

    monkeypatch.setattr(workflow_facade, "startWorkflow", _fake_start_workflow)

    blocked_result = await runner._execute_canon_gateway_launch_request(
        {
            "api": "canon_gateway_cli_run.v1",
            "action": "run",
            "requestId": "cg-launch-blocked-e2e",
            "workflow": "third-workflow",
            "inputs": {"mode": "blocked"},
            "target": {"platform": "telegram", "chatId": "-100999001"},
        }
    )

    journal = SqliteExecutionJournal(durable_root / "journal.sqlite3")
    blocked_latest = json.loads(observe_tool._handle_tool({"origin": "telegram:-100999001", "surface": "latest"}))

    assert blocked_result["ok"] is False
    assert blocked_result["run"]["runId"] == "run-blocked-e2e-1"
    assert blocked_result["run"]["status"] == "completed"
    assert blocked_result["run"]["result"]["status"] == "blocked_preconditions"
    assert blocked_result["run"]["reason"] == "workspace root must already exist"
    assert journal.list_run("run-blocked-e2e-1") == []
    assert blocked_latest["success"] is False

    accepted_result = await runner._execute_canon_gateway_launch_request(
        {
            "api": "canon_gateway_cli_run.v1",
            "action": "run",
            "requestId": "cg-launch-awaiting-e2e",
            "workflow": "third-workflow",
            "inputs": {"mode": "accepted"},
            "target": {"platform": "telegram", "chatId": "-100123456", "threadId": "777"},
        }
    )

    latest = json.loads(observe_tool._handle_tool({"origin": "telegram:-100123456:777", "surface": "latest"}))
    listing = json.loads(observe_tool._handle_tool({"origin": "telegram:-100123456:777", "surface": "list"}))
    full = json.loads(
        observe_tool._handle_tool(
            {"origin": "telegram:-100123456:777", "surface": "full", "run_id": "run-awaiting-e2e-1"}
        )
    )
    accepted_rows = journal.list_run("run-awaiting-e2e-1")

    assert accepted_result["ok"] is True
    assert accepted_result["run"]["runId"] == "run-awaiting-e2e-1"
    assert accepted_result["run"]["status"] == "awaiting-human-review"
    assert len(accepted_rows) == 1
    assert accepted_rows[0]["payload"]["runtimeContext"]["gateway_source"] == "telegram:-100123456:777"
    assert latest["success"] is True
    assert latest["run_id"] == accepted_result["run"]["runId"]
    assert listing["success"] is True
    assert "run-awaiting-e2e-1" in listing["markdown"]
    assert full["success"] is True
    assert "{" not in full["markdown"] and "}" not in full["markdown"]
    assert observed_roots == [str(durable_root), str(durable_root)]


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
                "api": "canon_gateway_cli_run.v1",
                "action": "run",
                "requestId": "cg-launch-test-3",
                "workflow": "third-workflow",
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


@pytest.mark.asyncio
async def test_gateway_local_launch_request_file_rejects_payload_request_id_traversal(monkeypatch, tmp_path: Path):
    """Claimed request files must use the filename id for invalid payload request ids.

    pre: a local request file has a safe claimed filename but a traversal-valued payload requestId.
    post: the gateway writes a fail-closed response for the safe filename id and does not write outside responses.
    raises: AssertionError when payload requestId controls the response path.
    """

    import tools.canon_gateway_launch as launch_tool

    runner = _make_runner()
    responses_dir = tmp_path / "responses"
    responses_dir.mkdir()
    monkeypatch.setattr(launch_tool, "_response_file", lambda request_id: responses_dir / f"{request_id}.response.json")

    async def _reject_execution(payload):
        raise AssertionError("invalid requestId should be rejected before execution")

    runner._execute_canon_gateway_launch_request = _reject_execution
    request_path = tmp_path / "cg-launch-safe.request.processing.json"
    request_path.write_text(
        json.dumps(
            {
                "api": "canon_gateway_cli_run.v1",
                "action": "run",
                "requestId": "../escaped",
                "workflow": "third-workflow",
                "inputs": {"request": "do not escape"},
                "target": {"platform": "telegram", "chatId": "-100123456"},
            }
        ),
        encoding="utf-8",
    )

    await runner._process_canon_gateway_launch_request(request_path)

    safe_response_path = responses_dir / "cg-launch-safe.response.json"
    escaped_response_path = tmp_path / "escaped.response.json"
    assert not request_path.exists()
    assert safe_response_path.exists()
    assert not escaped_response_path.exists()
    response = json.loads(safe_response_path.read_text(encoding="utf-8"))
    assert response["ok"] is False
    assert response["requestId"] == "cg-launch-safe"
    assert "invalid requestId" in response["error"]


@pytest.mark.asyncio
async def test_gateway_launch_watcher_recovers_stale_processing_request_after_restart(monkeypatch, tmp_path: Path):
    """Gateway watcher must not orphan claimed Canon launch requests after restart.

    pre: a previous gateway renamed one request to *.processing.json and exited before writing a response.
    post: a new watcher tick treats the stale processing file as work and processes it exactly once.
    raises: AssertionError while stale processing files remain invisible to the watcher.
    """

    import asyncio
    import gateway.run as gateway_run
    import tools.canon_gateway_launch as launch_tool

    runner = _make_runner()
    runner._running = True
    requests_dir = tmp_path / "requests"
    responses_dir = tmp_path / "responses"
    requests_dir.mkdir()
    responses_dir.mkdir()
    processing_path = requests_dir / "cg-launch-stale.request.processing.json"
    processing_path.write_text(
        json.dumps(
            {
                "api": "canon_gateway_cli_run.v1",
                "action": "run",
                "requestId": "cg-launch-stale",
                "workflow": "solution-modeling",
                "inputs": {"request": "Model a toy script that prints hello world"},
                "target": {"platform": "telegram", "chatId": "-100123456", "threadId": "777"},
            }
        ),
        encoding="utf-8",
    )
    stale_time = time.time() - 600
    os.utime(processing_path, (stale_time, stale_time))

    processed: list[Path] = []

    async def _fake_process(path: Path) -> None:
        processed.append(path)
        runner._running = False

    original_sleep = asyncio.sleep
    sleep_calls = 0

    async def _fast_sleep(delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 3:
            runner._running = False
        await original_sleep(0)

    monkeypatch.setattr(launch_tool, "_requests_dir", lambda: requests_dir)
    monkeypatch.setattr(launch_tool, "_response_file", lambda request_id: responses_dir / f"{request_id}.response.json")
    monkeypatch.setattr(gateway_run.asyncio, "sleep", _fast_sleep)
    runner._process_canon_gateway_launch_request = _fake_process

    await runner._canon_gateway_launch_watcher(interval=0.01)

    assert processed == [processing_path]


def test_gateway_local_launch_processing_stale_threshold_respects_timeout_plus_grace(tmp_path: Path):
    """Stale recovery must honor the larger of the default threshold and timeout-plus-grace.

    pre: one claimed processing file advertises a 600-second request timeout and another relies on defaults.
    post: the 600-second request stays non-stale before 630 seconds and becomes stale at 630 seconds,
          while the default path still becomes stale at the normal threshold.
    raises: AssertionError when long-running requests can be recovered before their timeout contract expires.
    """

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    long_wait_path = tmp_path / "cg-launch-long.request.processing.json"
    long_wait_path.write_text(json.dumps({"timeoutSeconds": 600}), encoding="utf-8")

    almost_stale = time.time() - 629
    os.utime(long_wait_path, (almost_stale, almost_stale))
    assert runner._canon_gateway_processing_request_is_stale(long_wait_path) is False

    stale = time.time() - 630
    os.utime(long_wait_path, (stale, stale))
    assert runner._canon_gateway_processing_request_is_stale(long_wait_path) is True

    default_path = tmp_path / "cg-launch-default.request.processing.json"
    default_path.write_text(json.dumps({}), encoding="utf-8")
    default_stale = time.time() - 301
    os.utime(default_path, (default_stale, default_stale))
    assert runner._canon_gateway_processing_request_is_stale(default_path) is True


@pytest.mark.asyncio
async def test_canon_command_stays_workflow_neutral_and_uses_generic_workflow_facade(monkeypatch):
    """/canon must stay workflow-neutral and dispatch through the generic facade seam.

    The adapter contract (docs/03-hermes-adapter-contract.md line 24) declares that
    the public workflow facade exposes startWorkflow/pauseWorkflow/resumeWorkflow/
    cancelWorkflow/inspectWorkflow/restartWorkflowFromCheckpoint/streamWorkflowEvents,
    and /canon must be a workflow-selection wrapper over that generic facade, not
    hardcode solution-modeling as the sole production path.
    """
    from integrations.hermes.canon_hermes.workflow_facade import (
        startWorkflow,
        inspectWorkflow,
        resumeWorkflow,
        cancelWorkflow,
    )
    import tools.canon_workflow_command as canon_cmd

    assert all(callable(fn) for fn in (startWorkflow, inspectWorkflow, resumeWorkflow, cancelWorkflow))

    parsed = parse_gateway_run_command(["run", "other-workflow", "--inputs-json", '{"request":"hello"}'])
    assert parsed == {"workflow": "other-workflow", "inputs": {"request": "hello"}}

    observed: dict[str, object] = {}

    async def _fake_run_gateway_canon_cli_command(
        *,
        workflow: str,
        inputs: dict[str, object],
        gateway_source,
        gateway_root,
        send_review_prompt,
    ) -> str:
        observed["workflow"] = workflow
        observed["inputs"] = inputs
        observed["gateway_source"] = gateway_source
        observed["gateway_root"] = gateway_root
        observed["send_review_prompt"] = send_review_prompt
        return f"generic-dispatch:{workflow}:{inputs['request']}"

    monkeypatch.setattr(canon_cmd, "_run_gateway_canon_cli_command", _fake_run_gateway_canon_cli_command)

    result = await handle_gateway_canon_command_live(
        _make_event('/canon run other-workflow --inputs-json {"request":"hello"}', thread_id="777"),
        send_review_prompt=AsyncMock(return_value={"message_id": "unused"}),
    )

    assert result == "generic-dispatch:other-workflow:hello"
    assert observed["workflow"] == "other-workflow"
    assert observed["inputs"] == {"request": "hello"}
    assert observed["gateway_source"]["thread_id"] == "777"
    assert observed["gateway_root"].name == "canon-current-gateway"
    assert callable(observed["send_review_prompt"])


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
