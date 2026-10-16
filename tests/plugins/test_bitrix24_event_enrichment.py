import json

import pytest

from plugins.bitrix24.enrichment import (
    Bitrix24SourceUnavailable,
    MessageFacts,
    RestBitrix24EventSource,
    TaskFacts,
    bot_user_id_from_webhook_url,
)


class FakeEventSource:
    """Supply explicit task/message facts without accessing the network."""

    def __init__(self, text="@hermes take a look", author_id="1"):
        self.task = TaskFacts("42", "Webhook smoke", "@hermes review task", "5", "6", "99", "52")
        self.message = MessageFacts("84", author_id, text)
        self.messages = {}
        self.unavailable = False
        self.calls = []

    def get_task(self, task_id):
        self.calls.append(("task", task_id))
        if self.unavailable:
            raise Bitrix24SourceUnavailable("unavailable")
        return self.task

    def get_task_message(self, chat_id, message_id):
        self.calls.append(("message", chat_id, message_id))
        if self.unavailable:
            raise Bitrix24SourceUnavailable("unavailable")
        return self.messages.get(message_id, self.message)


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def call(self, method, **payload):
        self.calls.append((method, payload))
        if self.error:
            raise self.error
        return self.response


def test_rest_task_found():
    client = FakeClient({"result": {"task": {
        "id": "1154", "title": "Задача", "description": "Описание",
        "createdBy": "5", "changedBy": "6", "chatId": "99", "groupId": "54",
    }}})
    assert RestBitrix24EventSource(client).get_task("1154") == TaskFacts(
        "1154", "Задача", "Описание", "5", "6", "99", "54",
    )
    assert client.calls == [("tasks.task.get", {
        "taskId": "1154", "select": ["ID", "TITLE", "DESCRIPTION", "CREATED_BY", "CHANGED_BY", "CHAT_ID", "GROUP_ID"],
    })]


@pytest.mark.parametrize("result", [[], {}, {"task": []}, None])
def test_rest_task_not_found(result):
    assert RestBitrix24EventSource(FakeClient({"result": result})).get_task("1154") is None


@pytest.mark.parametrize("returned_id,found", [(201404, True), (201406, False)])
def test_rest_message_matches_exact_id(returned_id, found):
    client = FakeClient({"result": {"messages": [{"id": returned_id, "author_id": 5, "text": "коммент"}]}})
    result = RestBitrix24EventSource(client).get_task_message("99", "201404")
    assert result == (MessageFacts("201404", "5", "коммент") if found else None)
    assert client.calls == [("im.dialog.messages.get", {"DIALOG_ID": "chat99", "FIRST_ID": 201403, "LIMIT": 1})]


@pytest.mark.parametrize("operation", ["task", "message"])
def test_rest_source_translates_errors(operation):
    error = RuntimeError("transport failed")
    source = RestBitrix24EventSource(FakeClient(error=error))
    with pytest.raises(Bitrix24SourceUnavailable) as caught:
        if operation == "task":
            source.get_task("1154")
        else:
            source.get_task_message("99", "201404")
    assert caught.value.__cause__ is error


def test_bot_id_from_old_rest_url():
    assert bot_user_id_from_webhook_url("https://example.com/rest/78/fake/") == "78"


@pytest.mark.parametrize("url", ["", "https://example.com/", "https://example.com/rest/api/78/fake", "https://example.com/rest/no-id/fake"])
def test_bot_id_missing(url):
    with pytest.raises(ValueError):
        bot_user_id_from_webhook_url(url)


def real_payload(event="ONTASKCOMMENTADD", event_id="real-event"):
    fields = {"ID": "0", "MESSAGE_ID": "201404", "TASK_ID": "1154"} if "COMMENT" in event else {"ID": "1154"}
    return {
        "event": event, "event_id": event_id, "event_handler_id": "208", "ts": "1790784385",
        "auth": {"domain": "example.com", "application_token": "fake-token"},
        "data": {"FIELDS_AFTER": fields, "FIELDS_BEFORE": "undefined", "IS_ACCESSIBLE_AFTER": "undefined", "IS_ACCESSIBLE_BEFORE": "N"},
    }


@pytest.fixture
def ingress(monkeypatch):
    from plugins.bitrix24.dedup import InProcessEventDeduper
    from plugins.bitrix24.handoff import _agent_request_from_result
    from plugins.bitrix24.ingress import _make_webhook_ingress_handler

    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/78/fake")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "fake-token")
    monkeypatch.setenv("BITRIX24_BOT_MENTION", "@tux")
    monkeypatch.setenv("BITRIX24_ALLOWED_GROUP_IDS", "52,54")
    source = FakeEventSource()
    source.task = TaskFacts("1154", "Задача", "Описание", "5", "6", "99", "52")
    handler = _make_webhook_ingress_handler(
        event_source=source, bot_user_id="78", deduper=InProcessEventDeduper(),
        agent_request_from_result=_agent_request_from_result,
    )
    return source, handler


@pytest.mark.parametrize("author,text,mode,mentioned,reason", [
    ("5", "просто коммент", None, False, "not_targeted"),
    ("5", "Привет @TUX", "human_comment", True, None),
    ("78", "Привет @tux", "bot_mention", True, None),
    ("78", "[USER=78]Tux Plastinkabot[/USER]", None, False, "not_targeted"),
    ("5", "[USER=78]Tux Plastinkabot[/USER]", None, False, "not_targeted"),
    ("78", "просто коммент", None, False, "not_targeted"),
    ("0", "X создал задачу @tux", None, True, "system_message"),
])
@pytest.mark.parametrize("event", ["ONTASKCOMMENTADD", "ONTASKCOMMENTUPDATE"])
def test_ingress_author_policy(ingress, author, text, mode, mentioned, reason, event):
    source, handler = ingress
    source.message = MessageFacts("201404", author, text)
    result = json.loads(handler(real_payload(event)))
    if reason:
        assert result["accepted"] is False
        assert result["reason"] == reason
        assert "agent_request" not in result
    else:
        assert result["accepted"] is True
        assert result["targeting"]["mode"] == mode
        assert result["mentioned"] is mentioned
        assert result["message"] == text
        assert result["message_id"] == "201404"
        assert result["author_id"] == author
        assert result["normalized_turn"]["object_id"] == "201404"
        assert result["task_title"] == "Задача"
        assert result["agent_request"]["task"]["title"] == "Задача"
        assert result["agent_request"]["comments"][0]["message"] == text


@pytest.mark.parametrize("event,actor", [("ONTASKADD", "5"), ("ONTASKUPDATE", "6")])
def test_ingress_task_real_shape(ingress, event, actor):
    source, handler = ingress
    source.task = TaskFacts("1154", "Задача", "@tux Описание", actor, actor, "99", "52")
    result = json.loads(handler(real_payload(event)))
    assert result["accepted"] is False
    assert result["reason"] == "not_targeted"
    assert "agent_request" not in result
    assert source.calls == []


@pytest.mark.parametrize("missing,reason", [("task", "task_not_found"), ("chat", "task_not_found"), ("message", "message_not_found")])
def test_ingress_missing_facts(ingress, missing, reason):
    source, handler = ingress
    if missing == "task":
        source.task = None
    elif missing == "chat":
        source.task = TaskFacts("1154", "Задача", "", "5", "6", "", "52")
    else:
        source.message = None
    result = json.loads(handler(real_payload()))
    assert result["accepted"] is False
    assert result["reason"] == reason


@pytest.mark.parametrize("allowed,group_id", [("52,54", "17"), ("52,54", ""), ("", "52")])
def test_ingress_rejects_task_outside_allowed_groups(ingress, monkeypatch, allowed, group_id):
    source, handler = ingress
    monkeypatch.setenv("BITRIX24_ALLOWED_GROUP_IDS", allowed)
    source.task = TaskFacts("1154", "Задача", "", "5", "6", "99", group_id)
    source.message = MessageFacts("201404", "5", "@tux сделай")
    result = json.loads(handler(real_payload()))
    assert result["accepted"] is False
    assert result["reason"] == "group_not_allowed"
    assert "agent_request" not in result
    assert ("message", "99", "201404") not in source.calls


@pytest.mark.parametrize("group_id", ["52", "54"])
def test_ingress_accepts_task_in_allowed_group(ingress, group_id):
    source, handler = ingress
    source.task = TaskFacts("1154", "Задача", "", "5", "6", "99", group_id)
    source.message = MessageFacts("201404", "5", "@tux сделай")
    assert json.loads(handler(real_payload()))["accepted"] is True


def test_ingress_unavailable_does_not_dedup_recovery(ingress):
    source, handler = ingress
    source.unavailable = True
    payload = real_payload()
    assert json.loads(handler(payload))["reason"] == "source_unavailable"
    source.unavailable = False
    source.message = MessageFacts("201404", "5", "@tux коммент")
    assert json.loads(handler(payload))["accepted"] is True
    assert json.loads(handler(payload))["reason"] == "duplicate_event"
