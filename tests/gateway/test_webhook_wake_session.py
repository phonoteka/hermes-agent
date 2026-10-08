"""HTTP falsifiers for waking an existing session without creating a new one.

Authentication and routing use the real webhook owner. The runner retains its
real injection implementation; only storage, authorization and delivery are doubles.
"""

import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform, PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource


SECRET = "isolated-wake-secret"
KEY = "agent:main:telegram:dm:42"
TEXT = "  wake this session\nwithout trimming  "


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))


def _runner(*, unknown=False, ended=False, running=True):
    """Supply the real runner with an exact-key store and captured transport."""
    now = datetime.now()
    entry = SessionEntry(
        session_key=KEY, session_id="session-42", created_at=now,
        updated_at=now, platform=Platform.TELEGRAM,
        origin=SessionSource(platform=Platform.TELEGRAM, chat_id="42",
                             chat_type="dm", user_id="42", user_name="tester"),
    )
    entries = {} if unknown else {KEY: entry}
    store = SimpleNamespace(
        get=MagicMock(side_effect=entries.get),
        lookup_by_session_key=MagicMock(side_effect=entries.get),
        _is_session_ended_in_db=MagicMock(return_value=ended),
        get_or_create_session=MagicMock(),
    )
    target = SimpleNamespace(handle_message=AsyncMock())
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = SimpleNamespace(
        _store=store,
        lookup_by_session_key=AsyncMock(side_effect=entries.get),
        get_or_create_session=AsyncMock(),
    )
    runner._running = running
    runner._draining = False
    runner._background_tasks = set()
    runner.adapters = {Platform.TELEGRAM: target}
    runner._profile_adapters = {}
    runner._is_user_authorized_for_source = MagicMock(return_value=True)
    runner._adapter_for_source = MagicMock(return_value=target)
    return runner, target, entry


@asynccontextmanager
async def _client(*, wake=True, unknown=False, ended=False, running=True):
    """Bind only an ephemeral loopback HTTP listener and close it deterministically."""
    runner, target, entry = _runner(unknown=unknown, ended=ended, running=running)
    route = {"secret": SECRET}
    if wake:
        route["wake_session"] = True
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
        "host": "127.0.0.1", "port": 0, "routes": {"wake": route},
    }))
    adapter.gateway_runner = runner
    ordinary_turns = AsyncMock()
    adapter.handle_message = ordinary_turns
    app = web.Application(client_max_size=adapter._max_body_bytes)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    async with TestClient(TestServer(app)) as client:
        try:
            yield client, adapter, runner, target, entry, ordinary_turns
        finally:
            if adapter._background_tasks:
                await asyncio.gather(*tuple(adapter._background_tasks))


async def _post(client, *, payload=None, bad_signature=False, delivery="delivery-1"):
    payload = {"session_key": KEY, "text": TEXT} if payload is None else payload
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    if bad_signature:
        signature = "sha256=" + "0" * 64
    response = await client.post("/webhooks/wake", data=body, headers={
        "Content-Type": "application/json",
        "X-Hub-Signature-256": signature,
        "X-Request-ID": delivery,
    })
    await response.read()
    return response


@pytest.mark.asyncio
async def test_accepted_wakes_existing_session_without_creation():
    async with _client() as (client, adapter, runner, target, entry, ordinary):
        response = await _post(client)
        assert response.status == 202
        target.handle_message.assert_awaited_once()
        event = target.handle_message.await_args.args[0]
        assert event.text == TEXT
        assert event.internal is True
        assert event.allow_gateway_control is False
        assert event.source == entry.origin
        assert event.metadata["gateway_session_key"] == KEY
        assert event.metadata["gateway_session_id"] == entry.session_id
        assert event.metadata["gateway_session_strict"] is True
        assert event.metadata["hermes_wake_origin"] == "webhook:wake"
        runner.session_store.get_or_create_session.assert_not_called()
        runner._async_session_store.get_or_create_session.assert_not_awaited()
        ordinary.assert_not_awaited()
        duplicate = await _post(client)
        assert duplicate.status == 200
        target.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_accepted_control_ordinary_route_keeps_per_delivery_turn():
    async with _client(wake=False) as (client, adapter, runner, target, entry, ordinary):
        response = await _post(client)
        assert response.status == 202
        if adapter._background_tasks:
            await asyncio.gather(*tuple(adapter._background_tasks))
        ordinary.assert_awaited_once()
        event = ordinary.await_args.args[0]
        assert event.source.chat_id == "webhook:wake:delivery-1"
        assert event.internal is False
        target.handle_message.assert_not_awaited()
        duplicate = await _post(client)
        assert duplicate.status == 200
        ordinary.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("case,expected", [
    ("bad_signature", 401), ("unknown", 404), ("ended", 409),
    ("not_running", 503), ("missing_text", 400),
])
async def test_reject_invalid_wake_without_delivery(case, expected):
    async with _client(unknown=case == "unknown", ended=case == "ended",
                       running=case != "not_running") as state:
        client, adapter, runner, target, entry, ordinary = state
        payload = {"session_key": KEY} if case == "missing_text" else None
        response = await _post(client, payload=payload,
                               bad_signature=case == "bad_signature")
        assert response.status == expected
        if adapter._background_tasks:
            await asyncio.gather(*tuple(adapter._background_tasks))
        target.handle_message.assert_not_awaited()
        ordinary.assert_not_awaited()
        runner.session_store.get_or_create_session.assert_not_called()
        runner._async_session_store.get_or_create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_reject_control_valid_request_returns_202():
    async with _client() as (client, adapter, runner, target, entry, ordinary):
        assert (await _post(client)).status == 202


@pytest.mark.parametrize("conflicting", [
    {"cron_job": "existing-job"},
    {"deliver_only": True, "deliver": "telegram"},
    {"script": "route-transformer.py"},
    {"skills": ["example-skill"]},
    {"prompt": "rendered prompt"},
])
def test_validate_route_conflicting_wake_pipeline_raises(conflicting):
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1"}))
    with pytest.raises(ValueError, match="wake-route"):
        adapter._validate_route("wake-route", {
            "secret": SECRET, "wake_session": True, **conflicting,
        })


def test_validate_route_control_accepts_secret_only_wake():
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1"}))
    assert adapter._validate_route("wake-route", {"secret": SECRET, "wake_session": True}) is None


def test_validate_route_control_requires_secret():
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1"}))
    with pytest.raises(ValueError, match="wake-route"):
        adapter._validate_route("wake-route", {"wake_session": True})
