from __future__ import annotations

import json

import pytest
import yaml


class _CapturingContext:
    def __init__(self) -> None:
        self.tools: list[dict] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)


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
def ingress_tool(hermes_home):
    from plugins.bitrix24 import register

    ctx = _CapturingContext()
    register(ctx)
    tool = next((tool for tool in ctx.tools if tool["name"] == "bitrix24_webhook_ingress"), None)
    if tool is None:
        pytest.fail(
            "S2b route-back-to-S2a: public ingress seam missing; expected "
            "plugins.bitrix24.register(ctx) to expose bitrix24_webhook_ingress"
        )
    return tool


def _task_payload(
    *,
    event_id: str = "evt-task-1",
    event: str = "ONTASKADD",
    domain: str = "example.com",
    description: str = "@hermes review task",
    title: str | None = "Webhook smoke",
    auth_extra: dict | None = None,
) -> dict:
    auth = {"domain": domain, "application_token": "expected-app-token"}
    if isinstance(auth_extra, dict):
        auth.update(auth_extra)
    fields = {
        "ID": "42",
        "DESCRIPTION": description,
    }
    if title is not None:
        fields["TITLE"] = title
    return {
        "event_id": event_id,
        "event": event,
        "auth": auth,
        "data": {"FIELDS": fields},
    }


def _comment_payload(
    *,
    event_id: str = "evt-comment-1",
    event: str = "ONTASKCOMMENTADD",
    domain: str = "example.com",
    post_message: str = "plain update without bot mention",
) -> dict:
    return {
        "event_id": event_id,
        "event": event,
        "auth": {"domain": domain, "application_token": "expected-app-token"},
        "data": {
            "FIELDS": {
                "ID": "84",
                "TASK_ID": "42",
                "POST_MESSAGE": post_message,
            }
        },
    }


def _chat_payload(
    *,
    event_id: str = "evt-chat-1",
    event: str = "ONIMBOTMESSAGEADD",
    domain: str = "example.com",
    message: str = "@hermes please check the task",
    dialog_id: str | None = "chat99",
) -> dict:
    data = {
        "MESSAGE": message,
        "FIELDS": {
            "ID": "chat-message-7",
            "TASK_ID": "42",
            "MESSAGE": message,
        },
    }
    if dialog_id is not None:
        data["DIALOG_ID"] = dialog_id
    return {
        "event_id": event_id,
        "event": event,
        "auth": {"domain": domain, "application_token": "expected-app-token"},
        "data": data,
    }


def _invoke(tool: dict, payload: dict, *, task_id: str = "build-fix") -> dict:
    return json.loads(tool["handler"](payload, task_id=task_id))


def test_bitrix24_webhook_keeps_s2a_fail_closed_foreign_source(ingress_tool, monkeypatch):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    result = _invoke(ingress_tool, _task_payload(domain="foreign.example"))

    assert set(result) == {"accepted", "reason", "event", "source_domain"}
    assert result["accepted"] is False
    assert result["reason"] == "unauthorized_source"


def test_bitrix24_webhook_rejects_invalid_payload_before_event_checks(ingress_tool, monkeypatch):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    result = json.loads(ingress_tool["handler"](None, task_id="build-fix"))

    assert result == {"accepted": False, "reason": "invalid_payload"}


@pytest.mark.parametrize(
    "raw_url",
    [
        "https://example.com/not-rest-path",
        "https://example.com/rest/api/1/token",
        "https://example.com/rest/1/test/tasks.task.get",
    ],
)
def test_bitrix24_webhook_rejects_invalid_old_rest_webhook_base_before_auth_and_domain_checks(
    ingress_tool,
    monkeypatch,
    raw_url,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", raw_url)
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    result = _invoke(ingress_tool, _task_payload())

    assert result == {
        "accepted": False,
        "reason": "invalid_webhook_url",
        "event": "ONTASKADD",
    }


def test_bitrix24_webhook_rejects_when_application_auth_is_not_configured(
    ingress_tool,
    monkeypatch,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.delenv("BITRIX24_APPLICATION_TOKEN", raising=False)

    result = _invoke(ingress_tool, _task_payload())

    assert result == {
        "accepted": False,
        "reason": "webhook_auth_not_configured",
        "event": "ONTASKADD",
    }


@pytest.mark.parametrize(
    "payload",
    [
        _task_payload(auth_extra={"application_token": ""}),
        _task_payload(auth_extra={"application_token": "wrong-token"}),
        _task_payload() | {"auth": {"domain": "example.com"}},
    ],
)
def test_bitrix24_webhook_rejects_missing_or_mismatched_application_token_without_echo(
    ingress_tool,
    monkeypatch,
    payload,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    result = _invoke(ingress_tool, payload)
    serialized = json.dumps(result)

    assert result == {
        "accepted": False,
        "reason": "unauthorized_application_token",
        "event": "ONTASKADD",
    }
    assert '"application_token":' not in serialized
    assert "expected-app-token" not in serialized
    assert "wrong-token" not in serialized


def test_bitrix24_webhook_rejects_unsupported_event(ingress_tool, monkeypatch):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    result = _invoke(
        ingress_tool,
        {
            "event_id": "evt-unsupported-1",
            "event": "ONCALENDARENTRYADD",
            "auth": {"domain": "example.com", "application_token": "expected-app-token"},
            "data": {"FIELDS": {"ID": "9"}},
        },
    )

    assert result == {
        "accepted": False,
        "reason": "unsupported_event",
        "event": "ONCALENDARENTRYADD",
    }


def test_bitrix24_webhook_rejects_supported_event_without_event_id_as_unknown_shape(
    ingress_tool,
    monkeypatch,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    result = _invoke(
        ingress_tool,
        {
            "event": "ONTASKCOMMENTADD",
            "auth": {"domain": "example.com", "application_token": "expected-app-token"},
            "data": {"FIELDS": {"ID": "84", "TASK_ID": "42", "POST_MESSAGE": "@hermes hi"}},
        },
    )

    assert result == {
        "accepted": False,
        "reason": "unknown_payload_shape",
        "event": "ONTASKCOMMENTADD",
    }


def test_bitrix24_webhook_rejects_bad_supported_event_shape_as_unknown_payload_shape(
    ingress_tool,
    monkeypatch,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    result = _invoke(
        ingress_tool,
        {
            "event_id": "evt-bad-shape-1",
            "event": "ONTASKCOMMENTADD",
            "auth": {"domain": "example.com", "application_token": "expected-app-token"},
            "data": {"FIELDS": {"ID": "84", "TASK_ID": "42"}},
        },
    )

    assert result == {
        "accepted": False,
        "reason": "unknown_payload_shape",
        "event": "ONTASKCOMMENTADD",
        "event_id": "evt-bad-shape-1",
    }


def test_bitrix24_webhook_normalizes_one_targeted_task_event_and_dedups_retries(
    ingress_tool,
    monkeypatch,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    first = _invoke(ingress_tool, _task_payload())
    second = _invoke(ingress_tool, _task_payload())

    assert first["accepted"] is True
    assert first["reason"] == "targeted_event"
    assert first["targeted"] is True
    assert first["deduped"] is False
    assert first["targeting"] == {"mode": "project_rule", "mention": "@hermes"}
    assert first["normalized_turn"] == {
        "source": "bitrix24",
        "event": "ONTASKADD",
        "event_id": "evt-task-1",
        "object_id": "42",
    }
    assert first["turns"] == [first["normalized_turn"]]
    assert second["accepted"] is False
    assert set(second) == {"accepted", "reason", "event", "event_id", "deduped"}
    assert second["reason"] == "duplicate_event"
    assert second["event_id"] == "evt-task-1"
    assert second["deduped"] is True


def test_bitrix24_webhook_dedups_across_two_handler_instances_in_same_process(monkeypatch):
    from plugins.bitrix24.dedup import InProcessEventDeduper
    from plugins.bitrix24.ingress import _make_webhook_ingress_handler

    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    shared_deduper = InProcessEventDeduper()
    first_handler = _make_webhook_ingress_handler(deduper=shared_deduper)
    second_handler = _make_webhook_ingress_handler(deduper=shared_deduper)

    first = json.loads(first_handler(_task_payload(event_id="evt-cross-handler-1"), task_id="build-fix"))
    second = json.loads(second_handler(_task_payload(event_id="evt-cross-handler-1"), task_id="build-fix"))

    assert first["accepted"] is True
    assert first["reason"] == "targeted_event"
    assert second == {
        "accepted": False,
        "reason": "duplicate_event",
        "event": "ONTASKADD",
        "event_id": "evt-cross-handler-1",
        "deduped": True,
    }


def test_bitrix24_webhook_normalizes_targeted_task_update_as_project_rule(
    ingress_tool,
    monkeypatch,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    result = _invoke(ingress_tool, _task_payload(event_id="evt-task-update-1", event="ONTASKUPDATE"))

    assert result["accepted"] is True
    assert result["reason"] == "targeted_event"
    assert result["targeting"]["mode"] == "project_rule"
    assert result["normalized_turn"] == {
        "source": "bitrix24",
        "event": "ONTASKUPDATE",
        "event_id": "evt-task-update-1",
        "object_id": "42",
    }
    assert len(result["turns"]) == 1


def test_bitrix24_webhook_accepts_sparse_task_update_without_title(
    ingress_tool,
    monkeypatch,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    result = _invoke(
        ingress_tool,
        _task_payload(
            event_id="evt-task-update-sparse-1",
            event="ONTASKUPDATE",
            title=None,
            description="description only update",
        ),
    )

    assert result["accepted"] is True
    assert result["event"] == "ONTASKUPDATE"
    assert result["task_id"] == "42"
    assert result["reason"] == "targeted_event"
    assert result["payload"]["data"]["FIELDS"] == {"ID": "42", "DESCRIPTION": "description only update"}


def test_bitrix24_webhook_sanitizes_auth_fields_from_accepted_result(
    ingress_tool,
    monkeypatch,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "fake-secret-token")

    result = _invoke(
        ingress_tool,
        _comment_payload(event_id="evt-comment-secret-1", post_message="@hermes take a look")
        | {"auth": {"domain": "example.com", "application_token": "fake-secret-token"}},
    )

    serialized = json.dumps(result)
    assert result["accepted"] is True
    assert "auth" not in result["payload"]
    assert "application_token" not in serialized
    assert "fake-secret-token" not in serialized
    assert "download_url" not in serialized
    assert "auth" not in json.dumps(result["agent_request"])


def test_bitrix24_webhook_normalizes_targeted_comment_add_and_update_mentions(
    ingress_tool,
    monkeypatch,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    add_result = _invoke(
        ingress_tool,
        _comment_payload(event_id="evt-comment-add-1", post_message="@hermes take a look"),
    )
    update_result = _invoke(
        ingress_tool,
        _comment_payload(
            event_id="evt-comment-update-1",
            event="ONTASKCOMMENTUPDATE",
            post_message="@hermes updated context",
        ),
    )

    assert add_result["accepted"] is True
    assert add_result["reason"] == "targeted_event"
    assert add_result["targeting"]["mode"] == "mention"
    assert add_result["message"] == "@hermes take a look"
    assert add_result["normalized_turn"] == {
        "source": "bitrix24",
        "event": "ONTASKCOMMENTADD",
        "event_id": "evt-comment-add-1",
        "object_id": "84",
    }

    assert update_result["accepted"] is True
    assert update_result["reason"] == "targeted_event"
    assert update_result["targeting"]["mode"] == "mention"
    assert update_result["message"] == "@hermes updated context"
    assert update_result["normalized_turn"] == {
        "source": "bitrix24",
        "event": "ONTASKCOMMENTUPDATE",
        "event_id": "evt-comment-update-1",
        "object_id": "84",
    }
    assert update_result["agent_request"]["source"] == "bitrix24_webhook_ingress"
    assert update_result["agent_request"]["task"]["id"] == "42"
    assert update_result["agent_request"]["comments"][0]["message"] == "@hermes updated context"
    assert update_result["deferred_actions"] == [
        {
            "tool": "bitrix24_task_comment_add",
            "args": {"task_id": "42", "message": ""},
            "deferred": True,
            "reason": "targeted_comment_reply",
            "requires_agent_message": True,
        }
    ]
    assert "download_url" not in json.dumps(update_result["agent_request"])


def test_bitrix24_webhook_normalizes_targeted_chat_mentions(
    ingress_tool,
    monkeypatch,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    result = _invoke(ingress_tool, _chat_payload())

    assert result["accepted"] is True
    assert result["reason"] == "targeted_event"
    assert result["targeting"]["mode"] == "mention"
    assert result["message"] == "@hermes please check the task"
    assert result["normalized_turn"] == {
        "source": "bitrix24",
        "event": "ONIMBOTMESSAGEADD",
        "event_id": "evt-chat-1",
        "object_id": "chat-message-7",
    }
    assert len(result["turns"]) == 1
    assert result["dialog_id"] == "chat99"
    assert result["payload"]["data"]["DIALOG_ID"] == "chat99"
    assert result["agent_request"]["source"] == "bitrix24_webhook_ingress"
    assert result["agent_request"]["task"]["id"] == "42"
    assert result["deferred_actions"] == [
        {
            "tool": "bitrix24_chat_send",
            "args": {"chat_id": "chat99", "message": ""},
            "deferred": True,
            "reason": "targeted_chat_reply",
            "requires_agent_message": True,
        }
    ]


def test_bitrix24_webhook_chat_handoff_skips_reply_candidate_without_explicit_dialog_id(
    ingress_tool,
    monkeypatch,
):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    result = _invoke(ingress_tool, _chat_payload(event_id="evt-chat-no-dialog-1", dialog_id=None))

    assert result["accepted"] is True
    assert result["agent_request"]["source"] == "bitrix24_webhook_ingress"
    assert "dialog_id" not in result
    assert result.get("deferred_actions") is None


def test_bitrix24_webhook_ignores_non_target_comment_events(ingress_tool, monkeypatch):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    result = _invoke(ingress_tool, _comment_payload())

    assert set(result) == {"accepted", "reason", "targeted", "event", "event_id", "source_domain"}
    assert result["accepted"] is False
    assert result["reason"] == "not_targeted"
    assert result["targeted"] is False
