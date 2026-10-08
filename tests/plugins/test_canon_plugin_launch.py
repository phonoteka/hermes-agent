"""Native async completion and cancellation for the mechanical Canon command tools."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import queue
import sys

import pytest

from plugins.canon import transport


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """Pre: temporary directory. Post: isolate all carriers and native state. Raises: OSError."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(transport, "get_default_hermes_root", lambda: tmp_path)
    return tmp_path


def test_async_command_completion_preserves_large_paused_stdout(profile, monkeypatch):
    """Pre: isolated registry. Post: completion tail and full native log match stdout; carriers disappear. Raises: assertions."""
    import model_tools
    from tools.registry import registry
    from tools.process_registry import process_registry

    while True:
        try:
            process_registry.completion_queue.get_nowait()
        except queue.Empty:
            break
    payload = {"status": "paused", "humanGatePrompt": {"cardBody": "x" * 5000}, "opaque": [1, 2]}
    output = json.dumps(payload) + "\n"
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: [sys.executable, "-c", f"import sys;sys.stdout.write({output!r})"])

    class Context:
        def dispatch_tool(self, name, args, **kwargs):
            """Pre: native tool call. Post: real registry result. Raises: registry errors."""
            return registry.dispatch(name, args, **kwargs)

    accepted = json.loads(asyncio.run(transport.handle_command("validate", {
        "workflow": "opaque.json", "wait_mode": "async-no-wait", "timeout_seconds": 10,
    }, ctx=Context(), task_id="canon-cli-async-test")))
    assert accepted["notify_on_complete"] is True
    event = process_registry.completion_queue.get(timeout=20)
    assert event["session_id"] == accepted["session_id"]
    assert event["session_key"] == "canon-cli-async-test"
    assert event["exit_code"] == 0
    assert event["output"] == output[-2000:]
    raw_log = registry.dispatch("process_manage", {
        "action": "log", "session_id": accepted["session_id"], "offset": 0, "limit": 10000,
    })
    assert isinstance(raw_log, str)
    logged = json.loads(raw_log)
    assert json.loads(logged["output"]) == payload
    assert not list((profile / "runtime/canon").glob("hermes-canon-*"))


def test_timeout_cancels_native_process_and_cleans_carriers(profile, monkeypatch):
    """Pre: native process cannot finish. Post: killed and carriers removed. Raises: assertions."""
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: ["/bin/true"])
    calls = []

    class Context:
        def dispatch_tool(self, name, args, **kwargs):
            """Pre: recorded native call. Post: timeout or killed response. Raises: none."""
            calls.append((name, args))
            if name == "terminal":
                return json.dumps({"session_id": "native-timeout", "exit_code": 0})
            if args["action"] == "wait":
                return json.dumps({"status": "timeout", "process_running": True})
            return json.dumps({"status": "killed"})

    result = json.loads(asyncio.run(transport.handle_command("validate", {
        "workflow": "opaque.json", "timeout_seconds": 1,
    }, ctx=Context())))
    assert result == {"error": "Canon process did not complete within native wait"}
    assert [(name, args.get("action")) for name, args in calls] == [
        ("terminal", None), ("process_manage", "wait"), ("process_manage", "kill"),
    ]
    assert not list((profile / "runtime/canon").glob("hermes-canon-*"))
