"""Real Hermes-to-Canon subprocess and SQLite journal regression proofs for J05."""

from __future__ import annotations

from copy import deepcopy
import inspect
import importlib.util
import json
from pathlib import Path
import queue
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from typing import Any

import pytest


def _installed_canon_root() -> Path:
    spec = importlib.util.find_spec("canon")
    assert spec is not None and spec.origin is not None
    return Path(spec.origin).resolve().parents[2]


CANON_ROOT = _installed_canon_root()
sys.path.insert(0, str(CANON_ROOT / "src"))

from hermes_cli.plugins import PluginManager, parse_manifest_file
from canon.cli import _load_source_bound_cli_request
from tools.process_registry import process_registry
from tools.registry import registry


HERMES_ROOT = Path(__file__).resolve().parents[2]
CANON_VENV = Path(sys.executable)


def _canon_durable_probe(action: str, db_path: Path, identity: str) -> Any:
    """Read Canon-owned durable state in the fixed Canon Python environment."""

    program = textwrap.dedent(
        f"""
        import json
        import sys
        from pathlib import Path

        sys.path.insert(0, {str(CANON_ROOT / "src")!r})
        from canon.durability import SqliteCheckpointBackend
        from canon.events import build_run_projection
        from canon.journal import SqliteExecutionJournal

        action = sys.argv[1]
        db_path = Path(sys.argv[2])
        identity = sys.argv[3]
        if action == "journal":
            value = SqliteExecutionJournal(db_path).list_run(identity)
        elif action == "projection":
            rows = SqliteExecutionJournal(db_path).list_run(identity)
            projection = build_run_projection(run_id=identity, rows=rows, artifact_refs=[])
            value = {{
                "threadId": projection.thread_id,
                "eventSequence": projection.eventSequence,
                "appendSequence": projection.appendSequence,
                "status": projection.status,
            }}
        elif action == "checkpoint":
            value = SqliteCheckpointBackend(db_path).get_checkpoint(identity)
        else:
            raise SystemExit(f"unsupported Canon durable probe action: {{action}}")
        sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        """
    ).strip()
    completed = subprocess.run(
        [str(CANON_VENV), "-c", program, action, str(db_path), identity],
        check=True,
        capture_output=True,
        text=True,
        cwd=CANON_ROOT,
    )
    return json.loads(completed.stdout)


def _assert_preconditions() -> None:
    """Fail closed when the current checkout and editable Canon are unavailable."""

    assert HERMES_ROOT.is_dir(), f"Hermes worktree is missing: {HERMES_ROOT}"
    assert CANON_ROOT.is_dir(), f"Canon worktree is missing: {CANON_ROOT}"
    assert CANON_VENV.is_file() and CANON_VENV.stat().st_mode & 0o111
    for relative in (
        "plugins/canon/transport.py",
        "plugins/canon/background_runner.py",
        "tests/plugins/test_canon_plugin_launch.py",
    ):
        assert (HERMES_ROOT / relative).is_file(), f"missing Hermes authority: {relative}"
    for relative in (
        "src/canon/cli.py",
        "src/canon/host_provider_registry.py",
        "src/canon/journal.py",
        "tests/helpers/fake_host.py",
    ):
        assert (CANON_ROOT / relative).is_file(), f"missing Canon authority: {relative}"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _copy_pack(tmp_path: Path, *, name: str, run_id: str, thread_id: str) -> Path:
    source = CANON_ROOT / "tests" / "fixtures" / "workflows" / "minimal_pack"
    root = tmp_path / name
    shutil.copytree(source, root)
    request = json.loads((root / "run-request.json").read_text(encoding="utf-8"))
    request["runId"] = run_id
    request["threadId"] = thread_id
    request["runConfiguration"]["executionRoot"] = str(tmp_path / f"execution-{name}")
    _write_json(root / "run-request.json", request)
    routing_path = root / "policies/model-routing.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    routing["profiles"]["default"]["hermesProfileId"] = "j05-test-profile"
    _write_json(routing_path, routing)
    return root


def _human_gate_pack(tmp_path: Path) -> Path:
    root = _copy_pack(
        tmp_path,
        name="checkpoint-pack",
        run_id="run-j05-lifecycle",
        thread_id="thread-j05-lifecycle",
    )
    workflow = json.loads((root / "workflow.json").read_text(encoding="utf-8"))
    workflow["phaseContracts"][0]["inputPorts"] = ["task", "feedback"]
    workflow["statePorts"] = [{
        "portId": "feedback-state",
        "schemaRef": "https://canon.local/spec/schemas/canon-authority-model.schema.v1.json#/$defs/gateFeedbackBundle",
        "valueClass": "semantic",
        "persistence": "durable",
        "initialValue": {"items": []},
        "reducer": {"kind": "append"},
    }]
    workflow["dataEdges"].extend([
        {
            "edgeId": "gate-feedback-to-state",
            "from": {"ownerId": "await-user", "portId": "feedback-output"},
            "to": {"ownerId": workflow["workflowId"], "portId": "feedback-state"},
        },
        {
            "edgeId": "state-feedback-to-draft",
            "from": {"ownerId": workflow["workflowId"], "portId": "feedback-state"},
            "to": {"ownerId": "draft_any_id", "portId": "feedback"},
        },
    ])
    workflow["gates"][0] = {
        "gateId": "await-user",
        "subjects": [{"recordSelector": {"phaseId": "draft_any_id", "portId": "draft"}}],
        "evaluator": {
            "kind": "human",
            "cardBindingRef": "cards/await-user.json",
            "deliveryPolicyRef": "canon://delivery/operator",
        },
        "decisionSchemaRef": "https://canon.local/spec/schemas/canon-authority-model.schema.v1.json#/$defs/gateDecision",
        "routes": {"accept": "phase_2", "revise": "draft_any_id", "reject": "done"},
        "attemptPolicy": {"decisionMaxAttempts": 2, "reworkMaxAttempts": 3},
        "feedbackOutput": {
            "portId": "feedback-output",
            "schemaRef": "https://canon.local/spec/schemas/canon-authority-model.schema.v1.json#/$defs/gateFeedbackBundle",
            "valueClass": "semantic",
            "persistence": "durable",
        },
    }
    workflow["controlFlow"] = {
        "entry": "draft_any_id",
        "edges": [
            {"edgeId": "draft-to-await-user", "from": "draft_any_id", "to": "await-user"},
            {"edgeId": "finalize-to-final-gate", "from": "phase_2", "to": "final_valid"},
        ],
        "terminals": [{"terminalId": "done", "status": "succeeded"}],
    }
    _write_json(root / "workflow.json", workflow)
    _write_json(root / "cards/await-user.json", {
        "cardId": "await-user",
        "title": "Resume approval",
        "actions": ["approve", "revise", "abort"],
        "presentation": {
            "format": "markdown",
            "title": {"path": "title", "style": "h1"},
            "sections": [{"heading": "Draft", "path": "draft", "style": "h2"}],
        },
    })
    return root


SOLUTION_MODELING_TASK_INPUT = (
    "Нужно подготовить карточку задачи для обновления README.md, pyproject.toml и workflow.json; "
    "ничего не записывать до подтверждения."
)
SOLUTION_MODELING_REVISION_INSTRUCTIONS = (
    "Уточни карточку: перечисли README.md, pyproject.toml и workflow.json отдельно и сохрани "
    "ограничение «не записывать до подтверждения»."
)
SOLUTION_MODELING_INITIAL_TASK_CARD: dict[str, Any] = {
    "schemaVersion": "task-card.schema.v1",
    "goal": {"text": "S7_INITIAL_CARD: Подготовить карточку задачи по исходному описанию."},
    "tasks": [{"text": "Сформулировать задачу по явно указанным требованиям."}],
    "deliverables": [{"text": "Семантическая карточка задачи без лишних полей."}],
    "constraints": [{"text": "Не записывать изменения до подтверждения."}],
    "acceptanceCriteria": [{"text": "Карточка соответствует схеме task-card.schema.v1."}],
}
SOLUTION_MODELING_REVISED_TASK_CARD: dict[str, Any] = {
    "schemaVersion": "task-card.schema.v1",
    "goal": {"text": "S7_REVISED_CARD: Подготовить карточку для обновления трёх отдельных файлов."},
    "tasks": [
        {"text": "Перечислить README.md как отдельную задачу."},
        {"text": "Перечислить pyproject.toml как отдельную задачу."},
        {"text": "Перечислить workflow.json как отдельную задачу."},
    ],
    "deliverables": [{"text": "Отдельно проверенная карточка для README.md, pyproject.toml и workflow.json."}],
    "constraints": [{"text": "Сохранять ограничение: не записывать изменения до подтверждения."}],
    "acceptanceCriteria": [
        {"text": "README.md, pyproject.toml и workflow.json перечислены отдельно."},
        {"text": "Карточка соответствует схеме task-card.schema.v1."},
    ],
}
def _write_bootstrap(tmp_path: Path) -> Path:
    """Create the only child-process adapter: the external phase-host boundary."""

    bootstrap = tmp_path / "canon-bootstrap.py"
    bootstrap.write_text(
        textwrap.dedent(
            f"""
            import json
            from copy import deepcopy
            from pathlib import Path
            import sys
            import types
            from types import SimpleNamespace
            from unittest.mock import MagicMock, patch
            sys.path.insert(0, {str(HERMES_ROOT)!r})
            sys.path.insert(0, {str(CANON_ROOT / "src")!r})
            sys.path.insert(0, {str(CANON_ROOT)!r})
            tests_package = types.ModuleType("tests")
            tests_package.__path__ = [{str(CANON_ROOT / 'tests')!r}]
            sys.modules["tests"] = tests_package
            from tests.helpers.fake_host import FakeHostAdapter
            from canon.host_provider_registry import HostAdapterProviderRegistry
            from integrations.hermes.canon_hermes.profile_host_adapter import CanonProfileHostAdapter
            from run_agent import AIAgent
            import hermes_cli.runtime_provider as runtime_provider_module

            TASK_INPUT = {SOLUTION_MODELING_TASK_INPUT!r}
            REVISION_INSTRUCTIONS = {SOLUTION_MODELING_REVISION_INSTRUCTIONS!r}
            INITIAL_TASK_CARD = {SOLUTION_MODELING_INITIAL_TASK_CARD!r}
            REVISED_TASK_CARD = {SOLUTION_MODELING_REVISED_TASK_CARD!r}
            PROFILES_ROOT = Path({str(tmp_path / 'hermes-home')!r})
            TRACE_PATH = Path({str(tmp_path / 'solution-modeling-agent-trace.jsonl')!r})

            runtime_provider_module.resolve_runtime_provider = lambda **kwargs: {{
                "provider": kwargs.get("requested") or "openai-codex",
                "api_mode": "responses",
                "base_url": "https://example.invalid/v1",
                "api_key": "test-only",
            }}

            def _append_trace(row):
                TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
                with TRACE_PATH.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\\n")

            def _agent_factory(**kwargs):
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
                    agent = AIAgent(**kwargs)

                class _Transport:
                    def build_kwargs(self, *, model, messages, tools=None, max_tokens=None, **_kwargs):
                        return {{"model": model, "messages": messages, "tools": tools}}

                    def preflight_kwargs(self, api_kwargs, **_kwargs):
                        return api_kwargs

                    def validate_response(self, _response):
                        return True

                    def normalize_response(self, response, **_kwargs):
                        return response.normalized_message

                original_run = agent.run_conversation

                def _recording_run(prompt, **run_kwargs):
                    history = deepcopy(run_kwargs.get("conversation_history") or [])
                    history_text = json.dumps(history, sort_keys=True, ensure_ascii=False)
                    is_revision = REVISION_INSTRUCTIONS in prompt
                    agent._solution_modeling_response = REVISED_TASK_CARD if is_revision else INITIAL_TASK_CARD
                    _append_trace({{
                        "sessionId": agent.session_id,
                        "promptContainsTaskInput": TASK_INPUT in prompt,
                        "promptContainsRevisionInstructions": is_revision,
                        "historyCount": len(history),
                        "historyContainsTaskInput": TASK_INPUT in history_text,
                    }})
                    return original_run(prompt, **run_kwargs)

                def _response_call(*_args, **_kwargs):
                    content = json.dumps(
                        agent._solution_modeling_response,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
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

                agent.run_conversation = _recording_run
                agent._get_transport = lambda: _Transport()
                agent._interruptible_api_call = _response_call
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

            def _resolve(self, selector, request):
                if selector != "canon-hermes-profile":
                    raise RuntimeError("J05 bootstrap received an unexpected host provider")
                if request.workflow.get("workflowId") == "solution-modeling-workflow":
                    host = CanonProfileHostAdapter(
                        profiles_root=PROFILES_ROOT,
                        capabilities=set(request.capabilities),
                        checkpoint_backend=request.store.checkpoint_backend,
                        artifact_backend=request.store.artifact_backend,
                        agent_factory=_agent_factory,
                    )
                else:
                    host = FakeHostAdapter(
                        capabilities=set(request.capabilities),
                        checkpoint_backend=request.store.checkpoint_backend,
                        artifact_backend=request.store.artifact_backend,
                    )
                host.journal = request.store.journal
                return host

            HostAdapterProviderRegistry.resolve = _resolve
            from canon.cli import main
            raise SystemExit(main(sys.argv[1:]))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    bootstrap.chmod(0o700)
    return bootstrap


@pytest.fixture
def real_canon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Pre: isolated temp profile. Post: plugin children use test-owned durable roots; unload on exit. Raises: setup errors."""

    _assert_preconditions()
    profile_root = tmp_path / "hermes-home"
    for profile_dir in (
        profile_root,
        profile_root / "profiles" / "j05-test-profile",
    ):
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text(
            "model:\n  provider: openai-codex\n  default: gpt-5.6-sol\nagent:\n  max_turns: 3\n",
            encoding="utf-8",
        )
        (profile_dir / "SOUL.md").write_text(
            "isolated solution-modeling test profile\n",
            encoding="utf-8",
        )
    execution_root = tmp_path / "execution"
    execution_root.mkdir()
    bootstrap = _write_bootstrap(tmp_path)
    manager = PluginManager(scope_key=str(profile_root))
    plugin_dir = HERMES_ROOT / "plugins" / "canon"
    manifest = parse_manifest_file(plugin_dir / "plugin.yaml", plugin_dir, "bundled", "")
    assert manifest is not None
    while True:
        try:
            process_registry.completion_queue.get_nowait()
        except queue.Empty:
            break
    manager._load_plugin(manifest)
    monkeypatch.setenv("HERMES_HOME", str(profile_root))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    for tool_name in ("canon_run", "canon_checkpoint", "canon_inspect"):
        entry = registry.get_entry(tool_name, scope=manager.scope_key)
        assert entry is not None
        handle = inspect.getclosurevars(entry.handler).globals["handle_command"]
        monkeypatch.setitem(handle.__globals__, "_canon_command_prefix", lambda: [str(CANON_VENV), str(bootstrap)])
        monkeypatch.setitem(handle.__globals__, "get_hermes_home", lambda: profile_root)
        monkeypatch.setitem(handle.__globals__, "get_default_hermes_root", lambda: profile_root)
    try:
        yield {
            "manager": manager,
            "scope": manager.scope_key,
            "profile": profile_root,
            "execution": execution_root,
            "agent_trace": tmp_path / "solution-modeling-agent-trace.jsonl",
        }
    finally:
        manager.unload()
        for session in process_registry.list_sessions():
            if session.get("status") == "running":
                process_registry.kill_process(session["session_id"])
        assert not any(session.get("status") == "running" for session in process_registry.list_sessions())


def _workflow_input_file(execution: Path, value: dict[str, Any]) -> str:
    """Pre: execution is test-owned. Post: writes exact input for CLI; returns path. Raises: OSError."""
    path = execution / "workflow-input.json"
    _write_json(path, value)
    return str(path)


def _dispatch(scope: str, tool: str, args: dict[str, Any], *, task_id: str | None = None) -> dict[str, Any]:
    raw = registry.dispatch(tool, args, scope=scope, task_id=task_id)
    if isinstance(raw, dict):
        return raw
    return json.loads(raw)


def _fresh_rows(db_path: Path, run_id: str) -> list[dict[str, Any]]:
    """Read durable rows through a new sqlite3 connection and a new journal instance."""

    assert db_path.is_file(), f"journal was not created: {db_path}"
    with sqlite3.connect(db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM execution_journal WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        assert count >= 0
    return _canon_durable_probe("journal", db_path, run_id)


def _journal_oracle(
    result: dict[str, Any],
    db_path: Path,
    *,
    expected_kinds: set[str],
) -> tuple[list[dict[str, Any]], Any]:
    """Correlate one framed result to exact fresh journal rows and projection sequences."""

    run_id = result.get("runId")
    thread_id = result.get("threadId")
    assert isinstance(run_id, str) and run_id
    assert isinstance(thread_id, str) and thread_id
    rows = _fresh_rows(db_path, run_id)
    assert rows, "missing correlated durable journal evidence"
    assert all(row["runId"] == run_id and row["threadId"] == thread_id for row in rows)
    kinds = {row["eventKind"] for row in rows}
    assert expected_kinds.issubset(kinds)
    event_sequences = [row["sequence"] for row in rows]
    append_sequences = [row["appendSequence"] for row in rows]
    assert len(event_sequences) == len(set(event_sequences))
    assert event_sequences == list(range(event_sequences[0], event_sequences[0] + len(event_sequences)))
    assert append_sequences == sorted(set(append_sequences))
    projection = _canon_durable_probe("projection", db_path, run_id)
    assert projection["threadId"] == thread_id
    assert projection["eventSequence"] == event_sequences
    assert projection["appendSequence"] == append_sequences
    if isinstance(result.get("eventSequence"), list):
        assert result["eventSequence"] == event_sequences
    if isinstance(result.get("appendSequence"), list):
        assert result["appendSequence"] == append_sequences
    return rows, projection


def test_completed_launch_has_fresh_correlated_journal_projection(
    tmp_path: Path, real_canon: dict[str, Any],
) -> None:
    pack = _copy_pack(tmp_path, name="completed-pack", run_id="run-j05-completed", thread_id="thread-j05-completed")
    execution = real_canon["execution"]
    execution.mkdir(exist_ok=True)
    result = _dispatch(
        real_canon["scope"],
        "canon_run",
        {
            "workflow": str(pack / "workflow.json"),
            "workflow_input_json": _workflow_input_file(execution, {"task": "J05 completed launch"}),
            "execution_root": str(execution),
            "wait_mode": "sync-short",
            "timeout_seconds": 90,
        },
    )
    assert result.get("status") == "completed", result
    db_path = real_canon["profile"] / "runtime" / "canon" / "durable" / "journal.sqlite"
    rows, projection = _journal_oracle(
        result,
        db_path,
        expected_kinds={"run.accepted", "run.completed"},
    )
    assert projection["status"] == "completed"
    assert rows[-1]["eventKind"] == "run.completed"


def _checkpoint_payload(pack: Path, prompt: dict[str, Any], decision: dict[str, Any], request_id: str) -> dict[str, Any]:
    packaged = json.loads((pack / "run-request.json").read_text(encoding="utf-8"))
    handle = prompt["resumeHandle"]
    return {
        "schemaVersion": "checkpoint-control-request.schema.v1",
        "action": "checkpointControl",
        "mode": "continue_paused",
        "workflowRef": packaged["workflowRef"],
        "runId": handle["runId"],
        "threadId": handle["threadId"],
        "checkpointId": handle["checkpointId"],
        "nodeId": handle["nodeId"],
        "gateId": prompt["gateId"],
        "subjectRevision": prompt["subjectRevision"],
        "subjectDigest": prompt["subjectDigest"],
        "decisionAttempt": prompt["decisionAttempt"],
        "gateDecision": decision,
        "workflowInput": packaged["workflowInput"],
        "inputPublications": packaged["inputPublications"],
        "runConfiguration": packaged["runConfiguration"],
        "requestId": request_id,
    }


def _solution_modeling_checkpoint_payload(
    workflow_path: Path,
    workflow_input: dict[str, Any],
    execution_root: Path,
    prompt: dict[str, Any],
    decision: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    """Pre: Canon-valid input and prompt. Post: decision uses Canon-owned framing. Raises: Canon validation errors."""
    framing = _load_source_bound_cli_request(
        workflow_path=workflow_path,
        workflow_input=workflow_input,
        execution_root=str(execution_root),
        profile_id="default",
        require_explicit_identity=False,
        runtime_mode="durable-production",
        recursion_limit=64,
    )
    handle = prompt["resumeHandle"]
    return {
        "schemaVersion": "checkpoint-control-request.schema.v1",
        "action": "checkpointControl",
        "mode": "continue_paused",
        "workflowRef": framing["workflowRef"],
        "runId": handle["runId"],
        "threadId": handle["threadId"],
        "checkpointId": handle["checkpointId"],
        "nodeId": handle["nodeId"],
        "gateId": prompt["gateId"],
        "subjectRevision": prompt["subjectRevision"],
        "subjectDigest": prompt["subjectDigest"],
        "decisionAttempt": prompt["decisionAttempt"],
        "gateDecision": decision,
        "workflowInput": framing["workflowInput"],
        "inputPublications": framing["inputPublications"],
        "runConfiguration": framing["runConfiguration"],
        "requestId": request_id,
    }


def _fresh_checkpoint_prompt(result: dict[str, Any], db_path: Path) -> dict[str, Any]:
    """Rebuild the next typed prompt from a fresh durable checkpoint alias read."""

    handle = result.get("resumeHandle")
    assert isinstance(handle, dict), result
    alias = _canon_durable_probe("checkpoint", db_path, handle["checkpointId"])
    return {
        "status": "paused",
        "resumeHandle": handle,
        "gateId": alias["gateId"],
        "subjectRevision": alias["subjectRevision"],
        "subjectDigest": alias["subjectDigest"],
        "decisionAttempt": alias["decisionAttempt"],
    }


def _await_completion(session_id: str) -> dict[str, Any]:
    """Pre: tracked child. Post: returns its native completion event. Raises: queue.Empty on timeout."""
    while True:
        event = process_registry.completion_queue.get(timeout=90)
        if event.get("type") == "completion" and event.get("session_id") == session_id:
            return event


def _full_child_result(session_id: str) -> dict[str, Any]:
    """Pre: child has completed. Post: decodes full native stdout, not the completion tail. Raises: JSON/assertion errors."""
    raw = registry.dispatch("process_manage", {
        "action": "log", "session_id": session_id, "offset": 0, "limit": 1048576,
    })
    logged = json.loads(raw) if isinstance(raw, str) else raw
    assert "error" not in logged, logged
    return json.loads(logged["output"])


def test_checkpoint_lifecycle_preserves_one_journal_lineage(
    tmp_path: Path, real_canon: dict[str, Any],
) -> None:
    """Pre: isolated real CLI. Post: revisions and approval preserve one durable lineage. Raises: assertion failures."""
    pack = _human_gate_pack(tmp_path)
    execution = real_canon["execution"]
    execution.mkdir(exist_ok=True)
    launch = _dispatch(
        real_canon["scope"],
        "canon_run",
        {
            "workflow": str(pack / "workflow.json"),
            "workflow_input_json": _workflow_input_file(execution, {"task": "J05 revision lifecycle"}),
            "execution_root": str(execution),
            "wait_mode": "sync-short",
            "timeout_seconds": 90,
        },
    )
    assert launch.get("status") == "paused", launch
    prompt = launch["humanGatePrompt"] if isinstance(launch.get("humanGatePrompt"), dict) else launch
    assert prompt["status"] == "paused"
    assert set(prompt["resumeHandle"]) >= {"runId", "threadId", "checkpointId", "nodeId"}
    db_path = real_canon["profile"] / "runtime" / "canon" / "durable" / "journal.sqlite"
    checkpoint_db = real_canon["profile"] / "runtime" / "canon" / "durable" / "checkpoint.sqlite"
    run_id = prompt["resumeHandle"]["runId"]
    thread_id = prompt["resumeHandle"]["threadId"]
    previous_rows = _fresh_rows(db_path, run_id)
    assert all(row["threadId"] == thread_id for row in previous_rows)
    previous_count = len(previous_rows)
    previous_digest = prompt["subjectDigest"]

    foreign = _checkpoint_payload(pack, prompt, {"action": "approve"}, "foreign")
    foreign["threadId"] = "foreign-thread"
    rejected = _dispatch(real_canon["scope"], "canon_checkpoint", {
        "workflow": str(pack / "workflow.json"),
        "subcommand": "continue-paused",
        "run_id": run_id,
        "thread_id": thread_id,
        "request_json": foreign,
        "wait_mode": "sync-real",
        "timeout_seconds": 90,
    })
    assert rejected.get("success") is False or "error" in rejected
    assert len(_fresh_rows(db_path, run_id)) == previous_count

    for index, decision in enumerate((
        {"action": "revise", "revisionInstructions": "preserve the current subject"},
        {"action": "revise", "revisionInstructions": "make the second correction"},
        {"action": "approve"},
    ), start=1):
        payload = _checkpoint_payload(pack, prompt, decision, f"j05-decision-{index}")
        accepted = _dispatch(
            real_canon["scope"],
            "canon_checkpoint",
            {
                "workflow": str(pack / "workflow.json"),
                "subcommand": "continue-paused",
                "run_id": run_id,
                "thread_id": thread_id,
                "request_json": payload,
                "wait_mode": "async-no-wait",
                "timeout_seconds": 90,
            },
            task_id=f"j05-checkpoint-{index}",
        )
        assert accepted.get("notify_on_complete") is True, accepted
        completion = _await_completion(accepted["session_id"])
        assert completion["exit_code"] == 0, completion
        child_result = _full_child_result(accepted["session_id"])
        if decision["action"] == "revise":
            assert child_result["resumeHandle"]["runId"] == run_id
            assert child_result["resumeHandle"]["threadId"] == thread_id
        else:
            assert child_result["runId"] == run_id
            assert child_result["threadId"] == thread_id
        rows = _fresh_rows(db_path, run_id)
        assert len(rows) > previous_count
        assert [row["sequence"] for row in rows] == list(range(rows[0]["sequence"], rows[0]["sequence"] + len(rows)))
        operation_kinds = [row["eventKind"] for row in rows if row.get("payload", {}).get("requestId") == payload["requestId"]]
        expected_operation_kinds = [
            "checkpoint.control.requested",
            "checkpoint.control.validated",
            "checkpoint.control.started",
            "checkpoint.control.completed",
        ]
        assert operation_kinds[:4] == expected_operation_kinds
        assert operation_kinds[4:] == ([] if decision["action"] == "revise" else ["run.completed"])
        if decision["action"] == "revise":
            prompt = _fresh_checkpoint_prompt(child_result, checkpoint_db)
            assert prompt["subjectDigest"] != previous_digest
            previous_digest = prompt["subjectDigest"]
            previous_count = len(rows)
        else:
            assert child_result.get("status") == "completed"
            assert rows[-1]["eventKind"] == "run.completed"


def test_oracle_sensitivity_rejects_schema_valid_output_without_journal_evidence(tmp_path: Path) -> None:
    run_id = "run-j05-oracle"
    thread_id = "thread-j05-oracle"
    schema_valid = {
        "schemaVersion": "canon-run-result.schema.v2",
        "status": "completed",
        "runId": run_id,
        "threadId": thread_id,
        "workflowRef": "workflow:minimal-pack",
        "artifactRefs": [],
        "result": {"ok": True},
    }
    empty_db = tmp_path / "empty-journal.sqlite"
    _canon_durable_probe("journal", empty_db, run_id)
    with pytest.raises(AssertionError, match="missing correlated durable journal evidence"):
        _journal_oracle(schema_valid, empty_db, expected_kinds={"run.accepted", "run.completed"})

    artifact_only = tmp_path / "artifact-only.sqlite"
    _canon_durable_probe("journal", artifact_only, run_id)
    (tmp_path / "artifact-only").mkdir()
    with pytest.raises(AssertionError, match="missing correlated durable journal evidence"):
        _journal_oracle({**schema_valid, "artifactRefs": ["artifact://run-j05-oracle/result"]}, artifact_only, expected_kinds={"run.completed"})


def test_solution_modeling_workflow_revise_then_approve_installed_e2e(
    tmp_path: Path, real_canon: dict[str, Any],
) -> None:
    """Pre: installed workflow and isolated host. Post: revision replays the session and approval completes its journal. Raises: assertion failures."""
    workflow_path = CANON_ROOT / "src" / "canon_workflows" / "packs" / "solution_modeling_workflow_pack" / "workflow.json"
    workflow_input = {"taskInput": SOLUTION_MODELING_TASK_INPUT}
    execution = real_canon["execution"]
    execution.mkdir(exist_ok=True)
    launch = _dispatch(
        real_canon["scope"],
        "canon_run",
        {
            "workflow": str(workflow_path),
            "workflow_input_json": _workflow_input_file(execution, workflow_input),
            "execution_root": str(execution),
            "wait_mode": "sync-short",
            "timeout_seconds": 90,
        },
    )
    assert launch.get("status") == "paused", launch
    initial_prompt = launch.get("humanGatePrompt") if isinstance(launch.get("humanGatePrompt"), dict) else launch
    assert isinstance(initial_prompt, dict)
    assert initial_prompt["status"] == "paused"
    assert initial_prompt["gateId"] == "approve-task-card"
    assert initial_prompt["actions"] == ["approve", "revise", "abort"]
    assert SOLUTION_MODELING_TASK_INPUT in initial_prompt["cardBody"] or "S7_INITIAL_CARD" in initial_prompt["cardBody"]
    assert "S7_INITIAL_CARD" in initial_prompt["cardBody"]
    initial_handle = initial_prompt["resumeHandle"]
    run_id = initial_handle["runId"]
    thread_id = initial_handle["threadId"]
    assert initial_prompt["workflowRef"] == "workflow:solution-modeling-workflow"
    assert initial_prompt["subjectRevision"] == 1
    assert initial_prompt["subjectDigest"]
    assert initial_prompt["cardBodyDigest"]

    db_path = real_canon["profile"] / "runtime" / "canon" / "durable" / "journal.sqlite"
    checkpoint_db = real_canon["profile"] / "runtime" / "canon" / "durable" / "checkpoint.sqlite"
    initial_alias = _canon_durable_probe("checkpoint", checkpoint_db, initial_handle["checkpointId"])
    assert initial_alias["checkpointIdentityClass"] == "public-gate-resume-alias"
    initial_session_ref = initial_alias["agentSessionRef"]
    initial_checkpoint_ref = initial_alias["agentCheckpointRef"]
    assert initial_session_ref
    assert initial_checkpoint_ref
    trace_path = real_canon["agent_trace"]
    initial_trace = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert len(initial_trace) == 1
    initial_private_session_id = initial_trace[0]["sessionId"]
    assert isinstance(initial_private_session_id, str)
    assert initial_private_session_id.startswith("canon-")
    assert initial_trace == [
        {
            "historyContainsTaskInput": False,
            "historyCount": 0,
            "promptContainsRevisionInstructions": False,
            "promptContainsTaskInput": True,
            "sessionId": initial_private_session_id,
        }
    ]

    observed = _dispatch(
        real_canon["scope"],
        "canon_inspect",
        {"surface": "report", "run_id": run_id},
    )
    initial_report = observed
    assert initial_report["status"] == "paused"
    assert initial_report["runId"] == run_id
    assert initial_report["threadId"] == thread_id
    assert initial_report["workflowRef"] == "workflow:solution-modeling-workflow"
    artifact_root = real_canon["profile"] / "runtime" / "canon" / "durable" / "artifacts"
    initial_artifact_refs = initial_report["artifactRefs"]
    assert initial_artifact_refs
    assert all((artifact_root / ref / "content.json").is_file() for ref in initial_artifact_refs)
    assert json.loads((artifact_root / initial_artifact_refs[0] / "content.json").read_text(encoding="utf-8")) == SOLUTION_MODELING_TASK_INPUT
    assert initial_report["pendingHumanGate"]["gateId"] == "approve-task-card"
    for _ in range(2):
        inspected = _dispatch(
            real_canon["scope"],
            "canon_inspect",
            {"surface": "inspect", "run_id": run_id},
        )
        assert inspected["runId"] == run_id, inspected

    revise_payload = _solution_modeling_checkpoint_payload(
        workflow_path,
        workflow_input,
        execution,
        initial_prompt,
        {"action": "revise", "revisionInstructions": SOLUTION_MODELING_REVISION_INSTRUCTIONS},
        "s7-solution-modeling-revise",
    )
    revise_accepted = _dispatch(
        real_canon["scope"],
        "canon_checkpoint",
        {
            "workflow": str(workflow_path),
            "subcommand": "continue-paused",
            "run_id": run_id,
            "thread_id": thread_id,
            "request_json": revise_payload,
            "wait_mode": "async-no-wait",
            "timeout_seconds": 90,
        },
        task_id="s7-solution-modeling-revise",
    )
    assert revise_accepted.get("notify_on_complete") is True, revise_accepted
    revise_completion = _await_completion(revise_accepted["session_id"])
    assert revise_completion["exit_code"] == 0, revise_completion.get("output", revise_completion)
    revised_result = _full_child_result(revise_accepted["session_id"])
    assert revised_result["status"] == "paused"
    revised_prompt = revised_result["humanGatePrompt"]
    assert "gateId" in revised_prompt, revised_result
    assert revised_prompt["workflowRef"] == "workflow:solution-modeling-workflow"
    assert revised_prompt["resumeHandle"]["runId"] == run_id
    assert revised_prompt["resumeHandle"]["threadId"] == thread_id
    assert revised_prompt["gateId"] == "approve-task-card"
    assert revised_prompt["subjectRevision"] == initial_prompt["subjectRevision"] + 1
    assert revised_prompt["subjectDigest"] != initial_prompt["subjectDigest"]
    assert revised_prompt["cardBody"] != initial_prompt["cardBody"]
    assert "S7_REVISED_CARD" in revised_prompt["cardBody"]
    assert all(filename in revised_prompt["cardBody"] for filename in ("README.md", "pyproject.toml", "workflow.json"))

    revised_alias = _canon_durable_probe("checkpoint", checkpoint_db, revised_prompt["resumeHandle"]["checkpointId"])
    assert revised_alias["agentSessionRef"] == initial_session_ref
    assert revised_alias["agentCheckpointRef"]
    assert revised_alias["agentCheckpointRef"] != initial_checkpoint_ref
    trace_rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert len(trace_rows) == 2
    assert trace_rows[1]["sessionId"] == initial_private_session_id
    assert trace_rows[1]["promptContainsTaskInput"] is False
    assert trace_rows[1]["promptContainsRevisionInstructions"] is True
    assert trace_rows[1]["historyCount"] > 0
    assert trace_rows[1]["historyContainsTaskInput"] is True

    observed = _dispatch(
        real_canon["scope"],
        "canon_inspect",
        {"surface": "report", "run_id": run_id},
    )
    revised_report = observed
    assert revised_report["status"] == "paused"
    assert revised_report["runId"] == run_id
    assert revised_report["threadId"] == thread_id
    assert revised_report["workflowRef"] == "workflow:solution-modeling-workflow"
    revised_artifact_refs = revised_report["artifactRefs"]
    assert set(initial_artifact_refs).issubset(revised_artifact_refs)
    assert all((artifact_root / ref / "content.json").is_file() for ref in revised_artifact_refs)
    assert revised_report["pendingHumanGate"]["gateId"] == "approve-task-card"
    assert len(revised_report["gateDecisions"]) == 1
    assert revised_report["gateDecisions"][0]["decision"] == "revise"
    inspected = _dispatch(
        real_canon["scope"],
        "canon_inspect",
        {"surface": "inspect", "run_id": run_id},
    )
    assert inspected["runId"] == run_id, inspected

    approve_payload = _solution_modeling_checkpoint_payload(
        workflow_path,
        workflow_input,
        execution,
        revised_prompt,
        {"action": "approve"},
        "s7-solution-modeling-approve",
    )
    approve_accepted = _dispatch(
        real_canon["scope"],
        "canon_checkpoint",
        {
            "workflow": str(workflow_path),
            "subcommand": "continue-paused",
            "run_id": run_id,
            "thread_id": thread_id,
            "request_json": approve_payload,
            "wait_mode": "async-no-wait",
            "timeout_seconds": 90,
        },
        task_id="s7-solution-modeling-approve",
    )
    assert approve_accepted.get("notify_on_complete") is True, approve_accepted
    approve_completion = _await_completion(approve_accepted["session_id"])
    assert approve_completion["exit_code"] == 0, approve_completion.get("output", approve_completion)
    final_result = _full_child_result(approve_accepted["session_id"])
    assert final_result["runId"] == run_id
    assert final_result["threadId"] == thread_id
    assert final_result["status"] == "completed"
    assert final_result["result"] == SOLUTION_MODELING_REVISED_TASK_CARD
    final_result_artifact_refs = final_result["artifactRefs"]
    assert all((artifact_root / ref / "content.json").is_file() for ref in final_result_artifact_refs)

    observed = _dispatch(
        real_canon["scope"],
        "canon_inspect",
        {"surface": "report", "run_id": run_id},
    )
    final_report = observed
    assert final_report["status"] == "completed"
    assert final_report["runId"] == run_id
    assert final_report["threadId"] == thread_id
    assert final_report["workflowRef"] == "workflow:solution-modeling-workflow"
    final_report_artifact_refs = final_report["artifactRefs"]
    assert set(revised_artifact_refs).issubset(final_report_artifact_refs)
    assert set(final_result_artifact_refs).issubset(final_report_artifact_refs)
    assert all((artifact_root / ref / "content.json").is_file() for ref in final_report_artifact_refs)
    assert final_report["pendingHumanGate"] is None
    assert [decision["decision"] for decision in final_report["gateDecisions"]] == ["revise", "accept"]

    rows, projection = _journal_oracle(
        final_result,
        db_path,
        expected_kinds={
            "run.accepted",
            "wait.issued",
            "gate.decision",
            "phase.execution.completed",
            "backend.session.linked",
            "checkpoint.control.completed",
            "run.completed",
        },
    )
    assert projection["status"] == "completed"
    assert rows[-1]["eventKind"] == "run.completed"
    assert rows[-1]["nodeId"] == "approved"
    phase_completions = [row for row in rows if row["eventKind"] == "phase.execution.completed"]
    assert len(phase_completions) == 2
    assert all(row["nodeId"] == "disambiguate-task" for row in phase_completions)
    backend_rows = [row for row in rows if row["eventKind"] == "backend.session.linked"]
    backend_session_refs = {row["payload"]["sessionRef"] for row in backend_rows}
    assert len(backend_session_refs) == 1
    backend_session_ref = next(iter(backend_session_refs))
    assert isinstance(backend_session_ref, str) and backend_session_ref
    assert backend_session_ref != initial_session_ref
    assert [row["payload"]["continuationMode"] for row in backend_rows] == ["fresh", "replayed"]
    assert backend_rows[0]["payload"]["historyCount"] == 0
    assert backend_rows[1]["payload"]["historyCount"] > 0
    accept_rows = [
        row for row in rows
        if row["eventKind"] == "gate.decision" and row.get("payload", {}).get("decision") == "accept"
    ]
    assert len(accept_rows) == 1
    assert all(row["sequence"] < accept_rows[0]["sequence"] for row in phase_completions)
    serialized = json.dumps(
        {"initial": initial_prompt, "revised": revised_prompt, "result": final_result, "report": final_report, "rows": rows},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert "taskSpec" not in serialized
    assert "implementationPlan" not in serialized
    execution_paths = [path for path in execution.rglob("*")]
    assert not any(path.name.startswith("hello") and path.suffix == ".txt" for path in execution_paths)
    assert not any(path.name in {"taskSpec", "implementationPlan"} for path in execution_paths)
