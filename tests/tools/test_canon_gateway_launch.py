"""Tests for the local-only Canon gateway CLI-run request surface."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import tools.canon_gateway_launch as launch_tool


def test_canon_gateway_launch_schema_exposes_only_workflow_neutral_fields() -> None:
    """The public tool schema must not expose workflow-specific launch knobs.

    pre: the canon_gateway_launch tool is registered for general Hermes use.
    post: only workflow-neutral workflow/chat/thread/inputs/timeout/wait-mode fields remain.
    raises: AssertionError when legacy workflow-specific fields remain public.
    """

    properties = launch_tool.CANON_GATEWAY_LAUNCH_SCHEMA["parameters"]["properties"]

    assert set(properties) == {"workflow_name", "chat_id", "thread_id", "inputs", "timeout_seconds", "wait_mode"}
    assert launch_tool.CANON_GATEWAY_LAUNCH_SCHEMA["parameters"]["required"] == [
        "workflow_name",
        "inputs",
    ]
    assert properties["timeout_seconds"]["maximum"] == 600
    timeout_description = properties["timeout_seconds"]["description"]
    assert "sync-short" in timeout_description
    assert "90" in timeout_description
    assert "sync-real" in timeout_description
    assert "600" in timeout_description
    assert properties["wait_mode"]["enum"] == ["sync-short", "sync-real", "async-no-wait"]
    assert "request_json" not in properties


def test_build_gateway_local_launch_request_emits_inputs_envelope_for_dm_launch() -> None:
    """Builder must emit the workflow-neutral inputs transport envelope.

    pre: caller provides a non-empty workflow plus workflow-level inputs.
    post: payload carries workflow selector and inputs object without the internal request-json path.
    raises: AssertionError when the builder emits legacy action/api fields or leaks requestJson.
    """

    payload = launch_tool.build_gateway_local_launch_request(
        workflow_name="solution-modeling",
        chat_id="-100123456",
        inputs={"request": "keep me public"},
    )

    assert payload["api"] == "canon_gateway_cli_run.v1"
    assert payload["action"] == "run"
    assert payload["workflow"] == "solution-modeling"
    assert payload["inputs"] == {"request": "keep me public"}
    assert payload["target"] == {"platform": "telegram", "chatId": "-100123456"}
    assert payload["timeoutSeconds"] == 90
    assert payload["waitMode"] == "sync-short"
    assert "requestJson" not in payload
    assert "taskText" not in payload


def test_build_gateway_local_launch_request_accepts_real_wait_timeout_up_to_600_seconds() -> None:
    """Builder must accept the real-task wait ceiling without changing the short default.

    pre: caller explicitly selects the real sync wait mode and requests the documented 600-second ceiling.
    post: payload preserves `timeoutSeconds=600` and `waitMode=sync-real`; `601` is rejected fail-closed.
    raises: AssertionError when the public wait contract still caps real waits below 600 seconds.
    """

    payload = launch_tool.build_gateway_local_launch_request(
        workflow_name="solution-modeling",
        chat_id="-100123456",
        inputs={"request": "real task"},
        timeout_seconds=600,
        wait_mode="sync-real",
    )

    assert payload["timeoutSeconds"] == 600
    assert payload["waitMode"] == "sync-real"

    with pytest.raises(ValueError, match="timeout_seconds must be between 1 and 600"):
        launch_tool.build_gateway_local_launch_request(
            workflow_name="solution-modeling",
            chat_id="-100123456",
            inputs={"request": "too long"},
            timeout_seconds=601,
            wait_mode="sync-real",
        )


def test_build_gateway_local_launch_request_sync_real_defaults_to_600_seconds() -> None:
    """Wait-mode defaults must distinguish short smoke waits from real-task waits.

    pre: caller omits `timeout_seconds` and selects either the real sync wait mode or the short sync mode.
    post: `sync-real` defaults to 600 seconds while `sync-short` continues to default to 90 seconds.
    raises: AssertionError when omitted real-task waits still inherit the short smoke timeout.
    """

    real_wait_payload = launch_tool.build_gateway_local_launch_request(
        workflow_name="solution-modeling",
        chat_id="-100123456",
        inputs={"request": "real default"},
        wait_mode="sync-real",
    )
    short_wait_payload = launch_tool.build_gateway_local_launch_request(
        workflow_name="solution-modeling",
        chat_id="-100123456",
        inputs={"request": "short default"},
        wait_mode="sync-short",
    )

    assert real_wait_payload["waitMode"] == "sync-real"
    assert real_wait_payload["timeoutSeconds"] == 600
    assert short_wait_payload["waitMode"] == "sync-short"
    assert short_wait_payload["timeoutSeconds"] == 90


def test_build_gateway_local_launch_request_preserves_explicit_thread_id() -> None:
    """Topic launches must still preserve the explicit thread id.

    pre: caller supplies workflow-level inputs plus an explicit Telegram topic/thread id.
    post: payload carries the same thread id without inventing or rewriting it.
    raises: AssertionError when topic launches lose thread targeting.
    """

    payload = launch_tool.build_gateway_local_launch_request(
        workflow_name="opaque-workflow",
        chat_id="-100123456",
        thread_id="777",
        inputs={"request": "topic payload"},
    )

    assert payload["inputs"] == {"request": "topic payload"}
    assert payload["target"] == {"platform": "telegram", "chatId": "-100123456", "threadId": "777"}


def test_build_gateway_local_launch_request_defaults_to_origin_chat(monkeypatch) -> None:
    """Builder should default launch target to the active Telegram origin.

    pre: caller omits chat_id while running inside an active Telegram session context.
    post: payload targets the current origin chat/thread instead of failing for missing chat_id.
    raises: AssertionError when origin defaults are ignored.
    """

    monkeypatch.setattr(
        launch_tool,
        "_session_origin_target",
        lambda: {"platform": "telegram", "chat_id": "-100999", "thread_id": "555"},
    )

    payload = launch_tool.build_gateway_local_launch_request(
        workflow_name="solution-modeling",
        inputs={"request": "sticky origin"},
    )

    assert payload["target"] == {"platform": "telegram", "chatId": "-100999", "threadId": "555"}


def test_build_gateway_local_launch_request_fails_closed_without_chat_id_or_origin(monkeypatch) -> None:
    """Builder should still fail closed when no explicit or session origin chat exists.

    pre: caller omits chat_id outside any Telegram session origin.
    post: builder raises ValueError instead of inventing a destination.
    raises: AssertionError when destination defaults are synthesized without origin authority.
    """

    monkeypatch.setattr(launch_tool, "_session_origin_target", lambda: None)

    with pytest.raises(ValueError, match="chat_id is required"):
        launch_tool.build_gateway_local_launch_request(
            workflow_name="solution-modeling",
            inputs={"request": "no origin"},
        )


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        (None, "inputs is required"),
        ("not-an-object", "inputs must be an object"),
        (["not", "an", "object"], "inputs must be an object"),
    ],
)
def test_build_gateway_local_launch_request_rejects_invalid_inputs(
    inputs: object,
    message: str,
) -> None:
    """Builder must fail closed on missing or non-object public inputs.

    pre: caller supplies malformed workflow-level inputs.
    post: builder raises ValueError before any request file is written.
    raises: AssertionError when invalid inputs are accepted.
    """

    with pytest.raises(ValueError, match=message):
        launch_tool.build_gateway_local_launch_request(
            workflow_name="fake-workflow",
            chat_id="-100123456",
            inputs=inputs,
        )


def test_handle_tool_rejects_request_json_legacy_argument() -> None:
    """Direct handler calls must fail closed on removed request_json authority.

    pre: a caller bypasses schema validation and passes the removed request_json field.
    post: handler returns success=false with an unsupported-field error.
    raises: AssertionError when request_json is silently ignored or accepted.
    """

    result = json.loads(
        launch_tool._handle_tool(
            {
                "workflow_name": "fake-workflow",
                "chat_id": "-100123456",
                "inputs": {"request": "hello"},
                "request_json": "/tmp/legacy.json",
            }
        )
    )

    assert result["success"] is False
    assert "unsupported legacy fields" in result["error"]
    assert "request_json" in result["error"]


def test_request_gateway_local_canon_launch_waits_for_gateway_response(monkeypatch, tmp_path: Path) -> None:
    """Out-of-process callers should exchange request/response files under one local root.

    pre: temporary request/response directories stand in for HERMES_HOME and a helper thread writes
         the gateway response file after the request appears.
    post: request_gateway_local_canon_launch returns the decoded gateway response payload.
    raises: AssertionError when the response file is ignored or request paths drift outside the local root.
    """

    root = tmp_path / "gateway-control" / "canon-cli-run"
    monkeypatch.setattr(launch_tool, "_request_root", lambda: root)
    monkeypatch.setattr(launch_tool, "_requests_dir", lambda: root / "requests")
    monkeypatch.setattr(launch_tool, "_responses_dir", lambda: root / "responses")

    def _gateway_writer() -> None:
        deadline = time.time() + 5
        while time.time() < deadline:
            request_files = sorted((root / "requests").glob("*.request.json"))
            if request_files:
                payload = json.loads(request_files[0].read_text(encoding="utf-8"))
                response = {
                    "ok": True,
                    "requestId": payload["requestId"],
                    "api": payload["api"],
                    "gateway": {"pid": 3691171, "cwd": "/home/hermes/src/hermes-agent"},
                    "run": {"runId": "sm-test", "status": "awaiting-human-review", "artifactRoot": ".agent/live-solution-modeling/sm-test"},
                    "delivery": {"messageId": "review-msg-1", "chatId": payload["target"]["chatId"]},
                    "responseText": "ok",
                }
                response_path = launch_tool._response_file(payload["requestId"])
                response_path.parent.mkdir(parents=True, exist_ok=True)
                response_path.write_text(json.dumps(response), encoding="utf-8")
                return
            time.sleep(0.05)
        raise AssertionError("request file did not appear in time")

    thread = threading.Thread(target=_gateway_writer, daemon=True)
    thread.start()
    result = launch_tool.request_gateway_local_canon_launch(
        workflow_name="fake-workflow",
        chat_id="-100123456",
        inputs={"request": "dm payload"},
        timeout_seconds=5,
    )
    thread.join(timeout=1)

    assert result["ok"] is True
    assert result["run"]["runId"] == "sm-test"
    assert result["delivery"] == {"messageId": "review-msg-1", "chatId": "-100123456"}
    request_files = sorted((root / "requests").glob("*.request.json"))
    assert len(request_files) == 1
    request_payload = json.loads(request_files[0].read_text(encoding="utf-8"))
    assert request_payload["action"] == "run"
    assert request_payload["workflow"] == "fake-workflow"
    assert request_payload["inputs"] == {"request": "dm payload"}
    assert request_payload["waitMode"] == "sync-short"
    assert request_payload["target"] == {"platform": "telegram", "chatId": "-100123456"}


def test_request_gateway_local_canon_launch_async_no_wait_returns_tracking_handles(monkeypatch, tmp_path: Path) -> None:
    """Async no-wait mode must return request tracking truth without waiting for completion.

    pre: temporary request/response directories stand in for HERMES_HOME and no gateway response file exists yet.
    post: the call returns request/response tracking handles only and leaves the request queued for a watcher.
    raises: AssertionError when async/no-wait still blocks for terminal gateway completion.
    """

    root = tmp_path / "gateway-control" / "canon-cli-run"
    monkeypatch.setattr(launch_tool, "_request_root", lambda: root)
    monkeypatch.setattr(launch_tool, "_requests_dir", lambda: root / "requests")
    monkeypatch.setattr(launch_tool, "_responses_dir", lambda: root / "responses")

    result = launch_tool.request_gateway_local_canon_launch(
        workflow_name="fake-workflow",
        chat_id="-100123456",
        inputs={"request": "queue only"},
        wait_mode="async-no-wait",
    )

    assert result["accepted"] is True
    assert result["waitMode"] == "async-no-wait"
    assert result["requestId"].startswith("cg-launch-")
    assert Path(result["requestPath"]).is_file()
    assert Path(result["responsePath"]) == root / "responses" / f"{result['requestId']}.response.json"
    queued_payload = json.loads(Path(result["requestPath"]).read_text(encoding="utf-8"))
    assert queued_payload["waitMode"] == "async-no-wait"
    assert not Path(result["responsePath"]).exists()
