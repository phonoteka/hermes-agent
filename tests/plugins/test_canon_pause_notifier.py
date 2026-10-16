"""X4 RED: real Canon pauses delivered through the native gateway plugin boundary."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import stat
import sys
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import yaml

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import BasePlatformAdapter, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_cli.plugins import PluginManager, parse_manifest_file
from plugins.canon import transport
from tests.plugins.test_canon_plugin_cli_parity import (
    ROOT, binding_context, isolated_profile, native_context, real_context,
)
from tests.plugins.test_canon_plugin_journal_e2e import _human_gate_pack

TICK = 0.05


class _NoNetworkAdapter(BasePlatformAdapter):
    """Keep real busy-session routing while rejecting every network send."""

    def __init__(self):
        """Initialize a local Telegram-shaped adapter with no live transport."""
        super().__init__(PlatformConfig(enabled=True, token="test-only"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect=False):
        """Report readiness without connecting to a service."""
        return True

    async def disconnect(self):
        """Release adapter readiness without network I/O."""
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        """Fail if the notifier attempts a platform send instead of an internal turn."""
        raise AssertionError("network send is forbidden")

    async def get_chat_info(self, chat_id):
        """Return local identity for the real adapter routing boundary."""
        return {"id": chat_id, "type": "dm"}


def _gateway(tmp_path):
    """Build the probe's real gateway route with two persisted, busy sessions."""
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    entries = [store.get_or_create_session(SessionSource(
        platform=Platform.TELEGRAM, chat_id=chat, chat_type="dm",
        user_id=chat, user_name="test-only",
    )) for chat in ("42", "43")]
    adapter = _NoNetworkAdapter()
    adapter.set_message_handler(AsyncMock())
    for entry in entries:
        adapter._active_sessions[entry.session_key] = asyncio.Event()
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    runner._gateway_loop = asyncio.get_running_loop()
    runner._running = True
    runner._draining = False
    runner._background_tasks = set()
    runner._queued_events = {}
    runner._is_user_authorized = MagicMock(return_value=True)
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    return runner, adapter, [entry.session_key for entry in entries]


def _events(env, key):
    """Observe both real gateway queue locations without consuming events."""
    events = list(env.runner._queued_events.get(key, []))
    pending = env.adapter._pending_messages.get(key)
    if pending is not None:
        events.append(pending)
    return events


async def _wait_events(env, key, count=1):
    """Poll for at most ten seconds; absence is the planned behavioural falsifier."""
    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        if len(_events(env, key)) >= count:
            return _events(env, key)
        await asyncio.sleep(TICK)
    assert len(_events(env, key)) >= count, "no message delivered after real Canon pause"
    return _events(env, key)


async def _observe_quiet(env):
    """Allow multiple shortened ticks and assert no queue or dedup mutation."""
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        assert all(not _events(env, key) for key in env.keys)
        await asyncio.sleep(TICK)
    env.adapter._message_handler.assert_not_awaited()
    assert all(not event.is_set() for event in env.adapter._active_sessions.values())
    assert not env.state.exists() or not json.loads(env.state.read_text())


def _load(env, monkeypatch, *, injector=True):
    """Load real plugin registration, shortening its tick and pinning its real CLI bootstrap."""
    import hermes_cli.plugins as plugins_mod

    manager = PluginManager(scope_key=str(env.home))
    monkeypatch.setattr(plugins_mod, "get_plugin_manager", lambda: manager)
    if injector:
        env.runner._install_plugin_message_injector()
    assert manager.has_gateway_message_injector is injector
    plugin_dir = ROOT / "plugins/canon"
    manifest = parse_manifest_file(plugin_dir / "plugin.yaml", plugin_dir, "bundled", "")
    assert manifest is not None
    original_load = manager._load_directory_module

    def load_module(manifest, *, module_name=None):
        """Keep real module loading; patch collaborators before its real register executes."""
        module = original_load(manifest, module_name=module_name)
        loaded_transport = importlib.import_module(module.__name__ + ".transport")
        monkeypatch.setattr(loaded_transport, "_canon_command_prefix", env.prefix)
        notifier_name = module.__name__ + ".pause_notifier"
        # The RED baseline has no notifier. Do not manufacture it: registration
        # still runs, and the delivery assertion exposes its missing behaviour.
        if importlib.util.find_spec(notifier_name) is not None:
            notifier = importlib.import_module(notifier_name)
            monkeypatch.setattr(notifier, "TICK_SECONDS", TICK)
            for name, value in env.notifier_overrides.items():
                monkeypatch.setattr(notifier, name, value, raising=False)
            env.notifier_modules.append(notifier)
        return module

    monkeypatch.setattr(manager, "_load_directory_module", load_module)
    before = set(threading.enumerate())
    manager._load_plugin(manifest)
    loaded = manager._plugins["canon"]
    assert loaded.enabled, loaded.error
    env.managers.append(manager)
    return manager, set(threading.enumerate()) - before


@pytest_asyncio.fixture
async def notifier_env(binding_context, isolated_profile, tmp_path, monkeypatch):
    """Isolate stores and the real CLI host; unload all plugin workers on every exit."""
    home = isolated_profile
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {
        "enabled": ["canon"], "entries": {"canon": {"allow_gateway_injection": True}},
    }}))
    runner, adapter, keys = _gateway(tmp_path)
    # P2's binding_context pins the FakeHost bootstrap in the loaded plugin's transport;
    # mirror it into the directly imported transport used for unbound runs and index reads.
    prefix = binding_context.handle.__globals__["_canon_command_prefix"]
    monkeypatch.setattr(transport, "_canon_command_prefix", prefix)
    env = SimpleNamespace(
        home=home, tmp_path=tmp_path, ctx=binding_context,
        prefix=prefix,
        runner=runner, adapter=adapter, keys=keys, managers=[],
        # binding_context already loaded the real plugin (and its notifier) once;
        # keep that module so patched constants reach every live notifier instance.
        notifier_modules=[m for n, m in sys.modules.items() if n.endswith(".pause_notifier")],
        # Plugin discovery inside a real tool call may load a fresh notifier module later;
        # load_module applies these overrides to it as well.
        notifier_overrides={},
        state=home / "runtime/canon/pause-notifier.json",
    )
    yield env
    for manager in reversed(env.managers):
        manager.unload()
        manager.clear_gateway_message_injector(runner)
    if runner._background_tasks:
        await asyncio.gather(*runner._background_tasks, return_exceptions=True)


def _pack(env, run_id, *, human=True):
    """Give each real CLI run a distinct identity and execution root."""
    parent = env.tmp_path / run_id
    parent.mkdir()
    if human:
        pack = _human_gate_pack(parent)
    else:
        import shutil
        pack = parent / "completed-pack"
        shutil.copytree(Path(env.ctx.workflow).parent, pack)
    request_path = pack / "run-request.json"
    request = json.loads(request_path.read_text())
    request["runId"] = run_id
    request["threadId"] = "thread-" + run_id
    request["runConfiguration"]["executionRoot"] = str(parent / "execution")
    request_path.write_text(json.dumps(request))
    return pack


async def _run(env, run_id, key, *, human=True, delivery="Deliver in this chat"):
    """Pause or complete real Canon through native tools; only unbound runs bypass canon_run."""
    pack = _pack(env, run_id, human=human)
    (pack.parent / "execution").mkdir()
    args = {
        "workflow": str(pack / "workflow.json"),
        "request_json": str(pack / "run-request.json"),
        "wait_mode": "sync-real", "timeout_seconds": 180,
    }
    if key is not None:
        args["host_session"] = key
    if delivery is not None:
        args["human_gate_delivery"] = delivery
    if key is None:
        argv = transport.build_argv("run", args)
        result = await asyncio.to_thread(subprocess.run, argv, capture_output=True, text=True, timeout=190)
        assert result.returncode == 0, result.stderr
        value = json.loads(result.stdout)
    else:
        output = await asyncio.to_thread(env.ctx.invoke, "canon_run", args)
        value = json.loads(output)
    assert value.get("status") == ("paused" if human else "completed"), value
    result = await asyncio.to_thread(
        subprocess.run, transport.build_argv("index", {"limit": 100000}), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    matches = [row for row in rows if row["runId"] == run_id]
    assert len(matches) == 1, {"launch": value, "index": rows}
    row = matches[0]
    assert row["status"] == ("paused" if human else "completed"), row
    assert row["hostSession"] == key, row
    assert row["humanGateDelivery"] == delivery, row
    assert row["eventCount"] > 0
    return row


async def _prompt(env, row):
    """Read Canon's real card and continuation request from the default stores off the gateway loop."""
    argv = transport.build_argv("inspect", {"surface": "prompt", "run_id": row["runId"]})
    argv.extend(["--checkpoint-backend", "sqlite", "--checkpoint-db",
                 str(env.home / "runtime/canon/durable/checkpoint.sqlite")])
    result = await asyncio.to_thread(subprocess.run, argv, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


async def _assert_message(env, text, row, *, file_card=False):
    """Require an unchanged Canon card or private full-prompt file in a JSON wake."""
    prompt = await _prompt(env, row)
    message = json.loads(text)
    assert "\n" not in text
    assert message["type"] == "canon_human_gate_wake"
    for key in ("runId", "workflowRef", "humanGateDelivery"):
        assert message[key] == row[key]
    assert isinstance(message["instruction"], str) and message["instruction"].strip()
    assert message["actions"] == prompt["humanGatePrompt"]["actions"]
    assert message["continuePaused"] == prompt["continuePaused"]
    if file_card:
        assert "cardBody" not in message
        path = Path(message["cardBodyFile"])
        assert path.is_absolute()
        assert path == env.home / "runtime/canon/prompts" / f"{row['runId']}-{row['eventCount']}.json"
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        content = path.read_bytes()
        assert hashlib.sha256(content).hexdigest() == message["cardBodySha256"]
        assert json.loads(content) == prompt
    else:
        assert message["cardBody"] == prompt["humanGatePrompt"]["cardBody"]
        assert "cardBodyFile" not in message and "cardBodySha256" not in message
    return message


async def _assert_event(env, key, row, *, file_card=False):
    """Require one internal wake without interrupting or invoking a busy handler."""
    events = _events(env, key)
    assert len(events) == 1
    assert events[0].internal is True
    assert events[0].allow_gateway_control is False
    assert not env.adapter._active_sessions[key].is_set()
    env.adapter._message_handler.assert_not_awaited()
    return await _assert_message(env, events[0].text, row, file_card=file_card)


async def _approve(env, cp):
    """Approve the real pause using the continuation authority returned by Canon."""
    output = await asyncio.to_thread(env.ctx.invoke, "canon_checkpoint", {
        "subcommand": "continue-paused", "workflow": cp["workflow"],
        "request_json": {**cp["request"], "gateDecision": {"action": "approve"}},
        "wait_mode": "sync-real", "timeout_seconds": 180,
    })
    value = json.loads(output)
    assert value.get("status") == "completed", value


def _small_inline_limit(env, monkeypatch):
    """Force every live and later-loaded real notifier to use its oversize-card path."""
    env.notifier_overrides["MAX_INLINE_CHARS"] = 200
    for module in env.notifier_modules:
        monkeypatch.setattr(module, "MAX_INLINE_CHARS", 200, raising=False)


async def _run_unbound_refused(env, run_id):
    """Require Canon itself to refuse a Human Gate run without a host session."""
    pack = _pack(env, run_id)
    argv = transport.build_argv("run", {
        "workflow": str(pack / "workflow.json"),
        "request_json": str(pack / "run-request.json"),
    })
    result = await asyncio.to_thread(subprocess.run, argv, capture_output=True, text=True, timeout=190)
    assert result.returncode != 0, result.stdout
    assert "host-session" in result.stderr, result.stderr


@pytest.mark.asyncio
async def test_card_delivered(notifier_env, monkeypatch):
    """Deliver the real card only to its bound session and approve its continuation."""
    env = notifier_env
    _load(env, monkeypatch)
    row = await _run(env, "run-x5-inline", env.keys[0])
    await _wait_events(env, env.keys[0])
    assert not _events(env, env.keys[1])
    message = await _assert_event(env, env.keys[0], row)
    await _approve(env, message["continuePaused"])
    assert not _events(env, env.keys[1])


@pytest.mark.asyncio
async def test_card_file(notifier_env, monkeypatch):
    """An oversize wake carries an intact private prompt file, not an inline card."""
    env = notifier_env
    _load(env, monkeypatch)
    _small_inline_limit(env, monkeypatch)
    row = await _run(env, "run-x5-file", env.keys[0])
    await _wait_events(env, env.keys[0])
    assert not _events(env, env.keys[1])
    await _assert_event(env, env.keys[0], row, file_card=True)


@pytest.mark.asyncio
async def test_card_file_removed(notifier_env, monkeypatch):
    """Keep the file while paused, then delete it after real approval completes."""
    env = notifier_env
    _load(env, monkeypatch)
    _small_inline_limit(env, monkeypatch)
    row = await _run(env, "run-x5-cleanup", env.keys[0])
    await _wait_events(env, env.keys[0])
    message = await _assert_event(env, env.keys[0], row, file_card=True)
    path = Path(message["cardBodyFile"])
    await asyncio.sleep(2)
    assert path.is_file()
    await _approve(env, message["continuePaused"])
    deadline = asyncio.get_running_loop().time() + 10
    while path.exists() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(TICK)
    assert not path.exists(), "completed run retained its Human Gate prompt file"


@pytest.mark.asyncio
@pytest.mark.parametrize("target,delivery", [(0, "Deliver in this chat"), (1, "Deliver in this chat"), (0, None)],
                         ids=["session-k", "other-session-control", "unspecified-delivery"])
async def test_delivers_once(notifier_env, monkeypatch, target, delivery):
    """Route a real pause once to its bound session, never to the other session."""
    env = notifier_env
    _load(env, monkeypatch)
    row = await _run(env, "run-x4-deliver", env.keys[target], delivery=delivery)
    await _wait_events(env, env.keys[target])
    await _assert_event(env, env.keys[target], row)
    await asyncio.sleep(2)
    await _assert_event(env, env.keys[target], row)
    assert not _events(env, env.keys[1 - target])
    assert stat.S_IMODE(env.state.stat().st_mode) == 0o600


def _seed_older_runs(env, count):
    """Occupy the CLI index default window with real journal copies of one completed run."""
    from copy import deepcopy
    from canon.journal import SqliteExecutionJournal

    journal = SqliteExecutionJournal(env.home / "runtime/canon/durable/journal.sqlite")
    template = journal.list_run("run-x4-page-seed")
    assert template
    for n in range(count):
        for row in template:
            entry = deepcopy(row)
            entry.pop("sequence", None)
            entry.pop("appendSequence", None)
            entry["runId"] = f"run-x4-page-filler-{n:03d}"
            assert isinstance(journal.append(entry), dict)


@pytest.mark.asyncio
async def test_delivers_once_beyond_first_index_page(notifier_env, monkeypatch):
    """A bound pause older runs push past the CLI index default window is still delivered."""
    env = notifier_env
    _load(env, monkeypatch)
    # Small pages force several real index calls per tick (63 runs / 20 per page).
    import sys as _sys
    for name, module in list(_sys.modules.items()):
        if name.endswith(".pause_notifier") and hasattr(module, "INDEX_PAGE_SIZE"):
            monkeypatch.setattr(module, "INDEX_PAGE_SIZE", 20)
    await _run(env, "run-x4-page-seed", None, human=False, delivery=None)
    _seed_older_runs(env, 60)
    row = await _run(env, "run-x4-page-late", env.keys[0])
    await _wait_events(env, env.keys[0])
    await _assert_event(env, env.keys[0], row)
    assert not _events(env, env.keys[1])


@pytest.mark.asyncio
async def test_gateway_boundary_control(notifier_env, monkeypatch):
    """Prove the exact harness accepts a real consented plugin injection into a busy session."""
    from hermes_cli.plugins import PluginContext

    env = notifier_env
    manager, _ = _load(env, monkeypatch)
    ctx = PluginContext(manager._plugins["canon"].manifest, manager)
    assert ctx.inject_message("gateway harness control", session_key=env.keys[0]) is True
    events = await _wait_events(env, env.keys[0])
    assert len(events) == 1
    assert events[0].text == "gateway harness control"
    assert events[0].internal is True
    assert not env.adapter._active_sessions[env.keys[0]].is_set()
    assert not _events(env, env.keys[1])
    env.adapter._message_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["no-injector", "consent-denied", "no-host-session", "completed-run"])
async def test_no_injection(notifier_env, monkeypatch, control):
    """Reject unsafe/unbound pauses and never notify a completed bound run."""
    env = notifier_env
    if control == "consent-denied":
        (env.home / "config.yaml").write_text(yaml.safe_dump({"plugins": {
            "enabled": ["canon"], "entries": {"canon": {"allow_gateway_injection": False}},
        }}))
    _load(env, monkeypatch, injector=control != "no-injector")
    if control == "no-host-session":
        await _run_unbound_refused(env, "run-x4-control")
    else:
        await _run(env, "run-x4-control", env.keys[0], human=control != "completed-run")
    await _observe_quiet(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_state", [False, True], ids=["persistent-dedup", "deleted-state-control"])
async def test_restart(notifier_env, monkeypatch, delete_state):
    """Unload stops the worker; only the persisted dedup file prevents replay after reload."""
    env = notifier_env
    first_manager, threads = _load(env, monkeypatch)
    pause_a = await _run(env, "run-x4-a", env.keys[0])
    await _wait_events(env, env.keys[0])
    await _assert_event(env, env.keys[0], pause_a)
    assert stat.S_IMODE(env.state.stat().st_mode) == 0o600
    first_manager.unload()
    assert threads, "register did not start a notifier thread"
    assert all(not thread.is_alive() for thread in threads), "notifier thread survived unload"
    first_manager.clear_gateway_message_injector(env.runner)
    env.runner._queued_events.clear()
    env.adapter._pending_messages.clear()
    pause_b = await _run(env, "run-x4-b", env.keys[0])
    assert not _events(env, env.keys[0])
    if delete_state:
        env.state.unlink()
    _load(env, monkeypatch)
    expected = {pause_b["runId"]: pause_b}
    if delete_state:
        expected[pause_a["runId"]] = pause_a
    await _wait_events(env, env.keys[0], len(expected))
    await asyncio.sleep(2)
    events = _events(env, env.keys[0])
    assert len(events) == len(expected)
    messages = [json.loads(event.text) for event in events]
    assert sorted(message["runId"] for message in messages) == sorted(expected)
    for event, message in zip(events, messages):
        await _assert_message(env, event.text, expected[message["runId"]])
    assert all(event.internal and not event.allow_gateway_control for event in events)
    assert not _events(env, env.keys[1])
    assert not env.adapter._active_sessions[env.keys[0]].is_set()
    env.adapter._message_handler.assert_not_awaited()
