"""S3 RED: exercise plugin wake delivery through its Unix socket boundary.

Secondary-profile coverage awaits a supported profile-aware gateway harness;
_gateway currently constructs default-profile sessions only.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
from pathlib import Path
import threading
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import yaml

from hermes_constants import get_default_hermes_root
from plugins.canon import transport
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import BasePlatformAdapter, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_cli.plugins import PluginManager, parse_manifest_file
from tests.plugins.test_canon_plugin_cli_parity import (
    ROOT, binding_context, isolated_profile, native_context, real_context,
)
from tests.plugins.test_canon_plugin_journal_e2e import _human_gate_pack


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



def _load(env, monkeypatch, *, injector=True):
    """Load real plugin registration, pinning its real CLI bootstrap."""
    import hermes_cli.plugins as plugins_mod

    manager = PluginManager(scope_key=str(env.home))
    # A gateway process has exactly one plugin manager per Hermes home. binding_context
    # already loaded the plugin into a manager for this home; retire it so the manager that
    # owns the gateway injector is also the one whose plugin owns the wake socket.
    env.ctx.manager.unload()
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



def _socket_path():
    """Use the gateway-wide socket, not a profile-local derived location."""
    return Path(get_default_hermes_root()) / "runtime/canon/gateway-wake.sock"


async def _exchange(line):
    """Require a registered listener and complete one bounded wire exchange."""
    path = _socket_path()
    assert path.exists(), f"plugin registration did not create wake socket: {path}"
    async with asyncio.timeout(5):
        reader, writer = await asyncio.open_unix_connection(str(path))
        try:
            writer.write(line.encode("utf-8"))
            await writer.drain()
            return json.loads(await reader.readline())
        finally:
            writer.close()
            await writer.wait_closed()


def _request(env, prompt, key):
    return json.dumps({
        "type": "canon_human_gate_wake",
        "hostSession": key,
        "humanGateDelivery": "Deliver in this chat",
        "prompt": prompt,
    }, ensure_ascii=False) + "\n"


@pytest.mark.asyncio
async def test_socket_wake_injects_into_session(notifier_env, monkeypatch):
    """A real paused prompt becomes exactly one internal event in its bound session."""
    env = notifier_env
    _load(env, monkeypatch)
    row = await _run(env, "run-s3-socket", env.keys[0])
    prompt = await _prompt(env, row)
    assert await _exchange(_request(env, prompt, env.keys[0])) == {"accepted": True}
    async with asyncio.timeout(5):
        while not _events(env, env.keys[0]):
            await asyncio.sleep(0.01)
    events = _events(env, env.keys[0])
    assert len(events) == 1
    message = json.loads(events[0].text)
    assert message["type"] == "canon_human_gate_wake"
    for field in ("runId", "workflowRef", "humanGateDelivery"):
        assert message[field] == row[field]
    assert message["actions"] == prompt["humanGatePrompt"]["actions"]
    assert message["continuePaused"] == prompt["continuePaused"]
    assert message["cardBody"] == prompt["humanGatePrompt"]["cardBody"]
    assert "sha256:" + hashlib.sha256(message["cardBody"].encode("utf-8")).hexdigest() == prompt["humanGatePrompt"]["cardBodyDigest"]
    assert events[0].internal is True
    assert events[0].allow_gateway_control is False
    assert not _events(env, env.keys[1])
    assert not env.adapter._active_sessions[env.keys[0]].is_set()
    env.adapter._message_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["no-injector", "consent-denied"])
async def test_unknown_session_refused(notifier_env, monkeypatch, control):
    """Injection without gateway authority is refused without a queued event."""
    env = notifier_env
    if control == "consent-denied":
        (env.home / "config.yaml").write_text(yaml.safe_dump({"plugins": {
            "enabled": ["canon"],
            "entries": {"canon": {"allow_gateway_injection": False}},
        }}))
    _load(env, monkeypatch, injector=control != "no-injector")
    row = await _run(env, "run-s3-refused", env.keys[0])
    prompt = await _prompt(env, row)
    assert await _exchange(_request(env, prompt, env.keys[0])) == {
        "accepted": False, "code": "injection_refused",
    }
    assert all(not _events(env, key) for key in env.keys)
    env.adapter._message_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_line(notifier_env, monkeypatch):
    """Invalid JSON is rejected without injecting into any gateway session."""
    env = notifier_env
    _load(env, monkeypatch)
    assert await _exchange("not json\n") == {"accepted": False, "code": "malformed"}
    assert all(not _events(env, key) for key in env.keys)


@pytest.mark.asyncio
async def test_no_poller_thread(notifier_env, monkeypatch):
    """Plugin registration must not leave the retired polling worker running."""
    _load(notifier_env, monkeypatch)
    assert not any(
        thread.name == "canon-pause-notifier" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_run_host_config_carries_wake_socket(isolated_profile):
    """The real argv builder tells the host where the gateway listener accepts wakes."""
    argv = transport.build_argv("run", {"workflow": "workflow.json"})
    config = json.loads(Path(argv[argv.index("--host-config") + 1]).read_text())
    assert config["hostConfiguration"]["gatewayWakeSocket"] == str(_socket_path())
    assert _socket_path().is_absolute()


def test_explicit_host_config_forwarded_unchanged(isolated_profile):
    """An explicit caller's JSON string remains byte-identical on the CLI boundary."""
    explicit = '{ "hostConfiguration": {"callerOwned": true}, "mode": "production" }'
    argv = transport.build_argv("run", {"workflow": "workflow.json", "host_config": explicit})
    assert argv[argv.index("--host-config") + 1] == explicit


@pytest.mark.asyncio
async def test_socket_wake_secondary_profile_key(notifier_env, monkeypatch):
    """A secondary-profile session receives its wake without touching default sessions."""
    from gateway.config import Platform
    from gateway.session import SessionSource

    env = notifier_env
    _load(env, monkeypatch)
    # Profile-namespaced keys exist only in a multiplexing gateway (session_recovery._resolve_profile_for_key).
    monkeypatch.setattr(env.runner.session_store.config, "multiplex_profiles", True)
    # A real secondary profile: its home exists under the default root, as for bitrix-reader,
    # and the gateway serves it with its own adapter mapping (here the same no-network adapter).
    (env.home / "profiles" / "secondary").mkdir(parents=True)
    monkeypatch.setattr(env.runner, "_profile_adapters", {"secondary": {Platform.TELEGRAM: env.adapter}})
    entry = env.runner.session_store.get_or_create_session(SessionSource(
        platform=Platform.TELEGRAM, chat_id="44", chat_type="dm",
        user_id="44", user_name="test-only", profile="secondary",
    ))
    env.adapter._active_sessions[entry.session_key] = asyncio.Event()
    assert entry.session_key.startswith("agent:secondary:"), entry.session_key
    row = await _run(env, "run-s3-secondary", entry.session_key)
    prompt = await _prompt(env, row)
    assert await _exchange(_request(env, prompt, entry.session_key)) == {"accepted": True}
    async with asyncio.timeout(5):
        while not _events(env, entry.session_key):
            await asyncio.sleep(0.01)
    events = _events(env, entry.session_key)
    assert len(events) == 1
    message = json.loads(events[0].text)
    assert message["type"] == "canon_human_gate_wake"
    for field in ("runId", "workflowRef", "humanGateDelivery"):
        assert message[field] == row[field]
    assert message["actions"] == prompt["humanGatePrompt"]["actions"]
    assert message["continuePaused"] == prompt["continuePaused"]
    assert message["cardBody"] == prompt["humanGatePrompt"]["cardBody"]
    assert "sha256:" + hashlib.sha256(message["cardBody"].encode("utf-8")).hexdigest() == prompt["humanGatePrompt"]["cardBodyDigest"]
    assert events[0].internal is True
    assert events[0].allow_gateway_control is False
    assert all(not _events(env, key) for key in env.keys)
    assert not env.adapter._active_sessions[entry.session_key].is_set()
    env.adapter._message_handler.assert_not_awaited()

