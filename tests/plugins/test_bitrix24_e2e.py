from __future__ import annotations

import json
from typing import Any

import pytest

@pytest.fixture(autouse=True)
def fake_event_source(monkeypatch):
    from plugins.bitrix24 import ingress
    from tests.plugins.test_bitrix24_event_enrichment import FakeEventSource

    source = FakeEventSource('@hermes сделай краткое резюме и потом mark done')
    monkeypatch.setattr(ingress, "RestBitrix24EventSource", lambda client: source)
    return source

import yaml

_APP_TOKEN = "expected-app-token"


class _CapturingContext:
    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)

    def register_platform(self, **kwargs) -> None:
        self.platforms = [*getattr(self, "platforms", []), kwargs]


class _MockBitrix24Client:
    def __init__(self, attachment_metadata: dict[str, dict[str, Any]]) -> None:
        self.attachment_metadata = attachment_metadata
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, **payload: Any) -> dict[str, Any]:
        self.calls.append((method, payload))
        if method == "disk.file.get":
            file_id = str(payload.get("id"))
            return {"result": self.attachment_metadata[file_id]}
        raise AssertionError(
            "AC-6 REQ-5: webhook receipt must not perform live task mutation or downstream Bitrix24 reads; "
            f"observed unexpected method={method!r} payload={payload!r}"
        )


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("BITRIX24_BOT_MENTION", "@hermes")
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["bitrix24"]}}),
        encoding="utf-8",
    )
    return hermes_home


@pytest.fixture
def ingress_runtime(hermes_home):
    import plugins.bitrix24 as bitrix24
    from plugins.bitrix24 import attachments

    ctx = _CapturingContext()
    bitrix24.register(ctx)
    tool = next((tool for tool in ctx.tools if tool["name"] == "bitrix24_webhook_ingress"), None)
    if tool is None:
        pytest.fail(
            "AC-6 REQ-5: public ingress seam missing; expected plugins.bitrix24.register(ctx) "
            "to expose bitrix24_webhook_ingress before mocked e2e handoff tests run"
        )
    return bitrix24, attachments, tool


@pytest.fixture
def attachment_metadata() -> dict[str, dict[str, Any]]:
    return {
        "111": {
            "ID": "111",
            "DOWNLOAD_URL": "https://example.com/attachments/111.png",
            "CONTENT_TYPE": "image/png",
            "SIZE": 2048,
        }
    }


def _comment_payload(*, event_id: str = "evt-e2e-1", auth_extra: dict[str, Any] | None = None) -> dict[str, Any]:
    auth = {"domain": "example.com", "application_token": _APP_TOKEN}
    if isinstance(auth_extra, dict):
        auth.update(auth_extra)
    return {
        "event_id": event_id,
        "event": "ONTASKCOMMENTADD",
        "auth": auth,
        "data": {
            "FIELDS_AFTER": {
                "ID": "0",
                "MESSAGE_ID": "84",
                "TASK_ID": "42",
                "FILE_IDS": ["111"],
            }
        },
    }


def _invoke(tool: dict[str, Any], payload: dict[str, Any], *, task_id: str = "red-prep-e2e") -> dict:
    return json.loads(tool["handler"](payload, task_id=task_id))


def test_bitrix24_e2e_comment_ingress_requires_agent_request_handoff_before_downstream_checks(
    ingress_runtime,
    attachment_metadata,
    monkeypatch,
):
    bitrix24, attachments, ingress_tool = ingress_runtime
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", _APP_TOKEN)
    mock_client = _MockBitrix24Client(attachment_metadata)
    monkeypatch.setattr(attachments, "_get_client", lambda: mock_client)

    result = _invoke(ingress_tool, _comment_payload())

    assert result["accepted"] is True
    assert result["reason"] == "targeted_event"
    assert result["task_id"] == "42"
    assert result["message"] == "@hermes сделай краткое резюме и потом mark done"
    assert result["attachments"][0]["file_id"] == "111"
    assert result["attachments"][0]["vision"]["requested"] is True
    assert mock_client.calls == [("disk.file.get", {"id": "111"})]
    assert "agent_request" in result, (
        "AC-6 REQ-5: targeted comment ingress already materializes attachments, but mocked e2e handoff "
        "is still absent; expected agent_request with task context, comments, mocked vision summary, and "
        f"deferred mark-done candidate; observed keys={sorted(result)}"
    )
    assert result["deferred_actions"] == [
        {
            "tool": "bitrix24_task_comment_add",
            "args": {"task_id": "42", "message": ""},
            "deferred": True,
            "reason": "targeted_comment_reply",
            "requires_agent_message": True,
        },
        {
            "tool": "bitrix24_task_complete",
            "args": {"task_id": "42"},
            "deferred": True,
            "reason": "explicit_completion_request",
        },
    ]


def test_bitrix24_e2e_agent_request_carries_mocked_context_and_no_secret_leakage(
    ingress_runtime,
    attachment_metadata,
    monkeypatch,
):
    bitrix24, attachments, ingress_tool = ingress_runtime
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "fake-secret-token")
    monkeypatch.setattr(attachments, "_get_client", lambda: _MockBitrix24Client(attachment_metadata))

    result = _invoke(
        ingress_tool,
        _comment_payload(event_id="evt-e2e-2", auth_extra={"application_token": "fake-secret-token"}),
    )
    agent_request = result.get("agent_request")
    if agent_request is None:
        pytest.skip("AC-6 REQ-5 downstream context shape waits for agent_request materialization")

    assert agent_request["task"]["id"] == "42"
    assert agent_request["task"]["comments"][0]["id"] == "84"
    assert agent_request["attachments"][0]["vision_summary"]
    assert "download_url" not in json.dumps(agent_request["attachments"])
    assert "BITRIX24_WEBHOOK_URL" not in json.dumps(agent_request)
    assert "fake-secret-token" not in json.dumps(agent_request)
    assert "fake-secret-token" not in json.dumps(result)
    assert "auth" not in result.get("payload", {})


def test_bitrix24_e2e_mark_done_stays_deferred_until_explicit_tool_invocation(
    ingress_runtime,
    attachment_metadata,
    monkeypatch,
):
    bitrix24, attachments, ingress_tool = ingress_runtime
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", _APP_TOKEN)
    mock_client = _MockBitrix24Client(attachment_metadata)
    monkeypatch.setattr(attachments, "_get_client", lambda: mock_client)

    result = _invoke(ingress_tool, _comment_payload(event_id="evt-e2e-3"))
    deferred_actions = result.get("deferred_actions", [])
    if not deferred_actions:
        pytest.skip("AC-6 REQ-5 deferred mark-done contract waits for agent_request materialization")

    assert deferred_actions == [
        {
            "tool": "bitrix24_task_comment_add",
            "args": {"task_id": "42", "message": ""},
            "deferred": True,
            "reason": "targeted_comment_reply",
            "requires_agent_message": True,
        },
        {
            "tool": "bitrix24_task_complete",
            "args": {"task_id": "42"},
            "deferred": True,
            "reason": "explicit_completion_request",
        },
    ]
    assert "download_url" not in json.dumps(result["attachments"])
    assert mock_client.calls == [("disk.file.get", {"id": "111"})]
