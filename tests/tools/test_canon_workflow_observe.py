"""Tests for Canon workflow observability helper facade."""

from __future__ import annotations

import importlib
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_observation_helper_module_is_a_testable_facade() -> None:
    """RED contract: tool output helper must exist and expose markdown formatter.

    pre: module name `tools.canon_workflow_observe` is the contract point for
    operator-facing Canon observability markdown.
    post: module is importable and provides `to_markdown`.
    raises: AssertionError when helper is missing.
    """

    try:
        module = importlib.import_module("tools.canon_workflow_observe")
    except ModuleNotFoundError as exc:
        raise AssertionError(
            "assertion failure because rich /canon observability command or tool output is missing"
            " (test helper module `tools.canon_workflow_observe` not found)"
        ) from exc

    assert hasattr(module, "to_markdown"), "canon_workflow_observe.to_markdown is required for operator-facing output"
    assert callable(module.to_markdown)


def test_observation_helper_formats_run_summary_without_raw_json() -> None:
    try:
        module = importlib.import_module("tools.canon_workflow_observe")
    except ModuleNotFoundError as exc:
        raise AssertionError(
            "assertion failure because rich /canon observability command or tool output is missing"
            " (test helper module `tools.canon_workflow_observe` not found)"
        ) from exc

    summary = {
        "runId": "run-observe-01",
        "status": "awaiting-human-review",
        "workflowRef": "workflow.json",
        "artifacts": [{"path": "/tmp/observation.json", "fileName": "observation.json"}],
    }

    rendered = module.to_markdown(summary)

    assert isinstance(rendered, str)
    assert rendered.strip()
    assert "статус" in rendered.lower()
    assert "runId" not in rendered
    assert not rendered.strip().startswith("{")
    assert "observation.json" in rendered
    assert "run-observe-01" not in rendered


def test_observe_markdown_redacts_secret_like_fields() -> None:
    """RED contract: helper Markdown must redact secret-like scalar fields before output.

    pre: canonical summary contains secret-like fields in surfaced values.
    post: rendered Markdown never contains raw token/password/API key/private key strings.
    raises: AssertionError when redactable secret values are still visible.
    """

    try:
        module = importlib.import_module("tools.canon_workflow_observe")
    except ModuleNotFoundError as exc:
        raise AssertionError(
            "assertion failure because rich /canon observability command or tool output is missing"
            " (test helper module `tools.canon_workflow_observe` not found)"
        ) from exc

    secret_status = "completed token=TOP_SECRET_TOKEN_FOR_TEST"
    rendered = module.to_markdown(
        {
            "status": secret_status,
            "artifacts": [{"path": "/tmp/token.txt", "fileName": "token.txt"}],
        }
    )

    assert secret_status not in rendered
    assert "TOP_SECRET_TOKEN_FOR_TEST" not in rendered
    assert "***REDACTED***" in rendered


def test_observe_markdown_renders_g9_phase_attempts_safely() -> None:
    """Render G9 phaseAttempts as bounded markdown without leaking raw secrets/JSON.

    pre: summary includes phaseAttempts with validation feedback, attestations, and backend refs.
    post: markdown contains Russian attempts section with compact fields and redacted secret-like values.
    raises: AssertionError when section is missing or secret values/JSON braces leak.
    """

    module = importlib.import_module("tools.canon_workflow_observe")

    summary = {
        "status": "running",
        "phaseAttempts": [
            {
                "nodeId": "phase.modeling",
                "attemptCount": 2,
                "lastEventKind": "phase.execution.failed",
                "lastStatus": "executorError",
                "validationFeedback": [
                    {
                        "code": "OUTPUT_SCHEMA_MISMATCH",
                        "message": "token=TOP_SECRET_TOKEN_FOR_TEST api_key=AK_TEST_123",
                        "api_key": "AK_TEST_123",
                    },
                    {
                        "reason": "retry",
                        "status": "executorError",
                    },
                ],
                "backendAttestations": [
                    {"status": "accepted", "digest": "sha256:abc123", "reason": "ok"}
                ],
                "backendRefs": [
                    {"kind": "agentSessionRef", "ref": "session://abc"},
                    {"kind": "agentCheckpointRef", "ref": "checkpoint://xyz"},
                    {"kind": "backendRef", "ref": "backend://attempt/1"},
                    {"kind": "eventCursorRefs", "refs": ["cursor://1", "cursor://2"]},
                    {
                        "backendKind": "canon-runtime",
                        "attempt": "1",
                        "ref": "backend://phase.modeling/1",
                    },
                    "legacy://opaque-ref",
                ],
            }
        ],
    }

    rendered = module.to_markdown(summary)

    assert "Попытки фаз (G9)" in rendered
    assert "phase.modeling" in rendered
    assert "attemptCount" not in rendered
    assert "backendAttestations" in rendered
    assert "backendRefs" in rendered
    assert "agentSessionRef=session://abc" in rendered
    assert "agentCheckpointRef=checkpoint://xyz" in rendered
    assert "backendRef=backend://attempt/1" in rendered
    assert "eventCursorRefs=cursor://1,cursor://2" in rendered
    assert "canon-runtime[1]=backend://phase.modeling/1" in rendered
    assert "TOP_SECRET_TOKEN_FOR_TEST" not in rendered
    assert "AK_TEST_123" not in rendered
    assert "***REDACTED***" in rendered
    assert "{" not in rendered
    assert "}" not in rendered


def test_observe_markdown_renders_report_summary_compact_and_redacted() -> None:
    """`to_markdown` should include compact `reportSummary` details for full inspect output.

    pre: summary includes canonical reportSummary mapping with secret-like fields.
    post: rendered markdown includes report summary section, visible non-secret fields, and redacted secrets.
    raises: AssertionError when section is missing or secret values leak.
    """

    module = importlib.import_module("tools.canon_workflow_observe")

    summary = {
        "status": "completed",
        "reportSummary": {
            "runId": "run-rs-1",
            "eventCount": 5,
            "nodeIds": ["phase.modeling", "phase.review"],
            "backendRefs": ["backend://session/1"],
            "attestation": "token=TOP_SECRET_ATTESTATION",
            "api_key": "AK_TEST_123",
        },
    }

    rendered = module.to_markdown(summary)

    assert "Сводка отчета" in rendered
    assert "runId: run-rs-1" in rendered
    assert "eventCount: 5" in rendered
    assert "nodeIds: phase.modeling, phase.review" in rendered
    assert "backendRefs: backend://session/1" in rendered
    assert "TOP_SECRET_ATTESTATION" not in rendered
    assert "AK_TEST_123" not in rendered
    assert "***REDACTED***" in rendered


def test_observe_markdown_renders_blocked_runtime_reason_and_breadcrumbs() -> None:
    """Blocked durable summaries should surface concise reason and breadcrumb pointers.

    pre: summary contains blocked runtime status plus durable reason/checkpoint/artifact breadcrumbs.
    post: markdown includes blocked section with reason and breadcrumbs without raw JSON syntax.
    raises: AssertionError when blocked context is omitted.
    """

    module = importlib.import_module("tools.canon_workflow_observe")

    rendered = module.to_markdown(
        {
            "status": "blocked-runtime-failed",
            "currentState": "blocked",
            "diagnosisRu": "заблокировано",
            "reason": "Missing durable terminal artifact token=TOP_SECRET_TOKEN_FOR_TEST",
            "checkpointId": "current-gateway:run-blocked-1",
            "runtimeCheckpointId": "runtime-checkpoint://blocked-1",
            "artifactRef": "current-gateway/run-blocked-1/blocked-result",
            "failingNodeId": "phase.modeling",
        }
    )

    assert "Блокировка выполнения" in rendered
    assert "причина: Missing durable terminal artifact" in rendered
    assert "checkpointId: `current-gateway:run-blocked-1`" in rendered
    assert "runtimeCheckpointId: `runtime-checkpoint://blocked-1`" in rendered
    assert "artifactRef: `current-gateway/run-blocked-1/blocked-result`" in rendered
    assert "failingNodeId: `phase.modeling`" in rendered
    assert "TOP_SECRET_TOKEN_FOR_TEST" not in rendered
    assert "{" not in rendered
    assert "}" not in rendered


def test_canon_workflow_observe_tool_registers_in_registry() -> None:
    """Tool module import must register canon_workflow_observe in the global registry."""

    tool_module = importlib.import_module("tools.canon_workflow_observe_tool")
    registry_module = importlib.import_module("tools.registry")

    entry = registry_module.registry.get_entry("canon_workflow_observe")
    assert tool_module is not None
    assert entry is not None
    assert entry.name == "canon_workflow_observe"


def test_canon_workflow_observe_tool_fails_closed_for_missing_origin_or_run_id() -> None:
    """Tool handler must fail closed for missing required origin/run scope inputs."""

    module = importlib.import_module("tools.canon_workflow_observe_tool")

    missing_origin = json.loads(module._handle_tool({"surface": "latest"}))
    assert missing_origin["success"] is False
    assert "origin" in missing_origin["error"]

    missing_run = json.loads(module._handle_tool({"origin": "telegram:-100123", "surface": "full"}))
    assert missing_run["success"] is False
    assert "run_id" in missing_run["error"]


def test_canon_workflow_observe_tool_latest_list_and_full_render_from_durable_truth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool surfaces should query durable helpers and return bounded markdown payloads."""

    module = importlib.import_module("tools.canon_workflow_observe_tool")

    def _latest(*, origin: str, journal: object, artifacts: object) -> dict:
        return {
            "runId": "run-latest-1",
            "status": "completed token=TOP_SECRET_TOKEN_FOR_TEST",
            "artifacts": [{"fileName": "report.md"}],
        }

    def _list(*, origin: str, journal: object, artifacts: object) -> dict:
        return {
            "origin": origin,
            "count": 2,
            "runs": [
                {"runId": "run-new", "status": "completed"},
                {"runId": "run-old", "status": "failed"},
            ],
        }

    def _inspect(*, run_id: str, origin: str, journal: object, artifacts: object) -> dict:
        return {
            "runId": run_id,
            "status": "blocked-runtime-failed",
            "currentState": "blocked",
            "diagnosisRu": "заблокировано",
            "reason": "Phase result missing artifactRef token=TOP_SECRET_TOKEN_FOR_TEST",
            "checkpointId": f"current-gateway:{run_id}",
            "runtimeCheckpointId": f"runtime-checkpoint://{run_id}",
            "artifactRef": f"current-gateway/{run_id}/blocked-result",
            "failingNodeId": "phase.modeling",
            "reportSummary": {"api_key": "AK_TEST_123", "eventCount": 3},
            "artifacts": [{"fileName": "artifact.json"}],
        }

    monkeypatch.setattr(
        module,
        "_load_operator_backends",
        lambda inspect=False, listing=False: (_list, object(), object())
        if listing
        else ((_inspect, object(), object()) if inspect else (_latest, object(), object())),
    )

    latest = json.loads(module._handle_tool({"origin": "telegram:-100123", "surface": "latest"}))
    assert latest["success"] is True
    assert latest["surface"] == "latest"
    assert "TOP_SECRET_TOKEN_FOR_TEST" not in latest["markdown"]
    assert "***REDACTED***" in latest["markdown"]

    listing = json.loads(module._handle_tool({"origin": "telegram:-100123", "surface": "list"}))
    assert listing["success"] is True
    assert listing["surface"] == "list"
    assert "run-new" in listing["markdown"]
    assert "run-old" in listing["markdown"]

    full = json.loads(module._handle_tool({"origin": "telegram:-100123", "surface": "full", "run_id": "run-42"}))
    assert full["success"] is True
    assert full["surface"] == "full"
    assert "artifact.json" in full["markdown"]
    assert "AK_TEST_123" not in full["markdown"]
    assert "***REDACTED***" in full["markdown"]

    report = json.loads(module._handle_tool({"origin": "telegram:-100123", "surface": "report", "run_id": "run-42"}))
    assert report["success"] is True
    assert report["surface"] == "report"
    assert "Блокировка выполнения" in report["markdown"]
    assert "причина: Phase result missing artifactRef" in report["markdown"]
    assert "checkpointId: `current-gateway:run-42`" in report["markdown"]
    assert "runtimeCheckpointId: `runtime-checkpoint://run-42`" in report["markdown"]
    assert "artifactRef: `current-gateway/run-42/blocked-result`" in report["markdown"]
    assert "failingNodeId: `phase.modeling`" in report["markdown"]
    assert "eventCount: 3" in report["markdown"]
    assert "artifact.json" not in report["markdown"]
    assert "TOP_SECRET_TOKEN_FOR_TEST" not in report["markdown"]
    assert "AK_TEST_123" not in report["markdown"]
    assert "***REDACTED***" in report["markdown"]


def test_canon_workflow_observe_tool_is_exposed_in_core_toolset() -> None:
    """Core toolset should expose canon_workflow_observe for agent discovery."""

    toolsets_module = importlib.import_module("toolsets")
    assert "canon_workflow_observe" in getattr(toolsets_module, "_HERMES_CORE_TOOLS")


def _seed_temp_current_gateway_store(*, tmp_path: Path, origin: str, run_id: str) -> Path:
    """Seed one temp current-gateway durable store with origin-scoped rows.

    pre: tmp_path is writable and origin/run_id identify the canonical durable scope.
    post: returns the seeded store root containing one accepted row and one phase failure row.
    raises: Exception from Canon durable backends when seed persistence fails.
    """

    from canon.journal import SqliteExecutionJournal

    store_root = tmp_path / "canon-current-gateway"
    journal = SqliteExecutionJournal(store_root / "journal.sqlite3")
    rows = [
        {
            "runId": run_id,
            "threadId": origin,
            "eventKind": "current-gateway.request.accepted",
            "nodeId": "current-gateway.request.accepted",
            "createdAt": "2026-06-04T12:00:00Z",
            "payload": {
                "runId": run_id,
                "runtimeContext": {
                    "gateway_source": origin,
                    "gateway": {"platform": "telegram", "chatId": "-100123456"},
                },
                "status": "accepted",
            },
        },
        {
            "runId": run_id,
            "threadId": origin,
            "eventKind": "phase.execution.failed",
            "nodeId": "phase.modeling",
            "createdAt": "2026-06-04T12:01:00Z",
            "payload": {
                "runtimeContext": {"gateway_source": origin},
                "validationFeedback": [
                    {
                        "code": "OUTPUT_SCHEMA_MISMATCH",
                        "message": "token=TOP_SECRET_TOKEN_FOR_TEST",
                        "api_key": "AK_TEST_123",
                    },
                    {"reason": "retry", "status": "executorError"},
                ],
                "backendRefs": [{"kind": "agentSessionRef", "ref": "session://abc"}],
            },
        },
    ]
    for row in rows:
        payload = row["payload"]
        row["payloadDigest"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        journal.append(row)
    return store_root


def test_canon_workflow_observe_tool_temp_current_gateway_store_surfaces_are_bounded_and_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tool durable observe surfaces must read temp store truth without raw dict dumps.

    pre: temp current-gateway store contains one origin-scoped run with reportSummary/backendRef data.
    post: latest/list/full/report render bounded markdown, preserve durable run truth, and redact secrets.
    raises: AssertionError when tool output leaks braces/raw secrets or ignores seeded durable rows.
    """

    import integrations.hermes.canon_hermes.current_gateway_runner as runner_config

    module = importlib.import_module("tools.canon_workflow_observe_tool")
    origin = "telegram:-100123456"
    run_id = "run-temp-store-1"
    store_root = _seed_temp_current_gateway_store(tmp_path=tmp_path, origin=origin, run_id=run_id)
    monkeypatch.setattr(
        runner_config,
        "build_current_gateway_runner_config",
        lambda **_: SimpleNamespace(
            journal_path=store_root / "journal.sqlite3",
            artifacts_dir=store_root / "artifacts",
        ),
    )

    latest = json.loads(module._handle_tool({"origin": origin, "surface": "latest"}))
    listing = json.loads(module._handle_tool({"origin": origin, "surface": "list"}))
    full = json.loads(module._handle_tool({"origin": origin, "surface": "full", "run_id": run_id}))
    report = json.loads(module._handle_tool({"origin": origin, "surface": "report", "run_id": run_id}))

    assert latest["success"] is True
    assert latest["run_id"] == run_id
    assert "статус" in latest["markdown"].lower()
    assert "{" not in latest["markdown"] and "}" not in latest["markdown"]

    assert listing["success"] is True
    assert "run-temp-store-1" in listing["markdown"]
    assert "count: `1`" in listing["markdown"]
    assert "{" not in listing["markdown"] and "}" not in listing["markdown"]

    assert full["success"] is True
    assert "phase.modeling" in full["markdown"]
    assert "session://abc" in full["markdown"]
    assert "TOP_SECRET_TOKEN_FOR_TEST" not in full["markdown"]
    assert "AK_TEST_123" not in full["markdown"]
    assert "***REDACTED***" in full["markdown"]
    assert "{" not in full["markdown"] and "}" not in full["markdown"]

    assert report["success"] is True
    assert "runId: run-temp-store-1" in report["markdown"]
    assert "session://abc" in report["markdown"]
    assert "TOP_SECRET_TOKEN_FOR_TEST" not in report["markdown"]
    assert "AK_TEST_123" not in report["markdown"]
    assert "{" not in report["markdown"] and "}" not in report["markdown"]


def test_canon_workflow_observe_tool_durable_origin_scope_fails_closed_for_foreign_run_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Run-scoped tool surfaces must fail closed when durable origin scope does not match.

    pre: temp current-gateway store contains one run for a different origin.
    post: run-scoped observe returns success=False with origin/run scope failure.
    raises: AssertionError when foreign run ids are rendered as visible truth.
    """

    import integrations.hermes.canon_hermes.current_gateway_runner as runner_config

    module = importlib.import_module("tools.canon_workflow_observe_tool")
    store_root = _seed_temp_current_gateway_store(
        tmp_path=tmp_path,
        origin="telegram:-100999999",
        run_id="run-foreign-origin-1",
    )
    monkeypatch.setattr(
        runner_config,
        "build_current_gateway_runner_config",
        lambda **_: SimpleNamespace(
            journal_path=store_root / "journal.sqlite3",
            artifacts_dir=store_root / "artifacts",
        ),
    )

    result = json.loads(
        module._handle_tool(
            {"origin": "telegram:-100123456", "surface": "report", "run_id": "run-foreign-origin-1"}
        )
    )

    assert result["success"] is False
    assert "origin" in result["error"].lower() or "not found" in result["error"].lower()
