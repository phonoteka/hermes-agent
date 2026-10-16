from __future__ import annotations

import json
from urllib.parse import urlencode

import pytest

@pytest.fixture(autouse=True)
def fake_event_source(monkeypatch):
    from plugins.bitrix24 import ingress
    from tests.plugins.test_bitrix24_event_enrichment import FakeEventSource

    source = FakeEventSource('@hermes take a look')
    monkeypatch.setattr(ingress, "RestBitrix24EventSource", lambda client: source)
    monkeypatch.setenv("BITRIX24_ALLOWED_GROUP_IDS", "52,54")
    return source


from plugins.bitrix24.dedup import InProcessEventDeduper
from plugins.bitrix24.http import parse_bitrix24_webhook_payload
from plugins.bitrix24.ingress import _make_webhook_ingress_handler


EXPECTED_DOMAIN = "example.com"
EXPECTED_TOKEN = "expected-app-token"


@pytest.fixture
def ingress_handler(monkeypatch):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", EXPECTED_TOKEN)
    monkeypatch.setenv("BITRIX24_BOT_MENTION", "@hermes")
    return _make_webhook_ingress_handler(deduper=InProcessEventDeduper())


def test_parse_bitrix24_webhook_payload_normalizes_json_and_preserves_event_id():
    raw_body = json.dumps(
        {
            "event_id": "evt-json-1",
            "event": "ONTASKADD",
            "auth": {"domain": EXPECTED_DOMAIN, "application_token": EXPECTED_TOKEN},
            "data": {"FIELDS_AFTER": {"ID": "42"}},
        }
    ).encode("utf-8")

    payload = parse_bitrix24_webhook_payload(raw_body, "application/json; charset=utf-8")

    assert payload == {
        "event_id": "evt-json-1",
        "event": "ONTASKADD",
        "auth": {"domain": EXPECTED_DOMAIN, "application_token": EXPECTED_TOKEN},
        "data": {"FIELDS_AFTER": {"ID": "42"}},
    }


def test_parse_bitrix24_webhook_payload_normalizes_form_encoded_nested_keys():
    raw_body = urlencode(
        [
            ("event", "ONTASKCOMMENTADD"),
            ("ts", "1719050000"),
            ("event_handler_id", "handler-1"),
            ("auth[domain]", EXPECTED_DOMAIN),
            ("auth[application_token]", EXPECTED_TOKEN),
            ("auth[member_id]", "member-77"),
            ("data[FIELDS_AFTER][ID]", "0"),
            ("data[FIELDS_AFTER][TASK_ID]", "42"),
            ("data[FIELDS_AFTER][MESSAGE_ID]", "84"),
        ]
    ).encode("utf-8")

    payload = parse_bitrix24_webhook_payload(raw_body, "application/x-www-form-urlencoded")

    assert payload["event"] == "ONTASKCOMMENTADD"
    assert payload["auth"] == {
        "domain": EXPECTED_DOMAIN,
        "application_token": EXPECTED_TOKEN,
        "member_id": "member-77",
    }
    assert payload["data"] == {
        "FIELDS_AFTER": {
            "ID": "0",
            "TASK_ID": "42",
            "MESSAGE_ID": "84",
        },
    }
    assert payload["event_id"].startswith("b24_")


def test_parse_bitrix24_webhook_payload_derives_retry_stable_event_id():
    pairs = [
        ("event", "ONTASKCOMMENTADD"),
        ("ts", "1719050000"),
        ("event_handler_id", "handler-1"),
        ("auth[domain]", EXPECTED_DOMAIN),
        ("auth[application_token]", EXPECTED_TOKEN),
        ("auth[member_id]", "member-77"),
        ("data[FIELDS_AFTER][ID]", "0"),
        ("data[FIELDS_AFTER][TASK_ID]", "42"),
        ("data[FIELDS_AFTER][MESSAGE_ID]", "84"),
    ]
    same_retry_reordered = list(reversed(pairs))
    changed_task = [(key, "77" if key == "data[FIELDS_AFTER][TASK_ID]" else value) for key, value in pairs]
    changed_message = [(key, "85" if key == "data[FIELDS_AFTER][MESSAGE_ID]" else value) for key, value in pairs]

    first = parse_bitrix24_webhook_payload(urlencode(pairs).encode("utf-8"), "application/x-www-form-urlencoded")
    second = parse_bitrix24_webhook_payload(
        urlencode(same_retry_reordered).encode("utf-8"),
        "application/x-www-form-urlencoded",
    )
    different_task = parse_bitrix24_webhook_payload(
        urlencode(changed_task).encode("utf-8"),
        "application/x-www-form-urlencoded",
    )
    different_message = parse_bitrix24_webhook_payload(
        urlencode(changed_message).encode("utf-8"),
        "application/x-www-form-urlencoded",
    )

    assert first["event_id"] == second["event_id"]
    assert first["event_id"] != different_task["event_id"]
    assert first["event_id"] != different_message["event_id"]


def test_parse_bitrix24_webhook_payload_errors_do_not_echo_token_or_raw_secret():
    secret_token = "super-secret-app-token"
    raw_body = (
        '{"event":"ONTASKADD","auth":{"domain":"example.com","application_token":"'
        + secret_token
        + '"},"data":'
    ).encode("utf-8")

    with pytest.raises(ValueError) as excinfo:
        parse_bitrix24_webhook_payload(raw_body, "application/json")

    message = str(excinfo.value)
    assert message == "Invalid Bitrix24 webhook payload"
    assert secret_token not in message
    assert raw_body.decode("utf-8", errors="ignore") not in message


def test_parse_bitrix24_webhook_payload_output_is_accepted_by_ingress_handler(ingress_handler):
    raw_body = urlencode(
        [
            ("event", "ONTASKCOMMENTADD"),
            ("ts", "1719050000"),
            ("event_handler_id", "handler-1"),
            ("auth[domain]", EXPECTED_DOMAIN),
            ("auth[application_token]", EXPECTED_TOKEN),
            ("auth[member_id]", "member-77"),
            ("data[FIELDS_AFTER][ID]", "0"),
            ("data[FIELDS_AFTER][TASK_ID]", "42"),
            ("data[FIELDS_AFTER][MESSAGE_ID]", "84"),
        ]
    ).encode("utf-8")

    payload = parse_bitrix24_webhook_payload(raw_body, "application/x-www-form-urlencoded")
    result = json.loads(ingress_handler(payload, task_id="build-fix"))

    assert result["accepted"] is True
    assert result["reason"] == "targeted_event"
    assert result["event"] == "ONTASKCOMMENTADD"
    assert result["event_id"] == payload["event_id"]
    assert result["source_domain"] == EXPECTED_DOMAIN
    assert result["payload"]["data"]["FIELDS_AFTER"] == {
        "ID": "0", "TASK_ID": "42", "MESSAGE_ID": "84",
    }
