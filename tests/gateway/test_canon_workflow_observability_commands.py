"""Gateway /canon observability command behavior for Russian markdown and tool-backed status surfaces."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from tools.canon_workflow_command import handle_gateway_canon_command
from gateway.platforms.base import MessageEvent, Platform
from gateway.session import SessionSource


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="-100123456",
        user_name="operator",
        chat_type="group",
        thread_id="777",
    )


def _make_event(text: str, *, thread_id: str = "777", message_id: str = "m1") -> MessageEvent:
    source = _make_source()
    source = SessionSource(
        platform=source.platform,
        user_id=source.user_id,
        chat_id=source.chat_id,
        user_name=source.user_name,
        chat_type=source.chat_type,
        thread_id=thread_id,
    )
    return MessageEvent(text=text, source=source, message_id=message_id)


def test_canon_observability_subcommands_produce_markdown_without_raw_json(monkeypatch) -> None:
    """RED: these observability subcommands are required for S6.

    pre: `/canon` receives the command.
    post: output is not a fallback/unsupported gate; it must be operator-facing summary text.
    raises: AssertionError when surface is still missing.
    """

    import tools.canon_workflow_command as canon_cmd

    def _fake_loader(*, run_id: str, source: object) -> dict:
        return {
            "runId": run_id,
            "status": "completed",
            "timeline": ["queued", "completed"],
            "artifacts": ["artifact-a"],
            "events": ["event-a"],
            "report": {"status": "ok"},
            "control": {"stopped": False},
        }

    monkeypatch.setattr(canon_cmd, "_load_inspect_summary", _fake_loader)
    monkeypatch.setattr(
        canon_cmd,
        "_load_stream_events_summary",
        lambda *, run_id, source, after_sequence, limit: {
            "run": {"runId": run_id, "status": "completed"},
            "events": [{"sequence": 1, "eventKind": "node.completed", "nodeId": "phase.modeling"}],
            "nextCursor": None,
            "hasMore": False,
        },
    )

    for subcommand in ["full", "timeline", "artifacts", "events", "report", "control"]:
        result = handle_gateway_canon_command(_make_event(f"/canon {subcommand} run-obs-1"))

        assert "unsupported" not in result.lower()
        assert "failed closed" not in result.lower()
        assert result.strip()
        assert "`" in result
        assert "### Поверхность:" in result


def test_canon_list_without_run_id_uses_origin_scoped_durable_listing(monkeypatch) -> None:
    """`/canon list` must be origin-scoped and not require run-id input.

    pre: list loader returns durable origin-scoped payload.
    post: renderer includes count and run statuses in markdown without raw JSON braces.
    raises: AssertionError when command still requires run id or dumps raw json.
    """

    import tools.canon_workflow_command as canon_cmd

    def _fake_list_loader(*, source: object) -> dict:
        return {
            "origin": "telegram:-100123456",
            "count": 2,
            "runs": [
                {"runId": "run-origin-a-new", "status": "completed"},
                {"runId": "run-origin-a-old", "status": "failed"},
            ],
        }

    monkeypatch.setattr(canon_cmd, "_load_list_summary", _fake_list_loader)
    result = handle_gateway_canon_command(_make_event("/canon list"))

    assert "failed closed" not in result.lower()
    assert "run-origin-a-new" in result
    assert "run-origin-a-old" in result
    assert "Видимых запусков: `2`" in result
    assert "{" not in result and "}" not in result


def test_canon_list_fails_closed_when_durable_lookup_errors(monkeypatch) -> None:
    """`/canon list` must fail closed on durable list lookup errors."""

    import tools.canon_workflow_command as canon_cmd

    def _broken_list_loader(*, source: object) -> dict:
        raise RuntimeError("durable db unavailable")

    monkeypatch.setattr(canon_cmd, "_load_list_summary", _broken_list_loader)
    result = handle_gateway_canon_command(_make_event("/canon list"))

    assert "failed closed" in result.lower()
    assert "durable db unavailable" in result


def test_canon_observability_subcommands_require_topic_scoped_inputs_for_full_snapshot() -> None:
    """RED: observability command contract must validate scope in operator inputs.

    pre: command payload is missing explicit scope.
    post: handler returns fail-closed guidance rather than crashing or pretending success.
    raises: AssertionError when scope gate and structured response are absent.
    """

    result = handle_gateway_canon_command(_make_event("/canon timeline"))

    assert "failed closed" in result.lower(), (
        "assertion failure because rich /canon observability command or tool output is missing"
    )
    assert "usage" in result.lower()


def test_canon_observability_query_run_scope_fails_closed_when_durable_lookup_errors(monkeypatch) -> None:
    """Fail-closed behavior for run-scoped observability lookup errors.

    pre: durable lookup helper raises on run access failure.
    post: `/canon timeline <run-id>` returns an explicit fail-closed operator message.
    raises: AssertionError when success-shaped fallback is rendered.
    """

    import tools.canon_workflow_command as canon_cmd

    def _broken_loader(*, run_id: str, source: object) -> dict:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(canon_cmd, "_load_inspect_summary", _broken_loader)
    result = handle_gateway_canon_command(_make_event("/canon timeline run-obs-1"))

    assert "failed closed" in result.lower()
    assert "db unavailable" in result


def test_canon_observability_output_redacts_secret_like_fields(monkeypatch) -> None:
    """Operator `/canon inspect` output redacts secret-like durable values.

    pre: durable inspect summary contains a secret-like status value.
    post: rendered operator output keeps the run visible but never exposes the raw secret-like value.
    raises: AssertionError when the raw value leaks through the command renderer.
    """

    import tools.canon_workflow_command as canon_cmd

    secret_status = "TOP_SECRET_STATUS_VALUE"

    def _secret_loader(*, run_id: str, source: object) -> dict:
        return {
            "runId": run_id,
            "status": secret_status,
        }

    monkeypatch.setattr(canon_cmd, "_load_inspect_summary", _secret_loader)
    result = handle_gateway_canon_command(_make_event("/canon inspect run-obs-1"))

    assert "run-obs-1" in result
    assert secret_status not in result
    assert "***REDACTED***" in result


def test_canon_observability_output_is_localized_to_russian(monkeypatch) -> None:
    """Russian operator markdown for observability commands.

    pre: durable summary exists and contains surface payload.
    post: returned command text contains concise Russian section labels.
    raises: AssertionError when English section labels remain visible in markdown.
    """

    import tools.canon_workflow_command as canon_cmd

    def _fake_loader(*, run_id: str, source: object) -> dict:
        return {
            "runId": run_id,
            "status": "completed",
            "timeline": ["queued", "running", "completed"],
        }

    monkeypatch.setattr(canon_cmd, "_load_inspect_summary", _fake_loader)
    result = handle_gateway_canon_command(_make_event("/canon timeline run-obs-1"))

    assert "### Поверхность: Таймлайн" in result
    assert "- Команда: `/canon timeline run-obs-1`" in result
    assert "- Область запуска: `run-obs-1`" in result
    assert "- Таймлайн:" in result
    assert "- Command" not in result
    assert "Run scope" not in result


def test_canon_full_command_surfaces_phase_attempts_g9_fields(monkeypatch) -> None:
    """`/canon full` should pass phaseAttempts through observe markdown safely.

    pre: inspect loader returns G9 phaseAttempts payload.
    post: command response includes attempts section/fields and redacts secret-like values.
    raises: AssertionError when full command omits section or leaks raw values.
    """

    import tools.canon_workflow_command as canon_cmd

    def _loader(*, run_id: str, source: object) -> dict:
        return {
            "runId": run_id,
            "status": "running",
            "phaseAttempts": [
                {
                    "nodeId": "phase.modeling",
                    "attemptCount": 3,
                    "lastEventKind": "phase.execution.failed",
                    "lastStatus": "executorError token=TOP_SECRET_TOKEN_FOR_TEST",
                    "validationFeedback": [
                        {"message": "api_key=AK_TEST_123", "code": "OUTPUT_SCHEMA_MISMATCH"},
                        {"reason": "retry", "status": "executorError"},
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

    monkeypatch.setattr(canon_cmd, "_load_inspect_summary", _loader)
    result = handle_gateway_canon_command(_make_event("/canon full run-obs-g9"))

    assert "Попытки фаз (G9)" in result
    assert "phase.modeling" in result
    assert "backendAttestations" in result
    assert "backendRefs" in result
    assert "agentSessionRef=session://abc" in result
    assert "agentCheckpointRef=checkpoint://xyz" in result
    assert "backendRef=backend://attempt/1" in result
    assert "eventCursorRefs=cursor://1,cursor://2" in result
    assert "canon-runtime[1]=backend://phase.modeling/1" in result
    assert "TOP_SECRET_TOKEN_FOR_TEST" not in result
    assert "AK_TEST_123" not in result
    assert "***REDACTED***" in result


def test_canon_report_prefers_report_summary_and_redacts_secret_like_fields(monkeypatch) -> None:
    """`/canon report` must bind to observe `reportSummary` and redact secret-like values.

    pre: inspect loader returns both reportSummary (authoritative) and legacy report fields.
    post: renderer surfaces reportSummary values, avoids legacy report leak paths, and redacts secret-like values.
    raises: AssertionError when `<не опубликованно>` appears despite reportSummary or secret leaks occur.
    """

    import tools.canon_workflow_command as canon_cmd

    secret_attestation = "token=TOP_SECRET_ATTESTATION"

    def _loader(*, run_id: str, source: object) -> dict:
        return {
            "runId": run_id,
            "status": "completed",
            "reportSummary": {
                "runId": run_id,
                "eventCount": 12,
                "nodeIds": ["phase.modeling", "phase.review"],
                "backendRefs": ["backend://session/1"],
                "attestation": secret_attestation,
                "api_key": "AK_TEST_123",
            },
            "report": {
                "runId": "legacy-report-run",
                "eventCount": 999,
            },
        }

    monkeypatch.setattr(canon_cmd, "_load_inspect_summary", _loader)
    result = handle_gateway_canon_command(_make_event("/canon report run-report-summary-1"))

    assert "phase.modeling" in result
    assert "phase.review" in result
    assert "backend://session/1" in result
    assert "eventCount: 12" in result
    assert "legacy-report-run" not in result
    assert "eventCount: 999" not in result
    assert "<не опубликованно>" not in result
    assert "TOP_SECRET_ATTESTATION" not in result
    assert "AK_TEST_123" not in result
    assert "***REDACTED***" in result


def test_canon_events_uses_cursor_stream_arguments_and_renders_cursor_window(monkeypatch) -> None:
    """`/canon events` must parse cursor args and render Canon streamWorkflowEvents window.

    pre: stream loader is available and inspect fallback should not be used for events.
    post: handler forwards afterSequence/limit to stream loader and renders nextCursor/hasMore.
    raises: AssertionError when events command ignores cursor args or uses inspect-summary path.
    """

    import tools.canon_workflow_command as canon_cmd

    observed: dict[str, object] = {}

    def _inspect_should_not_run(*, run_id: str, source: object) -> dict:
        raise AssertionError("events command must not use inspect summary when cursor args are supplied")

    def _stream_loader(*, run_id: str, source: object, after_sequence: int | None, limit: int | None) -> dict:
        observed["run_id"] = run_id
        observed["after_sequence"] = after_sequence
        observed["limit"] = limit
        return {
            "run": {"runId": run_id, "status": "completed"},
            "events": [
                {"sequence": 8, "eventKind": "node.completed", "nodeId": "phase.modeling"},
                {"sequence": 9, "eventKind": "run.completed", "nodeId": "terminal"},
            ],
            "nextCursor": 9,
            "hasMore": True,
        }

    monkeypatch.setattr(canon_cmd, "_load_inspect_summary", _inspect_should_not_run)
    monkeypatch.setattr(canon_cmd, "_load_stream_events_summary", _stream_loader)

    result = handle_gateway_canon_command(
        _make_event("/canon events run-cursor-1 --after-sequence 7 --limit 2")
    )

    assert observed == {"run_id": "run-cursor-1", "after_sequence": 7, "limit": 2}
    assert "### Поверхность: События" in result
    assert "afterSequence=7, limit=2" in result
    assert "nextCursor=9, hasMore=True" in result
    assert "seq=8; kind=node.completed; node=phase.modeling" in result
    assert "seq=9; kind=run.completed; node=terminal" in result


def test_canon_events_fails_closed_on_invalid_cursor_argument_value() -> None:
    """`/canon events` must fail closed for malformed cursor args."""

    result = handle_gateway_canon_command(_make_event("/canon events run-cursor-1 --limit not-a-number"))

    assert "failed closed" in result.lower()
    assert "--limit must be a non-negative integer" in result


def _seed_temp_current_gateway_store(*, tmp_path: Path, origin: str, run_id: str) -> Path:
    """Seed one temp durable current-gateway store for `/canon` observability tests.

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


def test_canon_temp_current_gateway_store_list_inspect_and_report_share_durable_truth(
    monkeypatch, tmp_path: Path
) -> None:
    """`/canon` observe surfaces must read one temp durable store without raw dict dumps.

    pre: temp current-gateway store contains one origin-scoped run with reportSummary/backendRef data.
    post: list/inspect/report reflect the same durable truth, redact secrets, and avoid raw braces.
    raises: AssertionError when command output leaks dict dumps or diverges from the durable rows.
    """

    import integrations.hermes.canon_hermes.current_gateway_runner as runner_config

    origin = "telegram:-100123456:777"
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

    listing = handle_gateway_canon_command(_make_event("/canon list", thread_id="777"))
    inspect_result = handle_gateway_canon_command(_make_event(f"/canon inspect {run_id}", thread_id="777"))
    report = handle_gateway_canon_command(_make_event(f"/canon report {run_id}", thread_id="777"))

    assert "run-temp-store-1" in listing
    assert "Видимых запусков: `1`" in listing
    assert "{" not in listing and "}" not in listing

    assert f"`/canon inspect {run_id}` run `{run_id}` status `running`" in inspect_result

    assert "phase.modeling" in report
    assert "session://abc" in report
    assert "TOP_SECRET_TOKEN_FOR_TEST" not in report
    assert "AK_TEST_123" not in report
    assert "{" not in report and "}" not in report


def test_canon_thread_scoped_origin_list_inspect_and_report_use_topic_identity(
    monkeypatch, tmp_path: Path
) -> None:
    """Topic-scoped slash reads must use thread-aware durable origin identity.

    pre: temp current-gateway store contains one run for `telegram:<chat>:<thread>`.
    post: list/inspect/report from the same thread see the run and a different thread fails closed.
    raises: AssertionError when slash reads collapse topic origin to chat scope.
    """

    import integrations.hermes.canon_hermes.current_gateway_runner as runner_config

    origin = "telegram:-100123456:777"
    run_id = "run-thread-origin-1"
    store_root = _seed_temp_current_gateway_store(tmp_path=tmp_path, origin=origin, run_id=run_id)
    monkeypatch.setattr(
        runner_config,
        "build_current_gateway_runner_config",
        lambda **_: SimpleNamespace(
            journal_path=store_root / "journal.sqlite3",
            artifacts_dir=store_root / "artifacts",
        ),
    )

    listing = handle_gateway_canon_command(_make_event("/canon list", thread_id="777"))
    inspect_result = handle_gateway_canon_command(_make_event(f"/canon inspect {run_id}", thread_id="777"))
    report = handle_gateway_canon_command(_make_event(f"/canon report {run_id}", thread_id="777"))
    foreign_report = handle_gateway_canon_command(_make_event(f"/canon report {run_id}", thread_id="888"))

    assert run_id in listing
    assert "Видимых запусков: `1`" in listing

    assert f"`/canon inspect {run_id}` run `{run_id}` status `running`" in inspect_result

    assert "phase.modeling" in report
    assert "session://abc" in report
    assert "TOP_SECRET_TOKEN_FOR_TEST" not in report
    assert "AK_TEST_123" not in report

    assert "failed closed" in foreign_report.lower()
    assert "not found" in foreign_report.lower() or "origin" in foreign_report.lower()


def test_canon_durable_origin_scope_fails_closed_for_foreign_run_id(monkeypatch, tmp_path: Path) -> None:
    """`/canon` run-scoped observe surfaces must fail closed outside the durable origin scope.

    pre: temp current-gateway store contains one run for a different origin.
    post: report surface returns explicit fail-closed operator text.
    raises: AssertionError when a foreign-origin run becomes visible from this operator source.
    """

    import integrations.hermes.canon_hermes.current_gateway_runner as runner_config

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

    result = handle_gateway_canon_command(_make_event("/canon report run-foreign-origin-1", thread_id="777"))

    assert "failed closed" in result.lower()
    assert "origin" in result.lower() or "not found" in result.lower()
