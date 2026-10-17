"""Bitrix24 plugin platform: one gateway session per task, replies posted back to the task."""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode

import aiohttp
import pytest
import yaml

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionStore, build_session_key
from plugins.bitrix24.enrichment import MessageFacts, TaskFacts

TOKEN = "app-token-test"
DOMAIN = "portal.example.bitrix24.ru"
WEBHOOK = f"https://{DOMAIN}/rest/77/secret-value/"


class _Source:
    """Authoritative task/message facts keyed by message id (no network)."""

    def __init__(self) -> None:
        self.messages: dict[str, tuple[str, str, str]] = {}
        self.groups: dict[str, str] = {}

    def get_task(self, task_id):
        return TaskFacts(task_id, f"Task {task_id}", "", "1", "1", f"9{task_id}", self.groups.get(task_id, "52"))

    def get_task_message(self, chat_id, message_id):
        task_id, author, text = self.messages[message_id]
        return MessageFacts(message_id, author, text)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["bitrix24"]}}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", TOKEN)
    monkeypatch.setenv("BITRIX24_BOT_MENTION", "@tux")
    monkeypatch.setenv("BITRIX24_ALLOWED_GROUP_IDS", "52,54")
    source = _Source()
    from plugins.bitrix24 import ingress
    monkeypatch.setattr(ingress, "RestBitrix24EventSource", lambda client: source)
    from plugins.bitrix24 import dedup
    monkeypatch.setattr(ingress, "get_process_shared_event_deduper", lambda: dedup.InProcessEventDeduper())
    # Gateway order: plugin discovery registers the platform before any adapter is built.
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    import plugins.bitrix24 as bitrix24
    manager = PluginManager()
    bitrix24.register(PluginContext(PluginManifest(name="bitrix24", key="bitrix24", source="bundled"), manager))
    yield SimpleNamespace(home=home, source=source, manager=manager)
    manager.unload()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _comment(event_id: str, task_id: str, message_id: str, token: str = TOKEN) -> bytes:
    return urlencode({
        "event": "ONTASKCOMMENTADD",
        "event_id": event_id,
        "data[FIELDS_AFTER][TASK_ID]": task_id,
        "data[FIELDS_AFTER][MESSAGE_ID]": message_id,
        "data[FIELDS_AFTER][ID]": message_id,
        "auth[application_token]": token,
        "auth[domain]": DOMAIN,
    }).encode()


WORKFLOW = "/srv/canon/solution_modeling_workflow_pack/workflow.json"
EXECUTION_ROOT = "/srv/project"


def _created(event_id: str, task_id: str) -> bytes:
    return urlencode({
        "event": "ONTASKADD",
        "event_id": event_id,
        "data[FIELDS_AFTER][ID]": task_id,
        "auth[application_token]": TOKEN,
        "auth[domain]": DOMAIN,
    }).encode()


async def _adapter(port: int, task_workflow: dict | None = None, code_roots: dict | None = None):
    from plugins.bitrix24.platform import Bitrix24Adapter
    extra = {"host": "127.0.0.1", "port": port}
    if task_workflow is not None:
        extra["task_workflow"] = task_workflow
    if code_roots is not None:
        extra["code_roots"] = code_roots
    adapter = Bitrix24Adapter(PlatformConfig(enabled=True, extra=extra))
    events = []

    async def _handler(event):
        events.append(event)

    adapter.handle_message = AsyncMock(side_effect=_handler)
    assert await adapter.connect() is True
    return adapter, events


async def _post(port: int, body: bytes):
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"http://127.0.0.1:{port}/bitrix24/webhook", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as response:
            return response.status, json.loads(await response.text())


async def _drain(adapter):
    for _ in range(50):
        pending = [t for t in getattr(adapter, "_background_tasks", set()) if not t.done()]
        if not pending:
            return
        await asyncio.sleep(0.01)


def _key(event) -> str:
    cfg = GatewayConfig()
    return build_session_key(
        event.source, group_sessions_per_user=cfg.group_sessions_per_user,
        thread_sessions_per_user=cfg.thread_sessions_per_user,
    )


@pytest.mark.asyncio
async def test_task_comments_share_one_session(env):
    env.source.messages = {
        "501": ("42", "1", "@tux first"),
        "502": ("42", "7", "@tux second from another person"),
        "503": ("43", "1", "@tux other task"),
    }
    port = _free_port()
    adapter, events = await _adapter(port)
    try:
        results = [
            await _post(port, _comment("e1", "42", "501")),
            await _post(port, _comment("e2", "42", "502")),
            await _post(port, _comment("e3", "43", "503")),
        ]
        await _drain(adapter)
    finally:
        await adapter.disconnect()
    assert [status for status, _ in results] == [202, 202, 202]
    assert len(events) == 3
    keys = [_key(event) for event in events]
    assert keys[0] == keys[1]
    assert "task:42" in keys[0]
    assert keys[2] != keys[0] and "task:43" in keys[2]
    assert all(event.source.platform == Platform("bitrix24") for event in events)
    assert {event.source.user_id for event in events[:2]} == {"1", "7"}


@pytest.mark.asyncio
async def test_reject_unauthorized_and_untargeted(env):
    env.source.messages = {"601": ("42", "1", "plain note"), "602": ("42", "1", "@tux go")}
    port = _free_port()
    adapter, events = await _adapter(port)
    try:
        bad = await _post(port, _comment("e10", "42", "602", token="wrong"))
        quiet = await _post(port, _comment("e11", "42", "601"))
        good = await _post(port, _comment("e12", "42", "602"))
        await _drain(adapter)
    finally:
        await adapter.disconnect()
    assert bad == (401, {"error": "unauthorized_application_token"})
    assert quiet == (200, {"status": "ignored", "reason": "not_targeted"})
    assert good[0] == 202
    assert len(events) == 1


@pytest.mark.asyncio
async def test_send_posts_to_task_or_dialog(env, monkeypatch):
    from plugins.bitrix24 import platform as platform_mod
    calls = []

    class _Client:
        def call(self, method, **payload):
            calls.append((method, payload))
            return {"result": 9001}

    monkeypatch.setattr(platform_mod, "Bitrix24Client", lambda *a, **k: _Client())
    adapter = platform_mod.Bitrix24Adapter(PlatformConfig(enabled=True, extra={}))
    task = await adapter.send("task:42", "**Итог** и `code`")
    chat = await adapter.send("chat:chat77", "привет")
    other = await adapter.send("telegram:1", "x")
    assert task.success and chat.success and not other.success
    assert calls == [
        ("task.commentitem.add", {"taskId": "42", "fields": {"POST_MESSAGE": "[B]Итог[/B] и [CODE]code[/CODE]"}}),
        ("im.message.add", {"DIALOG_ID": "chat77", "MESSAGE": "привет"}),
    ]

    class _Broken:
        def call(self, method, **payload):
            raise RuntimeError(f"boom {WEBHOOK}")

    monkeypatch.setattr(platform_mod, "Bitrix24Client", lambda *a, **k: _Broken())
    failed = await adapter.send("task:42", "x")
    assert failed.success is False
    assert "secret-value" not in str(failed.error)


@pytest.mark.asyncio
async def test_register_and_injection_reach_task_session(env, tmp_path):
    from gateway.platform_registry import platform_registry
    from gateway.run import GatewayRunner
    (env.home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["bitrix24"], "entries": {"notify-plugin": {"allow_gateway_injection": True}}}
    }))
    try:
        entry = platform_registry.get("bitrix24")
        assert entry is not None
        adapter = entry.adapter_factory(PlatformConfig(enabled=True, extra={}))
        from plugins.bitrix24.platform import Bitrix24Adapter
        assert isinstance(adapter, Bitrix24Adapter)

        source = adapter.build_source(chat_id="task:42", chat_type="group", thread_id="42", user_id="1")
        store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
        session = store.get_or_create_session(source)
        delivered = []
        adapter.handle_message = AsyncMock(side_effect=lambda event: delivered.append(event))

        runner = object.__new__(GatewayRunner)
        runner.session_store = store
        runner.adapters = {Platform("bitrix24"): adapter}
        runner._profile_adapters = {}
        runner._running = True
        runner._draining = False
        runner._background_tasks = set()
        runner._is_user_authorized = MagicMock(return_value=True)
        runner._is_user_authorized_for_source = MagicMock(return_value=True)
        ok = await runner._dispatch_plugin_message_injection(
            session_key=session.session_key, content="wake", plugin_id="notify-plugin")
        missing = await runner._dispatch_plugin_message_injection(
            session_key="agent:main:bitrix24:group:task:404:404", content="wake", plugin_id="notify-plugin")
    finally:
        pass
    assert ok is True and missing is False
    assert len(delivered) == 1
    assert delivered[0].source.chat_id == "task:42"
    assert "task:42" in session.session_key


def _canon_run_arguments(prompt: str) -> dict:
    block = prompt.split("call canon_run with exactly these arguments", 1)[1].split("```json\n", 1)[1]
    return json.loads(block.split("\n```", 1)[0])


@pytest.mark.asyncio
async def test_new_task_of_served_group_starts_its_workflow_in_the_task_session(env):
    env.source.groups = {"1190": "54", "1191": "17"}
    env.source.messages = {"701": ("1190", "1", "@tux вопрос")}
    port = _free_port()
    adapter, events = await _adapter(port, {"workflow": WORKFLOW, "execution_root": EXECUTION_ROOT})
    try:
        served = await _post(port, _created("c1", "1190"))
        foreign = await _post(port, _created("c2", "1191"))
        comment = await _post(port, _comment("c3", "1190", "701"))
        await _drain(adapter)
    finally:
        await adapter.disconnect()
    assert served[0] == 202
    assert foreign == (200, {"status": "ignored", "reason": "group_not_allowed"})
    assert comment[0] == 202
    assert len(events) == 2
    created, tagged = events
    assert _key(created) == _key(tagged)
    assert created.source.chat_id == "task:1190"
    assert _canon_run_arguments(created.text) == {
        "workflow": WORKFLOW,
        "execution_root": EXECUTION_ROOT,
        "mode": "production",
        "wait_mode": "async-no-wait",
        "workflow_input_json": {"taskInput": {"bitrixTaskId": "1190"}},
        "human_gate_delivery": (
            "Post the card as your reply in this Bitrix24 task session (it becomes a task comment). "
            "The decision arrives later as a comment mentioning the bot; do not use clarify."
        ),
    }
    assert _canon_run_arguments(tagged.text) == _canon_run_arguments(created.text)
    assert "A new task was created" not in tagged.text
    assert "смоделируй решение" in tagged.text


@pytest.mark.asyncio
async def test_existing_task_comment_can_ask_about_code_without_losing_workflow_launch(env):
    env.source.messages = {"704": ("1180", "1", "@tux разбери функцию каталога")}
    port = _free_port()
    adapter, events = await _adapter(
        port, {"workflow": WORKFLOW, "execution_root": EXECUTION_ROOT},
        {"backend": EXECUTION_ROOT, "editor": "/var/www/editor.dev.plastinka.com"},
    )
    try:
        status, _ = await _post(port, _comment("c7", "1180", "704"))
        await _drain(adapter)
    finally:
        await adapter.disconnect()
    assert status == 202
    assert len(events) == 1
    assert "bitrix24_code_search" in events[0].text
    assert "bitrix24_code_read" in events[0].text
    assert "ordinary question" in events[0].text
    assert _canon_run_arguments(events[0].text)["workflow_input_json"] == {
        "taskInput": {"bitrixTaskId": "1180"},
    }


@pytest.mark.asyncio
async def test_comment_in_task_without_configured_workflow_carries_no_launch(env):
    env.source.messages = {"702": ("1193", "1", "@tux разбери задачу")}
    port = _free_port()
    adapter, events = await _adapter(port)
    try:
        await _post(port, _comment("c5", "1193", "702"))
        await _drain(adapter)
    finally:
        await adapter.disconnect()
    assert len(events) == 1
    assert "canon_run" not in events[0].text


@pytest.mark.asyncio
async def test_new_task_without_configured_workflow_is_not_dispatched(env):
    port = _free_port()
    adapter, events = await _adapter(port)
    try:
        result = await _post(port, _created("c4", "1192"))
        await _drain(adapter)
    finally:
        await adapter.disconnect()
    assert result == (200, {"status": "ignored", "reason": "task_workflow_not_configured"})
    assert events == []


@pytest.mark.parametrize("task_workflow", [
    {"workflow": "relative/workflow.json", "execution_root": EXECUTION_ROOT},
    {"workflow": WORKFLOW},
    "workflow.json",
])
def test_incomplete_task_workflow_config_is_rejected(env, task_workflow):
    from plugins.bitrix24.platform import Bitrix24Adapter
    with pytest.raises(ValueError):
        Bitrix24Adapter(PlatformConfig(enabled=True, extra={"task_workflow": task_workflow}))
