import asyncio
import hashlib
import json
import logging
import os
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter

EXPECTED_DOMAIN = "example.com"
EXPECTED_TOKEN = "expected-app-token"
EXPECTED_WEBHOOK_URL = "https://example.com/rest/1/test"
DOWNLOAD_URL = "https://files.example.com/download/secret.bin"


def _make_adapter(*, bitrix24_enabled: bool, route_path: str = "/bitrix24/webhook", capture_path=None) -> WebhookAdapter:
    config = PlatformConfig(
        enabled=True,
        extra={
            "host": "127.0.0.1",
            "port": 0,
            "rate_limit": 30,
            "routes": {
                "test-route": {
                    "secret": "generic-secret",
                    "prompt": "Generic webhook: {event}",
                    "deliver": "log",
                }
            },
            "bitrix24": {
                "enabled": bitrix24_enabled,
                "route_path": route_path,
                "deliver": "log",
            },
        },
    )
    if capture_path is not None:
        config.extra["bitrix24"]["capture_path"] = capture_path
    return WebhookAdapter(config)


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    if adapter._bitrix24_config.get("enabled") is True:
        app.router.add_post(adapter._bitrix24_route_path(), adapter._handle_bitrix24_webhook)
    return app


def _bitrix_payload(*, token: str = EXPECTED_TOKEN, domain: str = EXPECTED_DOMAIN, event: str = "ONTASKCOMMENTADD", event_id: str = "evt-accepted-1", description: str = "@hermes review task", title: str = "Webhook smoke") -> dict:
    fields: dict[str, object] = {"ID": "42", "DOWNLOAD_URL": DOWNLOAD_URL}
    if event in {"ONTASKCOMMENTADD", "ONTASKCOMMENTUPDATE"}:
        fields = {"ID": "0", "TASK_ID": "42", "MESSAGE_ID": "84", "DOWNLOAD_URL": DOWNLOAD_URL}
    data: dict[str, object] = {"FIELDS_AFTER": fields, "FIELDS_BEFORE": "undefined", "IS_ACCESSIBLE_AFTER": "undefined", "IS_ACCESSIBLE_BEFORE": "N"}
    return {
        "event_id": event_id,
        "event": event,
        "auth": {
            "domain": domain,
            "application_token": token,
            "member_id": "member-77",
        },
        "data": data,
    }


@pytest.fixture
def bitrix_env(monkeypatch):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", EXPECTED_WEBHOOK_URL)
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", EXPECTED_TOKEN)
    monkeypatch.setenv("BITRIX24_BOT_MENTION", "@hermes")
    from plugins.bitrix24 import ingress
    from tests.plugins.test_bitrix24_event_enrichment import FakeEventSource
    source = FakeEventSource()
    monkeypatch.setattr(ingress, "RestBitrix24EventSource", lambda client: source)
    return source


async def _capturing_client(adapter: WebhookAdapter):
    captured = []

    async def _capture(event):
        captured.append(event)

    adapter.handle_message = _capture
    return TestClient(TestServer(_create_app(adapter))), captured


@pytest.mark.asyncio
async def test_bitrix24_route_not_registered_when_disabled(bitrix_env):
    adapter = _make_adapter(bitrix24_enabled=False)
    client, _ = await _capturing_client(adapter)

    async with client as cli:
        resp = await cli.post("/bitrix24/webhook", json=_bitrix_payload())
        assert resp.status == 404


@pytest.mark.asyncio
async def test_bitrix24_dedicated_route_accepts_unsigned_payload_and_sanitizes_event(bitrix_env):
    adapter = _make_adapter(bitrix24_enabled=True)
    client, captured = await _capturing_client(adapter)

    async with client as cli:
        resp = await cli.post("/bitrix24/webhook", json=_bitrix_payload())
        assert resp.status == 202
        data = await resp.json()
        assert data["status"] == "accepted"
        assert data["route"] == "bitrix24"
        await asyncio.sleep(0)

    assert len(captured) == 1
    event = captured[0]
    assert event.source.chat_id == "bitrix24:evt-accepted-1"
    assert event.source.chat_name == "bitrix24/webhook"
    assert event.source.user_id == "bitrix24"
    assert event.source.user_name == "bitrix24"
    assert event.message_id == "evt-accepted-1"
    assert event.raw_message["event_id"] == "evt-accepted-1"
    assert event.raw_message["task_title"] == "Webhook smoke"
    assert event.raw_message["payload"]["data"]["FIELDS_AFTER"]["TASK_ID"] == "42"
    assert '"auth"' not in json.dumps(event.raw_message)
    assert EXPECTED_TOKEN not in json.dumps(event.raw_message)
    assert EXPECTED_WEBHOOK_URL not in json.dumps(event.raw_message)
    assert "DOWNLOAD_URL" not in json.dumps(event.raw_message)
    assert DOWNLOAD_URL not in json.dumps(event.raw_message)
    assert '"auth"' not in event.text
    assert EXPECTED_TOKEN not in event.text
    assert EXPECTED_WEBHOOK_URL not in event.text
    assert "DOWNLOAD_URL" not in event.text
    assert DOWNLOAD_URL not in event.text


@pytest.mark.asyncio
async def test_generic_webhook_route_still_rejects_unsigned_requests(bitrix_env):
    adapter = _make_adapter(bitrix24_enabled=True)
    client, captured = await _capturing_client(adapter)

    raw_body = json.dumps(_bitrix_payload()).encode("utf-8")
    async with client as cli:
        resp = await cli.post(
            "/webhooks/test-route",
            data=raw_body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 401
        await asyncio.sleep(0)

    assert captured == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "env", "expected_status", "expected_reason"),
    [
        (_bitrix_payload(token="wrong-token", event_id="evt-bad-token"), None, 401, "unauthorized_application_token"),
        (_bitrix_payload(domain="evil.example", event_id="evt-bad-domain"), None, 403, "unauthorized_source"),
        (_bitrix_payload(event="ONUNKNOWN", event_id="evt-unsupported"), None, 200, "unsupported_event"),
        (_bitrix_payload(event="ONTASKCOMMENTADD", event_id="evt-not-targeted", description="please review without mention"), None, 200, "not_targeted"),
        (_bitrix_payload(event_id="evt-bad-url"), {"BITRIX24_WEBHOOK_URL": "https://example.com/rest/api/1/test"}, 503, "invalid_webhook_url"),
        (_bitrix_payload(event_id="evt-no-token"), {"BITRIX24_APPLICATION_TOKEN": ""}, 503, "webhook_auth_not_configured"),
    ],
)
async def test_bitrix24_route_maps_ingress_rejections_without_dispatch(bitrix_env, monkeypatch, payload, env, expected_status, expected_reason):
    if expected_reason == "not_targeted":
        from plugins.bitrix24.enrichment import MessageFacts
        bitrix_env.message = MessageFacts("84", "5", "please review without mention")
    if env:
        for key, value in env.items():
            monkeypatch.setenv(key, value)

    adapter = _make_adapter(bitrix24_enabled=True)
    client, captured = await _capturing_client(adapter)

    async with client as cli:
        resp = await cli.post("/bitrix24/webhook", json=payload)
        assert resp.status == expected_status
        data = await resp.json()
        if expected_status == 200:
            assert data["status"] == "ignored"
            assert data["reason"] == expected_reason
        else:
            assert data["error"] == expected_reason
        await asyncio.sleep(0)

    assert captured == []


@pytest.mark.asyncio
async def test_bitrix24_route_acks_project_not_allowed_without_dispatch(bitrix_env, monkeypatch):
    import plugins.bitrix24.ingress as bitrix_ingress

    adapter = _make_adapter(bitrix24_enabled=True)
    client, captured = await _capturing_client(adapter)

    monkeypatch.setattr(
        bitrix_ingress,
        "_make_webhook_ingress_handler",
        lambda **kwargs: (lambda payload: json.dumps({"accepted": False, "reason": "project_not_allowed"})),
    )

    async with client as cli:
        resp = await cli.post("/bitrix24/webhook", json=_bitrix_payload(event_id="evt-project-not-allowed"))
        assert resp.status == 200
        data = await resp.json()
        assert data == {"status": "ignored", "reason": "project_not_allowed"}
        await asyncio.sleep(0)

    assert captured == []


@pytest.mark.asyncio
async def test_bitrix24_route_logs_safe_auth_rejection_diagnostics(bitrix_env, caplog):
    adapter = _make_adapter(bitrix24_enabled=True)
    client, captured = await _capturing_client(adapter)
    payload = _bitrix_payload(token="wrong-token", event_id="evt-bad-token-log")
    payload["event_handler_id"] = "handler-77"
    expected_digest = hashlib.sha256(EXPECTED_TOKEN.encode("utf-8")).hexdigest()[:12]
    received_digest = hashlib.sha256("wrong-token".encode("utf-8")).hexdigest()[:12]

    with caplog.at_level(logging.WARNING, logger="gateway.platforms.webhook"):
        async with client as cli:
            resp = await cli.post(
                "/bitrix24/webhook",
                json=payload,
                headers={"User-Agent": "Bitrix24 Webhook Engine"},
            )
            assert resp.status == 401
            await asyncio.sleep(0)

    assert captured == []
    log_text = caplog.text
    assert "Bitrix24 auth rejected" in log_text
    assert "event_handler_id=handler-77" in log_text
    assert "token_present=True" in log_text
    assert f"token_sha12={received_digest}" in log_text
    assert f"expected_sha12={expected_digest}" in log_text
    assert "Bitrix24 Webhook Engine" in log_text
    assert "wrong-token" not in log_text
    assert EXPECTED_TOKEN not in log_text


@pytest.mark.asyncio
async def test_bitrix24_route_can_sync_captured_application_token_without_logging_raw_secret(
    bitrix_env, tmp_path, monkeypatch, caplog
):
    adapter = _make_adapter(bitrix24_enabled=True)
    client, captured = await _capturing_client(adapter)
    captured_token = "x" * 32
    handler_id = "208"
    token_digest = hashlib.sha256(captured_token.encode("utf-8")).hexdigest()[:12]
    hermes_env = tmp_path / "hermes.env"
    backend_env = tmp_path / "backend.env"
    hermes_env.write_text(
        "BITRIX24_APPLICATION_TOKEN=old-token\n"
        "BITRIX24_CAPTURE_APPLICATION_TOKEN_ON_AUTH_REJECT=1\n"
    )
    backend_env.write_text("BITRIX24_APPLICATION_TOKEN=old-token\n")

    monkeypatch.setenv("BITRIX24_CAPTURE_APPLICATION_TOKEN_ON_AUTH_REJECT", "1")
    monkeypatch.setenv("BITRIX24_CAPTURE_EVENT_HANDLER_ID", handler_id)
    monkeypatch.setenv("BITRIX24_CAPTURE_DOMAIN", EXPECTED_DOMAIN)
    monkeypatch.setenv("BITRIX24_CAPTURE_TOKEN_SHA12", token_digest)
    monkeypatch.setenv(
        "BITRIX24_TOKEN_SYNC_ENV_FILES",
        f"{hermes_env}{os.pathsep}{backend_env}",
    )
    payload = _bitrix_payload(
        token=captured_token,
        event="ONTASKCOMMENTADD",
        event_id="evt-captured-token-sync",
        description="@hermes sync this token",
    )
    payload["event_handler_id"] = handler_id

    with caplog.at_level(logging.INFO, logger="gateway.platforms.webhook"):
        async with client as cli:
            resp = await cli.post(
                "/bitrix24/webhook",
                json=payload,
                headers={"User-Agent": "Bitrix24 Webhook Engine"},
            )
            assert resp.status == 202
            await asyncio.sleep(0)

    assert len(captured) == 1
    assert f"BITRIX24_APPLICATION_TOKEN={captured_token}" in hermes_env.read_text()
    assert f"BITRIX24_APPLICATION_TOKEN={captured_token}" in backend_env.read_text()
    assert "BITRIX24_CAPTURE_APPLICATION_TOKEN_ON_AUTH_REJECT=0" in hermes_env.read_text()
    assert "BITRIX24_CAPTURE_APPLICATION_TOKEN_ON_AUTH_REJECT=0" in backend_env.read_text()
    assert os.environ["BITRIX24_APPLICATION_TOKEN"] == captured_token
    assert "Bitrix24 application token synchronized" in caplog.text
    assert captured_token not in caplog.text
    assert EXPECTED_TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_bitrix24_route_parses_form_payload_and_dedupes(bitrix_env):
    adapter = _make_adapter(bitrix24_enabled=True)
    client, captured = await _capturing_client(adapter)

    form_body = urlencode(
        [
            ("event", "ONTASKCOMMENTADD"),
            ("event_id", "evt-form-dup"),
            ("ts", "1719050000"),
            ("event_handler_id", "handler-1"),
            ("auth[domain]", EXPECTED_DOMAIN),
            ("auth[application_token]", EXPECTED_TOKEN),
            ("auth[member_id]", "member-77"),
            ("data[FIELDS_AFTER][ID]", "0"),
            ("data[FIELDS_AFTER][TASK_ID]", "42"),
            ("data[FIELDS_AFTER][MESSAGE_ID]", "84"),
            ("data[FIELDS_AFTER][DOWNLOAD_URL]", DOWNLOAD_URL),
        ]
    ).encode("utf-8")

    async with client as cli:
        first = await cli.post(
            "/bitrix24/webhook",
            data=form_body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert first.status == 202
        duplicate = await cli.post(
            "/bitrix24/webhook",
            data=form_body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert duplicate.status == 200
        duplicate_data = await duplicate.json()
        assert duplicate_data == {"status": "ignored", "reason": "duplicate_event"}
        await asyncio.sleep(0)

    assert len(captured) == 1
    event = captured[0]
    assert event.message_id == "evt-form-dup"
    assert '"auth"' not in event.text
    assert "application_token" not in event.text
    assert EXPECTED_TOKEN not in event.text
    assert "DOWNLOAD_URL" not in event.text
    assert DOWNLOAD_URL not in event.text


@pytest.mark.asyncio
async def test_bitrix24_route_returns_400_for_parse_errors(bitrix_env):
    adapter = _make_adapter(bitrix24_enabled=True)
    client, captured = await _capturing_client(adapter)

    async with client as cli:
        resp = await cli.post(
            "/bitrix24/webhook",
            data=b'{"event":"ONTASKADD","auth":',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data == {"error": "Cannot parse body"}
        await asyncio.sleep(0)

    assert captured == []


@pytest.mark.asyncio
async def test_bitrix24_capture_accepted_payload(bitrix_env, tmp_path):
    capture_path = tmp_path / "nested" / "capture.jsonl"
    adapter = _make_adapter(bitrix24_enabled=True, capture_path=str(capture_path))
    client, dispatched = await _capturing_client(adapter)
    from dataclasses import replace
    bitrix_env.task = replace(bitrix_env.task, title="Задача")
    payload = _bitrix_payload(event_id="evt-capture-accepted", title="Задача")
    payload["data"]["nested"] = [{
        "auth": {"domain": "hidden"}, "access_token": "hidden-token",
        "client_secret": "hidden-secret", "PASSWORD": "hidden-password",
        "sourceUrl": "hidden-url", "ID": 17, "message": "Оставить текст",
    }]
    raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    async with client as cli:
        resp = await cli.post(
            "/bitrix24/webhook", data=raw_body,
            headers={"Content-Type": "application/json", "User-Agent": "Bitrix24 diagnostic test"},
        )
        assert resp.status == 202
        assert await resp.json() == {
            "status": "accepted", "route": "bitrix24", "event": "ONTASKCOMMENTADD",
            "delivery_id": "evt-capture-accepted",
        }
        await asyncio.sleep(0)
    lines = capture_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert set(record) == {"ts", "status", "reason", "content_type", "user_agent", "body_bytes", "payload"}
    assert record["ts"].endswith("Z")
    assert record["status"] == 202
    assert record["reason"] is None
    assert record["content_type"] == "application/json"
    assert record["user_agent"] == "Bitrix24 diagnostic test"
    assert record["body_bytes"] == len(raw_body)
    assert record["payload"]["event"] == "ONTASKCOMMENTADD"
    assert record["payload"]["auth"] == {
        "domain": EXPECTED_DOMAIN, "application_token": "<redacted>", "member_id": "<redacted>",
    }
    assert record["payload"]["data"]["FIELDS_AFTER"]["DOWNLOAD_URL"] == "<redacted>"
    assert record["payload"]["data"]["FIELDS_AFTER"]["TASK_ID"] == "42"
    assert record["payload"]["data"]["nested"] == [{
        "auth": "<redacted>", "access_token": "<redacted>", "client_secret": "<redacted>",
        "PASSWORD": "<redacted>", "sourceUrl": "<redacted>", "ID": 17, "message": "Оставить текст",
    }]
    assert EXPECTED_TOKEN not in lines[0]
    assert DOWNLOAD_URL not in lines[0]
    assert len(dispatched) == 1
    assert dispatched[0].raw_message["task_title"] == "Задача"
    assert payload["auth"]["application_token"] == EXPECTED_TOKEN


@pytest.mark.asyncio
async def test_bitrix24_capture_real_form_shape(bitrix_env, tmp_path):
    capture_path = tmp_path / "capture.jsonl"
    adapter = _make_adapter(bitrix24_enabled=True, capture_path=str(capture_path))
    client, dispatched = await _capturing_client(adapter)
    body = urlencode([
        ("event", "ONTASKCOMMENTADD"), ("data[FIELDS_AFTER][ID]", "0"),
        ("data[FIELDS_AFTER][TASK_ID]", "42"), ("data[FIELDS_AFTER][MESSAGE_ID]", "84"), ("auth[domain]", EXPECTED_DOMAIN),
        ("auth[application_token]", EXPECTED_TOKEN),
    ]).encode("utf-8")
    async with client as cli:
        resp = await cli.post(
            "/bitrix24/webhook", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status == 202
        assert (await resp.json())["status"] == "accepted"
        await asyncio.sleep(0)
    text = capture_path.read_text(encoding="utf-8")
    assert len(text.splitlines()) == 1
    record = json.loads(text)
    assert record["status"] == 202
    assert record["reason"] is None
    assert record["payload"]["data"]["FIELDS_AFTER"]["TASK_ID"] == "42"
    assert record["payload"]["data"]["FIELDS_AFTER"]["MESSAGE_ID"] == "84"
    assert record["payload"]["auth"]["application_token"] == "<redacted>"
    assert record["body_bytes"] == len(body)
    assert EXPECTED_TOKEN not in text
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_bitrix24_capture_unparseable_body(bitrix_env, tmp_path):
    capture_path = tmp_path / "capture.jsonl"
    adapter = _make_adapter(bitrix24_enabled=True, capture_path=str(capture_path))
    client, dispatched = await _capturing_client(adapter)
    raw_body = b'{"event":"ONTASKADD","auth":'
    async with client as cli:
        resp = await cli.post(
            "/bitrix24/webhook", data=raw_body, headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert await resp.json() == {"error": "Cannot parse body"}
    record = json.loads(capture_path.read_text(encoding="utf-8"))
    assert record["payload"] is None
    assert record["status"] == 400
    assert record["reason"] == "Cannot parse body"
    assert record["body_bytes"] == len(raw_body)
    assert dispatched == []


@pytest.mark.asyncio
async def test_bitrix24_capture_wrong_token(bitrix_env, tmp_path):
    capture_path = tmp_path / "capture.jsonl"
    adapter = _make_adapter(bitrix24_enabled=True, capture_path=str(capture_path))
    client, dispatched = await _capturing_client(adapter)
    async with client as cli:
        resp = await cli.post(
            "/bitrix24/webhook", json=_bitrix_payload(token="wrong-token", event_id="evt-capture-wrong"),
        )
        assert resp.status == 401
        assert await resp.json() == {"error": "unauthorized_application_token"}
    text = capture_path.read_text(encoding="utf-8")
    record = json.loads(text)
    assert record["status"] == 401
    assert record["reason"] == "unauthorized_application_token"
    assert record["payload"]["auth"]["application_token"] == "<redacted>"
    assert "wrong-token" not in text
    assert dispatched == []


@pytest.mark.asyncio
async def test_bitrix24_capture_disabled(bitrix_env, tmp_path):
    adapter = _make_adapter(bitrix24_enabled=True)
    client, dispatched = await _capturing_client(adapter)
    existing_files = set(tmp_path.rglob("*"))
    async with client as cli:
        resp = await cli.post("/bitrix24/webhook", json=_bitrix_payload(event_id="evt-capture-disabled"))
        assert resp.status == 202
        assert await resp.json() == {
            "status": "accepted", "route": "bitrix24", "event": "ONTASKCOMMENTADD",
            "delivery_id": "evt-capture-disabled",
        }
        await asyncio.sleep(0)
    assert set(tmp_path.rglob("*")) == existing_files
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_bitrix24_capture_write_failure_keeps_response(bitrix_env, tmp_path, caplog):
    parent = tmp_path / "regular-file"
    parent.write_text("do not change", encoding="utf-8")
    adapter = _make_adapter(bitrix24_enabled=True, capture_path=str(parent / "capture.jsonl"))
    client, dispatched = await _capturing_client(adapter)
    with caplog.at_level(logging.WARNING, logger="gateway.platforms.webhook"):
        async with client as cli:
            resp = await cli.post("/bitrix24/webhook", json=_bitrix_payload(event_id="evt-capture-write-failure"))
            assert resp.status == 202
            assert await resp.json() == {
                "status": "accepted", "route": "bitrix24", "event": "ONTASKCOMMENTADD",
                "delivery_id": "evt-capture-write-failure",
            }
            await asyncio.sleep(0)
    warnings = [record for record in caplog.records if "Bitrix24 capture failed" in record.message]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert EXPECTED_TOKEN not in caplog.text
    assert parent.read_text(encoding="utf-8") == "do not change"
    assert len(dispatched) == 1


def _real_comment_form(message_id: str = "84") -> bytes:
    return urlencode([
        ("event", "ONTASKCOMMENTADD"), ("event_handler_id", "208"),
        ("data[FIELDS_AFTER][ID]", "0"), ("data[FIELDS_AFTER][MESSAGE_ID]", message_id),
        ("data[FIELDS_AFTER][TASK_ID]", "42"), ("data[FIELDS_BEFORE]", "undefined"),
        ("data[IS_ACCESSIBLE_AFTER]", "undefined"), ("data[IS_ACCESSIBLE_BEFORE]", "N"),
        ("ts", "1790784385"), ("auth[domain]", EXPECTED_DOMAIN),
        ("auth[application_token]", EXPECTED_TOKEN), ("auth[member_id]", "member-77"),
    ]).encode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("author_id", "text", "message_id", "expected_status", "expected_body", "dispatched_mode"),
    [
        ("5", "@hermes посмотри", "201", 202, None, "human_comment"),
        ("1", "@hermes отчёт готов", "202", 202, None, "bot_mention"),
        ("5", "просто коммент", "203", 200, {"status": "ignored", "reason": "not_targeted"}, None),
        ("5", "[USER=1]Bot[/USER] без короткого тега", "204", 200, {"status": "ignored", "reason": "not_targeted"}, None),
        ("1", "[USER=1]Bot[/USER]", "205", 200, {"status": "ignored", "reason": "not_targeted"}, None),
        ("0", "@hermes создал задачу", "206", 200, {"status": "ignored", "reason": "system_message"}, None),
    ],
)
async def test_bitrix24_route_real_comment_shape_author_and_tag_policy(
    bitrix_env, author_id, text, message_id, expected_status, expected_body, dispatched_mode,
):
    from plugins.bitrix24.enrichment import MessageFacts, TaskFacts
    bitrix_env.task = TaskFacts("42", "Настоящая задача", "", "5", "5", "99")
    bitrix_env.message = MessageFacts(message_id, author_id, text)
    adapter = _make_adapter(bitrix24_enabled=True)
    client, captured = await _capturing_client(adapter)
    async with client as cli:
        resp = await cli.post(
            "/bitrix24/webhook", data=_real_comment_form(message_id),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status == expected_status
        if expected_body is not None:
            assert await resp.json() == expected_body
        await asyncio.sleep(0)
    assert ("message", "99", message_id) in bitrix_env.calls
    if dispatched_mode is None:
        assert captured == []
    else:
        assert len(captured) == 1
        prompt = captured[0].text
        assert text in prompt
        assert "Настоящая задача" in prompt
        assert dispatched_mode in prompt
        assert EXPECTED_TOKEN not in prompt


@pytest.mark.asyncio
async def test_bitrix24_route_source_unavailable_is_502_and_retry_is_not_deduped(bitrix_env):
    from plugins.bitrix24.enrichment import MessageFacts
    bitrix_env.message = MessageFacts("301", "5", "@hermes повтори")
    bitrix_env.unavailable = True
    adapter = _make_adapter(bitrix24_enabled=True)
    client, captured = await _capturing_client(adapter)
    body = _real_comment_form("301")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    async with client as cli:
        first = await cli.post("/bitrix24/webhook", data=body, headers=headers)
        assert first.status == 502
        assert await first.json() == {"error": "source_unavailable"}
        bitrix_env.unavailable = False
        retry = await cli.post("/bitrix24/webhook", data=body, headers=headers)
        assert retry.status == 202
        await asyncio.sleep(0)
    assert len(captured) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["ONTASKADD", "ONTASKUPDATE"])
async def test_bitrix24_route_ignores_task_events_even_with_tag(bitrix_env, event):
    """Only tagged comments reach the agent; task create/update never dispatches."""
    adapter = _make_adapter(bitrix24_enabled=True)
    client, dispatched = await _capturing_client(adapter)
    async with client as cli:
        resp = await cli.post("/bitrix24/webhook", json=_bitrix_payload(event=event, event_id=f"evt-{event}"))
        assert resp.status == 200
        assert (await resp.json())["reason"] == "not_targeted"
        await asyncio.sleep(0)
    assert dispatched == []
    assert bitrix_env.calls == []
