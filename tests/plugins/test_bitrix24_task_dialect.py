from __future__ import annotations

import json

import pytest

from plugins.bitrix24 import client as bitrix24_client
from plugins.bitrix24 import schemas as bitrix24_schemas
from plugins.bitrix24 import tools as bitrix24_tools


class _FakeClient:
    def __init__(self, result=None, error: Exception | None = None, results=None) -> None:
        self.result = result
        self.error = error
        self.results = list(results) if results is not None else None
        self.calls: list[tuple[str, dict]] = []

    def call(self, method: str, **payload):
        self.calls.append((method, payload))
        if self.error is not None:
            raise self.error
        if self.results is not None:
            return self.results.pop(0)
        return self.result if self.result is not None else {"result": {"id": 42}}


@pytest.mark.parametrize(
    ("handler", "method"),
    [
        (bitrix24_tools._handle_bitrix24_task_get, "tasks.task.get"),
        (bitrix24_tools._handle_bitrix24_task_update, "tasks.task.update"),
        (bitrix24_tools._handle_bitrix24_task_complete, "tasks.task.complete"),
        (bitrix24_tools._handle_bitrix24_task_comment_add, "task.commentitem.add"),
    ],
)
def test_old_rest_task_handlers_send_taskId_but_keep_public_task_id_envelope(monkeypatch, handler, method):
    fake_client = _FakeClient()
    monkeypatch.setattr(bitrix24_client, "_get_client", lambda: fake_client)

    payload: dict[str, object] = {"task_id": "42"}
    if method == "tasks.task.update":
        payload["fields"] = {"TITLE": "Updated"}
    elif method == "task.commentitem.add":
        payload["message"] = "  Added from test  "

    result = json.loads(handler(payload, task_id="slice-s3"))

    assert fake_client.calls == [
        (
            method,
            {
                "taskId": "42",
                **({"fields": {"TITLE": "Updated"}} if method == "tasks.task.update" else {}),
                **(
                    {"fields": {"POST_MESSAGE": "Added from test"}}
                    if method == "task.commentitem.add"
                    else {}
                ),
            },
        )
    ]
    assert result["success"] is True
    assert result["method"] == method
    assert result["task_id"] == "42"


def test_task_comments_resolve_task_chat_and_read_dialog_messages(monkeypatch):
    fake_client = _FakeClient(
        results=[
            {"result": {"task": {"id": "42", "chatId": "1832"}}},
            {"result": {"messages": [{"id": "5", "text": "visible task comment"}]}},
        ]
    )
    monkeypatch.setattr(bitrix24_client, "_get_client", lambda: fake_client)

    result = json.loads(bitrix24_tools._handle_bitrix24_task_comments({"task_id": "42"}))

    assert fake_client.calls == [
        ("tasks.task.get", {"taskId": "42", "select": ["ID", "CHAT_ID"]}),
        ("im.dialog.messages.get", {"DIALOG_ID": "chat1832"}),
    ]
    assert result["success"] is True
    assert result["method"] == "im.dialog.messages.get"
    assert result["task_id"] == "42"
    assert result["result"]["result"]["messages"][0]["text"] == "visible task comment"


def test_task_comments_fallback_to_task_entity_chat_lookup(monkeypatch):
    fake_client = _FakeClient(
        results=[
            {"result": {"task": {"id": "42"}}},
            {"result": {"id": 1832}},
            {"result": {"messages": [{"id": "7", "text": "fallback task chat"}]}},
        ]
    )
    monkeypatch.setattr(bitrix24_client, "_get_client", lambda: fake_client)

    result = json.loads(bitrix24_tools._handle_bitrix24_task_comments({"task_id": "42"}))

    assert fake_client.calls == [
        ("tasks.task.get", {"taskId": "42", "select": ["ID", "CHAT_ID"]}),
        ("im.chat.get", {"ENTITY_TYPE": "TASKS_TASK", "ENTITY_ID": "42"}),
        ("im.dialog.messages.get", {"DIALOG_ID": "chat1832"}),
    ]
    assert result["success"] is True
    assert result["method"] == "im.dialog.messages.get"
    assert result["task_id"] == "42"


def test_task_create_requires_responsible_id_and_maps_old_rest_fields(monkeypatch):
    fake_client = _FakeClient(result={"result": {"taskId": 77}})
    monkeypatch.setattr(bitrix24_client, "_get_client", lambda: fake_client)

    result = json.loads(
        bitrix24_tools._handle_bitrix24_task_create(
            {
                "title": "  Mock task  ",
                "description": "  dry run  ",
                "responsible_id": "5",
                "creator_id": 9,
            },
            task_id="slice-s3",
        )
    )

    assert fake_client.calls == [
        (
            "tasks.task.add",
            {
                "fields": {
                    "TITLE": "Mock task",
                    "DESCRIPTION": "dry run",
                    "RESPONSIBLE_ID": "5",
                    "CREATED_BY": "9",
                }
            },
        )
    ]
    assert result["success"] is True
    assert result["method"] == "tasks.task.add"
    assert result["task_id"] == "77"


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ({"title": "Task only"}, "responsible_id is required"),
        ({"title": "Task", "responsible_id": 0}, "responsible_id must be a positive integer"),
        ({"title": "Task", "responsible_id": "-1"}, "responsible_id must be a positive integer"),
        ({"title": "Task", "responsible_id": 1, "creator_id": "abc"}, "creator_id must be a positive integer"),
        ({"task_id": 0}, "task_id must be a positive integer"),
    ],
)
def test_task_handlers_reject_non_positive_ids(payload, expected_error):
    if "task_id" in payload:
        result = json.loads(bitrix24_tools._handle_bitrix24_task_get(payload, task_id="slice-s3"))
        assert result["method"] == "tasks.task.get"
    else:
        result = json.loads(bitrix24_tools._handle_bitrix24_task_create(payload, task_id="slice-s3"))
        assert result["method"] == "tasks.task.add"
    assert result["success"] is False
    assert result["error"] == expected_error


def test_task_create_schema_requires_responsible_id_and_positive_ids():
    parameters = bitrix24_schemas.BITRIX24_TASK_CREATE_SCHEMA["parameters"]
    responsible_schema = parameters["properties"]["responsible_id"]
    creator_schema = parameters["properties"]["creator_id"]

    assert sorted(parameters["required"]) == ["responsible_id", "title"]
    assert responsible_schema == creator_schema
    assert responsible_schema["anyOf"] == [
        {"type": "integer", "minimum": 1},
        {"type": "string", "pattern": "^[1-9][0-9]*$"},
    ]


def test_task_list_maps_supported_old_rest_payload_fields(monkeypatch):
    fake_client = _FakeClient(result={"result": {"tasks": [{"id": "42"}]}})
    monkeypatch.setattr(bitrix24_client, "_get_client", lambda: fake_client)

    result = json.loads(
        bitrix24_tools._handle_bitrix24_task_list(
            {
                "order": {"DEADLINE": "asc"},
                "filter": {"RESPONSIBLE_ID": 5, "!STATUS": 6},
                "select": ["ID", "TITLE", "STATUS"],
            },
            task_id="slice-s2",
        )
    )

    assert fake_client.calls == [
        (
            "tasks.task.list",
            {
                "order": {"DEADLINE": "asc"},
                "filter": {"RESPONSIBLE_ID": 5, "!STATUS": 6},
                "select": ["ID", "TITLE", "STATUS"],
            },
        )
    ]
    assert result["success"] is True
    assert result["method"] == "tasks.task.list"


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ({"order": []}, "order must be an object"),
        ({"filter": []}, "filter must be an object"),
        ({"select": "ID"}, "select must be an array of strings"),
        ({"select": ["ID", 7]}, "select must be an array of strings"),
    ],
)
def test_task_list_rejects_unsupported_payload_shapes(payload, expected_error):
    result = json.loads(bitrix24_tools._handle_bitrix24_task_list(payload, task_id="slice-s2"))

    assert result == {
        "success": False,
        "method": "tasks.task.list",
        "error": expected_error,
    }


def test_task_list_schema_allows_only_filter_order_and_select():
    parameters = bitrix24_schemas.BITRIX24_TASK_LIST_SCHEMA["parameters"]

    assert parameters["additionalProperties"] is False
    assert parameters["properties"] == {
        "order": {"type": "object"},
        "filter": {"type": "object"},
        "select": {"type": "array", "items": {"type": "string"}},
    }


@pytest.mark.parametrize(
    ("payload", "method", "expected_error"),
    [
        ({"task_id": 0, "message": "ok"}, "task.commentitem.add", "task_id must be a positive integer"),
        ({"task_id": "42", "message": "   "}, "task.commentitem.add", "message is required"),
        (
            {"task_id": "42", "comment_id": 0, "message": "ok"},
            "task.commentitem.update",
            "comment_id must be a positive integer",
        ),
        (
            {"task_id": "42", "comment_id": "7", "message": "   "},
            "task.commentitem.update",
            "message is required",
        ),
    ],
)
def test_task_comment_write_handlers_validate_positive_ids_and_non_empty_message(
    payload,
    method,
    expected_error,
):
    handler = (
        bitrix24_tools._handle_bitrix24_task_comment_add
        if method == "task.commentitem.add"
        else bitrix24_tools._handle_bitrix24_task_comment_update
    )

    result = json.loads(handler(payload, task_id="slice-s3"))

    assert result == {
        "success": False,
        "method": method,
        "error": expected_error,
    }


def test_task_comment_update_maps_task_id_comment_id_and_trimmed_message(monkeypatch):
    fake_client = _FakeClient(result={"result": {"id": 9}})
    monkeypatch.setattr(bitrix24_client, "_get_client", lambda: fake_client)

    result = json.loads(
        bitrix24_tools._handle_bitrix24_task_comment_update(
            {"task_id": "42", "comment_id": "9", "message": "  Updated from test  "},
            task_id="slice-s3",
        )
    )

    assert fake_client.calls == [
        (
            "task.commentitem.update",
            {"taskId": "42", "itemId": "9", "fields": {"POST_MESSAGE": "Updated from test"}},
        )
    ]
    assert result["success"] is True
    assert result["method"] == "task.commentitem.update"
    assert result["task_id"] == "42"


def test_task_comment_write_schemas_are_bounded_and_require_documented_fields():
    add_parameters = bitrix24_schemas.BITRIX24_TASK_COMMENT_ADD_SCHEMA["parameters"]
    update_parameters = bitrix24_schemas.BITRIX24_TASK_COMMENT_UPDATE_SCHEMA["parameters"]

    assert add_parameters["required"] == ["task_id", "message"]
    assert add_parameters["additionalProperties"] is False
    assert add_parameters["properties"] == {
        "task_id": bitrix24_schemas._POSITIVE_ID_SCHEMA,
        "message": {"type": "string"},
    }
    assert update_parameters["required"] == ["task_id", "comment_id", "message"]
    assert update_parameters["additionalProperties"] is False
    assert update_parameters["properties"] == {
        "task_id": bitrix24_schemas._POSITIVE_ID_SCHEMA,
        "comment_id": bitrix24_schemas._POSITIVE_ID_SCHEMA,
        "message": {"type": "string"},
    }


def test_chat_get_maps_public_chat_id_to_documented_dialog_id(monkeypatch):
    fake_client = _FakeClient(result={"result": {"chatId": "chat1435"}})
    monkeypatch.setattr(bitrix24_client, "_get_client", lambda: fake_client)

    result = json.loads(
        bitrix24_tools._handle_bitrix24_chat_get({"chat_id": "chat1435"}, task_id="slice-s3")
    )

    assert fake_client.calls == [("im.dialog.messages.get", {"DIALOG_ID": "chat1435"})]
    assert result["success"] is True
    assert result["method"] == "im.dialog.messages.get"
    assert result["chat_id"] == "chat1435"


def test_chat_get_normalizes_numeric_chat_id_to_task_dialog_id(monkeypatch):
    fake_client = _FakeClient(result={"result": {"messages": [{"id": "1"}]}})
    monkeypatch.setattr(bitrix24_client, "_get_client", lambda: fake_client)

    result = json.loads(bitrix24_tools._handle_bitrix24_chat_get({"chat_id": 1832}))

    assert fake_client.calls == [("im.dialog.messages.get", {"DIALOG_ID": "chat1832"})]
    assert result["success"] is True
    assert result["method"] == "im.dialog.messages.get"
    assert result["chat_id"] == "1832"


def test_task_comments_and_chat_get_schemas_describe_task_chat_without_widening():
    task_parameters = bitrix24_schemas.BITRIX24_TASK_COMMENTS_SCHEMA["parameters"]
    chat_parameters = bitrix24_schemas.BITRIX24_CHAT_GET_SCHEMA["parameters"]

    task_description = bitrix24_schemas.BITRIX24_TASK_COMMENTS_SCHEMA["description"]
    chat_description = bitrix24_schemas.BITRIX24_CHAT_GET_SCHEMA["description"]

    assert "task chat" in task_description
    assert "im.dialog.messages.get" in task_description
    assert "chat<chatId>" in task_description
    assert "chat<id>" in chat_description
    assert task_parameters["additionalProperties"] is False
    assert task_parameters["properties"] == {"task_id": bitrix24_schemas._POSITIVE_ID_SCHEMA}
    assert chat_parameters["additionalProperties"] is False
    assert chat_parameters["properties"] == {"chat_id": {"type": ["string", "integer"]}}


def test_chat_send_maps_public_chat_id_and_trimmed_message_to_documented_payload(monkeypatch):
    fake_client = _FakeClient(result={"result": {"id": 314}})
    monkeypatch.setattr(bitrix24_client, "_get_client", lambda: fake_client)

    result = json.loads(
        bitrix24_tools._handle_bitrix24_chat_send(
            {"chat_id": "sg77", "message": "  Hello project chat  "},
            task_id="slice-s4",
        )
    )

    assert fake_client.calls == [("im.message.add", {"DIALOG_ID": "sg77", "MESSAGE": "Hello project chat"})]
    assert result["success"] is True
    assert result["method"] == "im.message.add"
    assert result["chat_id"] == "sg77"


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ({"chat_id": "", "message": "ok"}, "chat_id is required"),
        ({"chat_id": "chat55", "message": "   "}, "message is required"),
    ],
)
def test_chat_send_validates_required_chat_id_and_non_empty_message(payload, expected_error):
    result = json.loads(bitrix24_tools._handle_bitrix24_chat_send(payload, task_id="slice-s4"))

    assert result == {
        "success": False,
        "method": "im.message.add",
        "error": expected_error,
    }


def test_chat_send_schema_is_bounded_and_supports_only_chat_id_and_message():
    parameters = bitrix24_schemas.BITRIX24_CHAT_SEND_SCHEMA["parameters"]

    assert parameters["required"] == ["chat_id", "message"]
    assert parameters["additionalProperties"] is False
    assert parameters["properties"] == {
        "chat_id": {"type": ["string", "integer"]},
        "message": {"type": "string"},
    }
