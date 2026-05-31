"""Tests for the local-only Canon gateway launch request surface."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import tools.canon_gateway_launch as launch_tool


def test_build_gateway_local_launch_request_rejects_placeholder_default() -> None:
    """Placeholder/default task text must fail closed before a request file is written.

    pre: caller attempts to use the packaged default Canon task text.
    post: request builder raises ValueError instead of creating a broad or placeholder launch.
    raises: AssertionError when placeholder text slips through local validation.
    """

    with pytest.raises(ValueError, match="packaged default"):
        launch_tool.build_gateway_local_launch_request(
            task_text="Model a solution and pause for human review.",
            chat_id="-100123456",
            thread_id="777",
        )


def test_request_gateway_local_canon_launch_waits_for_gateway_response(monkeypatch, tmp_path: Path) -> None:
    """Out-of-process callers should exchange request/response files under one local root.

    pre: temporary request/response directories stand in for HERMES_HOME and a helper thread writes
         the gateway response file after the request appears.
    post: request_gateway_local_canon_launch returns the decoded gateway response payload.
    raises: AssertionError when the response file is ignored or request paths drift outside the local root.
    """

    root = tmp_path / "gateway-control" / "canon-solution-modeling-launch"
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
                    "delivery": {"messageId": "review-msg-1", "chatId": payload["target"]["chatId"], "threadId": payload["target"]["threadId"]},
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
        task_text="Build a tiny JSON checklist for comparing blue and green.",
        chat_id="-100123456",
        thread_id="777",
        timeout_seconds=5,
    )
    thread.join(timeout=1)

    assert result["ok"] is True
    assert result["run"]["runId"] == "sm-test"
    assert result["delivery"]["threadId"] == "777"
    request_files = sorted((root / "requests").glob("*.request.json"))
    assert len(request_files) == 1
    request_payload = json.loads(request_files[0].read_text(encoding="utf-8"))
    assert request_payload["action"] == "launch_solution_modeling"
    assert request_payload["target"] == {"platform": "telegram", "chatId": "-100123456", "threadId": "777"}
