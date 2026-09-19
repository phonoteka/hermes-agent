from __future__ import annotations

import json

import pytest

from plugins.bitrix24 import client as bitrix24_client
from plugins.bitrix24 import tools as bitrix24_tools


class _FakeClient:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def call(self, method: str, **payload):
        self.calls.append((method, payload))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.parametrize(
    ("result", "preferred_field", "expected"),
    [
        ({"task_id": "42"}, "task_id", "42"),
        ({"taskId": "42"}, "task_id", "42"),
        ({"task_id": 42}, "task_id", "42"),
        ({"result": {"taskId": 42}}, "task_id", "42"),
        ({"result": {"task_id": 42}}, "task_id", "42"),
        ({"result": {"id": "nested-id"}}, "task_id", "nested-id"),
        ({"id": 99}, "task_id", "99"),
        ({"result": {"chat_id": "chat-7"}}, "chat_id", "chat-7"),
        ({}, "task_id", None),
    ],
)
def test_bitrix24_result_id_extracts_nested_and_fallback_ids(result, preferred_field, expected):
    assert bitrix24_client._result_id(result, preferred_field) == expected


@pytest.mark.parametrize(
    ("payload", "method", "expected_error", "expected_identifier"),
    [
        ({}, "tasks.task.get", "task_id is required", None),
        ({"task_id": 0}, "tasks.task.get", "task_id must be a positive integer", None),
        ({"task_id": "42", "fields": None}, "tasks.task.update", "fields is required", "42"),
        ({}, "im.dialog.messages.get", "chat_id is required", None),
    ],
)
def test_bitrix24_tool_handlers_return_required_field_error_envelopes(
    payload,
    method,
    expected_error,
    expected_identifier,
):
    handler = {
        "tasks.task.get": bitrix24_tools._handle_bitrix24_task_get,
        "tasks.task.update": bitrix24_tools._handle_bitrix24_task_update,
        "im.dialog.messages.get": bitrix24_tools._handle_bitrix24_chat_get,
    }[method]

    result = json.loads(handler(payload, task_id="coverage-f5"))

    assert result["success"] is False
    assert result["method"] == method
    assert result["error"] == expected_error
    if expected_identifier is None:
        assert "task_id" not in result and "chat_id" not in result
    elif method == "im.dialog.messages.get":
        assert result["chat_id"] == expected_identifier
    else:
        assert result["task_id"] == expected_identifier


def test_bitrix24_call_method_wraps_transport_errors_in_json_envelope(monkeypatch):
    fake_client = _FakeClient(error=RuntimeError("transport_error: timeout"))
    monkeypatch.setattr(bitrix24_client, "_get_client", lambda: fake_client)

    result = json.loads(
        bitrix24_client._call_method(
            "tasks.task.get",
            id_field="task_id",
            identifier="42",
            taskId="42",
        )
    )

    assert fake_client.calls == [("tasks.task.get", {"taskId": "42"})]
    assert result == {
        "success": False,
        "method": "tasks.task.get",
        "task_id": "42",
        "error": "transport_error: timeout",
    }


def test_bitrix24_call_method_derives_identifier_from_nested_result(monkeypatch):
    fake_client = _FakeClient(result={"result": {"taskId": 4242, "ok": True}})
    monkeypatch.setattr(bitrix24_client, "_get_client", lambda: fake_client)

    result = json.loads(bitrix24_client._call_method("tasks.task.add", id_field="task_id", fields={"TITLE": "T"}))

    assert fake_client.calls == [("tasks.task.add", {"fields": {"TITLE": "T"}})]
    assert result["success"] is True
    assert result["method"] == "tasks.task.add"
    assert result["task_id"] == "4242"
    assert result["result"] == {"result": {"taskId": 4242, "ok": True}}
