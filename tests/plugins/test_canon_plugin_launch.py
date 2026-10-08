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


@pytest.fixture
def human_gate_launch(profile, tmp_path, monkeypatch):
    """Launch the real plugin/CLI with a fake phase host and preserve CLI input bytes."""
    import model_tools
    from tests.plugins.test_canon_plugin_cli_parity import RegisteredContext, minimal_workflow
    from tests.plugins.test_canon_plugin_journal_e2e import _human_gate_pack

    canon_root = minimal_workflow().parents[4]
    hermes_root = Path(__file__).resolve().parents[2]
    pack = _human_gate_pack(tmp_path)
    execution = tmp_path / "execution"
    execution.mkdir()
    seen_config = tmp_path / "seen-host-config.json"
    seen_argv = tmp_path / "seen-cli-argv.json"
    bootstrap = tmp_path / "human-gate-bootstrap.py"
    bootstrap.write_text(
        "import sys, types, json\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(hermes_root)!r})\n"
        f"sys.path.insert(0, {str(canon_root / 'src')!r})\n"
        "import profile_runtime\n"
        "tests = types.ModuleType('tests')\n"
        f"tests.__path__ = [{str(canon_root / 'tests')!r}]\n"
        "sys.modules['tests'] = tests\n"
        "from tests.helpers.fake_host import FakeHostAdapter\n"
        "from canon.host_provider_registry import HostAdapterProviderRegistry\n"
        "def resolve(self, selector, request):\n"
        "    \"Return an isolated phase host backed by the real CLI stores.\"\n"
        "    assert selector == 'canon-hermes-profile'\n"
        "    host = FakeHostAdapter(capabilities=set(request.capabilities), "
        "checkpoint_backend=request.store.checkpoint_backend, artifact_backend=request.store.artifact_backend)\n"
        "    host.journal = request.store.journal\n"
        "    return host\n"
        "HostAdapterProviderRegistry.resolve = resolve\n"
        "from canon.cli import main\n"
        f"Path({str(seen_argv)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "if '--host-config' in sys.argv:\n"
        "    config_path = Path(sys.argv[sys.argv.index('--host-config') + 1])\n"
        f"    Path({str(seen_config)!r}).write_bytes(config_path.read_bytes())\n"
        "raise SystemExit(main(sys.argv[1:]))\n"
    )
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: [sys.executable, "-I", str(bootstrap)])
    context = RegisteredContext(real=True)
    args = {
        "workflow": str(pack / "workflow.json"),
        "host_session": "x3-session", "workflow_input_json": {"task": "X3"},
        "execution_root": str(execution), "wait_mode": "sync-real", "timeout_seconds": 60,
    }
    return context, args, seen_config, seen_argv


@pytest.mark.parametrize("observation", ["host-config", "journal"])
def test_human_gate_pause_without_hook(human_gate_launch, profile, observation):
    """A real Human Gate pause neither requests a hook nor records hook-delivery events."""
    from canon.journal import SqliteExecutionJournal

    context, args, seen_config, _ = human_gate_launch
    result = json.loads(context.invoke("canon_run", args))
    assert result.get("status") == "paused", result
    assert result["hostSession"] == "x3-session"
    config = json.loads(seen_config.read_bytes())
    rows = SqliteExecutionJournal(profile / "runtime/canon/durable/journal.sqlite").list_run(result["runId"])
    kinds = [row["eventKind"] for row in rows]
    assert "run.accepted" in kinds, kinds
    if observation == "host-config":
        assert "humanGateNotification" not in config, config
    else:
        assert not [kind for kind in kinds if kind.startswith("human-gate.")], kinds


def test_human_gate_pause_without_hook_explicit_config_control(human_gate_launch, profile, tmp_path):
    """An explicit user config reaches the real CLI at its exact path and bytes."""
    context, args, seen_config, seen_argv = human_gate_launch
    explicit = tmp_path / "user-host-config.json"
    (profile / "runtime/canon/durable").mkdir(parents=True)
    payload = json.dumps({
        "mode": "production", "hostAdapter": "canon-hermes-profile",
        "hostConfiguration": {"profilesRoot": str(profile)},
        "durableRoot": str(profile / "runtime/canon/durable"),
    }, indent=2).encode()
    explicit.write_bytes(payload)
    result = json.loads(context.invoke("canon_run", {**args, "host_config": str(explicit)}))
    assert result.get("status") == "paused", result
    argv = json.loads(seen_argv.read_text())
    assert argv.count("--host-config") == 1
    assert argv[argv.index("--host-config") + 1] == str(explicit)
    assert seen_config.read_bytes() == payload
    assert explicit.read_bytes() == payload
