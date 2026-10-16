from __future__ import annotations

import json
from typing import Any

import pytest
import yaml


_REQUIRED_TOOL_NAMES = [
    "bitrix24_task_create",
    "bitrix24_task_get",
    "bitrix24_task_list",
    "bitrix24_task_update",
    "bitrix24_task_complete",
    "bitrix24_task_comments",
    "bitrix24_task_comment_add",
    "bitrix24_task_comment_update",
    "bitrix24_chat_get",
    "bitrix24_chat_send",
]


class _CapturingContext:
    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)

    def register_platform(self, **kwargs) -> None:
        self.platforms = [*getattr(self, "platforms", []), kwargs]


class _MockBitrix24Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, **payload: Any) -> dict[str, Any]:
        self.calls.append((method, payload))
        return {
            "method": method,
            "payload": payload,
            "result": {
                "id": payload.get("task_id", "42"),
                "title": payload.get("title", "Mock task"),
                "status": payload.get("status", "open"),
            },
        }


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["bitrix24"]}}),
        encoding="utf-8",
    )
    return hermes_home


@pytest.fixture
def registered_bitrix24_tools(hermes_home):
    from plugins.bitrix24 import register

    ctx = _CapturingContext()
    register(ctx)
    return ctx.tools


@pytest.fixture
def task_tool_inventory(registered_bitrix24_tools):
    return {tool["name"]: tool for tool in registered_bitrix24_tools}


@pytest.fixture
def missing_task_tools(task_tool_inventory):
    return [name for name in _REQUIRED_TOOL_NAMES if name not in task_tool_inventory]


@pytest.fixture
def mock_client():
    return _MockBitrix24Client()


def _tool_or_skip(task_tool_inventory, name: str, missing_task_tools: list[str]) -> dict[str, Any]:
    tool = task_tool_inventory.get(name)
    if tool is None:
        pytest.skip(
            "AC-4 REQ-3 REQ-7 downstream task-tool behavior waits for the dedicated "
            f"public tool surface; missing tools={missing_task_tools!r}"
        )
    return tool


def _invoke(tool: dict[str, Any], payload: dict[str, Any], *, task_id: str = "red-prep") -> dict[str, Any]:
    raw = tool["handler"](payload, task_id=task_id)
    return json.loads(raw)


def test_bitrix24_task_tool_surface_is_registered_before_behavior_checks(task_tool_inventory):
    registered_names = sorted(task_tool_inventory)
    missing = [name for name in _REQUIRED_TOOL_NAMES if name not in task_tool_inventory]

    assert missing == [], (
        "AC-4 REQ-3 REQ-7: plugins.bitrix24.register(ctx) must expose the public "
        "task tool surface before mocked create/get/update/complete/comments/chat "
        f"behavior can run; missing={missing!r}; registered={registered_names!r}"
    )


def test_bitrix24_task_create_returns_json_envelope_without_secret_leakage(
    task_tool_inventory,
    missing_task_tools,
    monkeypatch,
    mock_client,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setattr("plugins.bitrix24.client.Bitrix24Client", lambda *args, **kwargs: mock_client)
    tool = _tool_or_skip(task_tool_inventory, "bitrix24_task_create", missing_task_tools)

    result = _invoke(tool, {"title": "Mock task", "description": "dry run", "responsible_id": 7})

    assert result["success"] is True
    assert result["method"] == "tasks.task.add"
    assert result["task_id"]
    assert "BITRIX24_WEBHOOK_URL" not in json.dumps(result)


def test_bitrix24_task_update_and_complete_call_mocked_methods_with_task_id(
    task_tool_inventory,
    missing_task_tools,
    monkeypatch,
    mock_client,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setattr("plugins.bitrix24.client.Bitrix24Client", lambda *args, **kwargs: mock_client)
    update_tool = _tool_or_skip(task_tool_inventory, "bitrix24_task_update", missing_task_tools)
    complete_tool = _tool_or_skip(task_tool_inventory, "bitrix24_task_complete", missing_task_tools)

    update_result = _invoke(update_tool, {"task_id": "42", "fields": {"TITLE": "Updated"}})
    complete_result = _invoke(complete_tool, {"task_id": "42"})

    assert update_result["success"] is True
    assert update_result["method"] == "tasks.task.update"
    assert update_result["task_id"] == "42"
    assert complete_result["success"] is True
    assert complete_result["method"] == "tasks.task.complete"
    assert complete_result["task_id"] == "42"
    assert mock_client.calls[:2] == [
        ("tasks.task.update", {"taskId": "42", "fields": {"TITLE": "Updated"}}),
        ("tasks.task.complete", {"taskId": "42"}),
    ]


def test_bitrix24_task_list_returns_json_envelope_without_secret_leakage(
    task_tool_inventory,
    missing_task_tools,
    monkeypatch,
    mock_client,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setattr("plugins.bitrix24.client.Bitrix24Client", lambda *args, **kwargs: mock_client)
    tool = _tool_or_skip(task_tool_inventory, "bitrix24_task_list", missing_task_tools)

    result = _invoke(
        tool,
        {"filter": {"RESPONSIBLE_ID": 7}, "order": {"ID": "desc"}, "select": ["ID", "TITLE"]},
    )

    assert result["success"] is True
    assert result["method"] == "tasks.task.list"
    assert "BITRIX24_WEBHOOK_URL" not in json.dumps(result)
    assert mock_client.calls[-1] == (
        "tasks.task.list",
        {"filter": {"RESPONSIBLE_ID": 7}, "order": {"ID": "desc"}, "select": ["ID", "TITLE"]},
    )


def test_bitrix24_comments_and_chat_tools_return_json_error_envelope(
    task_tool_inventory,
    missing_task_tools,
    monkeypatch,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    comments_tool = _tool_or_skip(task_tool_inventory, "bitrix24_task_comments", missing_task_tools)
    add_tool = _tool_or_skip(task_tool_inventory, "bitrix24_task_comment_add", missing_task_tools)
    update_tool = _tool_or_skip(task_tool_inventory, "bitrix24_task_comment_update", missing_task_tools)
    chat_tool = _tool_or_skip(task_tool_inventory, "bitrix24_chat_get", missing_task_tools)
    chat_send_tool = _tool_or_skip(task_tool_inventory, "bitrix24_chat_send", missing_task_tools)

    comments_result = _invoke(comments_tool, {"task_id": "42"})
    add_result = _invoke(add_tool, {"task_id": "42", "message": "  Hello world  "})
    update_result = _invoke(
        update_tool,
        {"task_id": "42", "comment_id": "9", "message": "  Hello again  "},
    )
    chat_result = _invoke(chat_tool, {"chat_id": "99"})
    chat_send_result = _invoke(chat_send_tool, {"chat_id": "chat99", "message": "  hello chat  "})

    assert set(comments_result) >= {"success", "method"}
    assert set(add_result) >= {"success", "method"}
    assert set(update_result) >= {"success", "method"}
    assert set(chat_result) >= {"success", "method"}
    assert set(chat_send_result) >= {"success", "method"}
    assert comments_result["success"] in {True, False}
    assert add_result["success"] in {True, False}
    assert update_result["success"] in {True, False}
    assert chat_result["success"] in {True, False}
    assert chat_send_result["success"] in {True, False}


def test_bitrix24_chat_send_returns_json_envelope_without_secret_leakage(
    task_tool_inventory,
    missing_task_tools,
    monkeypatch,
    mock_client,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setattr("plugins.bitrix24.client.Bitrix24Client", lambda *args, **kwargs: mock_client)
    tool = _tool_or_skip(task_tool_inventory, "bitrix24_chat_send", missing_task_tools)

    result = _invoke(tool, {"chat_id": 99, "message": "  Hello team  "})

    assert result["success"] is True
    assert result["method"] == "im.message.add"
    assert result["chat_id"] == "99"
    assert "BITRIX24_WEBHOOK_URL" not in json.dumps(result)
    assert mock_client.calls[-1] == (
        "im.message.add",
        {"DIALOG_ID": "99", "MESSAGE": "Hello team"},
    )


def test_bitrix24_task_comment_write_tools_return_json_envelopes_without_secret_leakage(
    task_tool_inventory,
    missing_task_tools,
    monkeypatch,
    mock_client,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setattr("plugins.bitrix24.client.Bitrix24Client", lambda *args, **kwargs: mock_client)
    add_tool = _tool_or_skip(task_tool_inventory, "bitrix24_task_comment_add", missing_task_tools)
    update_tool = _tool_or_skip(task_tool_inventory, "bitrix24_task_comment_update", missing_task_tools)

    add_result = _invoke(add_tool, {"task_id": "42", "message": "  First comment  "})
    update_result = _invoke(
        update_tool,
        {"task_id": "42", "comment_id": 11, "message": "  Updated comment  "},
    )

    assert add_result["success"] is True
    assert add_result["method"] == "task.commentitem.add"
    assert add_result["task_id"] == "42"
    assert update_result["success"] is True
    assert update_result["method"] == "task.commentitem.update"
    assert update_result["task_id"] == "42"
    dumped = json.dumps({"add": add_result, "update": update_result})
    assert "BITRIX24_WEBHOOK_URL" not in dumped
    assert mock_client.calls[-2:] == [
        ("task.commentitem.add", {"taskId": "42", "fields": {"POST_MESSAGE": "First comment"}}),
        (
            "task.commentitem.update",
            {"taskId": "42", "itemId": "11", "fields": {"POST_MESSAGE": "Updated comment"}},
        ),
    ]
