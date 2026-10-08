"""P1 behavioural falsifiers against the real Canon plugin and CLI."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def run_manifest(tmp_path, code):
    """Exercise the executable runner with a valid private manifest."""
    private = tmp_path / "runtime" / "canon" / "hermes-canon-parity"
    private.mkdir(parents=True, mode=0o700)
    manifest = private / "manifest.json"
    manifest.write_text(json.dumps({
        "schemaVersion": "canon-plugin-background-manifest.v1",
        "operation": "command", "argv": [sys.executable, "-c", code],
        "timeoutSeconds": 10, "cleanupRoot": str(private),
    }))
    manifest.chmod(0o600)
    result = subprocess.run([sys.executable, str(ROOT / "plugins/canon/background_runner.py"), str(manifest)], capture_output=True, text=True, timeout=20)
    assert not private.exists(), "runner must clean its private root"
    return result


def test_runner_paused_stdout_is_exact_passthrough(tmp_path):
    payload = json.dumps({"status": "paused", "pendingHumanGate": {"gateId": "g"}, "humanGatePrompt": {"cardBody": "x" * 3000}}, indent=2)
    result = run_manifest(tmp_path, f"print({payload!r})")
    assert result.returncode == 0, result.stdout
    assert result.stdout == payload + "\n"


def test_runner_negative_control_exit_and_stderr(tmp_path):
    result = run_manifest(tmp_path, "import sys; sys.stderr.write('private-error'); sys.exit(2)")
    assert result.returncode == 2
    assert "private-error" not in result.stdout
    assert "private-error" not in result.stderr


# The CLI parser, not plugin implementation details, owns the public flag oracle.
import argparse
import asyncio
from copy import deepcopy
import importlib.util
import stat

from canon.cli import build_parser
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from plugins.canon import register, transport

PROCESS_PARAMETERS = {"wait_mode", "timeout_seconds"}
JSON_PARAMETERS = {"request_json", "state_patch_json", "host_config", "backend", "workflow_input_json"}


def cli_leaves(parser=None, prefix=()):
    """Yield every executable parser, preserving checkpoint subcommands."""
    parser = build_parser() if parser is None else parser
    children = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not children:
        yield prefix, parser
    for action in children:
        for name, child in action.choices.items():
            yield from cli_leaves(child, prefix + (name,))


def cli_parameters(parser):
    """Expose each long-option spelling, including argparse aliases."""
    result = {}
    for action in parser._actions:
        if action.dest == "help":
            continue
        if action.option_strings:
            for flag in action.option_strings:
                if flag.startswith("--"):
                    result[flag[2:].replace("-", "_")] = (flag, action)
        else:
            result[action.dest] = (None, action)
    return result


def expected_tools():
    result = {}
    for path, parser in cli_leaves():
        row = result.setdefault("canon_" + path[0], {"parameters": {}, "required": set(), "subcommands": set()})
        for key, pair in cli_parameters(parser).items():
            row["parameters"][key] = pair
            flag, action = pair
            if action.required or (flag is None and action.nargs not in ("?", "*")):
                row["required"].add(key)
        if len(path) > 1:
            row["subcommands"].add(path[1])
            row["required"].add("subcommand")
    return result


class RegisteredContext:
    """Record real plugin registration; optionally dispatch native process tools."""
    def __init__(self, real=False):
        self.tools = {}
        self.real = real
        self.calls = []
        register(self)

    def register_tool(self, **entry):
        assert entry["name"] not in self.tools
        self.tools[entry["name"]] = entry

    def dispatch_tool(self, name, args, **kwargs):
        assert self.real, "unexpected transport dispatch in schema proof"
        assert name in {"terminal", "process_manage"}
        self.calls.append((name, args))
        from tools.registry import registry
        return registry.dispatch(name, args, **kwargs)

    def invoke(self, name, args):
        assert name in self.tools, f"missing CLI tool: {name}; registered: {sorted(self.tools)}"
        return asyncio.run(self.tools[name]["handler"](args))


def assert_parity(tools):
    expected = expected_tools()
    assert set(tools) == set(expected), "registered command tools differ from Canon CLI"
    for name, row in expected.items():
        params = tools[name]["schema"]["parameters"]
        keys = set(row["parameters"]) | PROCESS_PARAMETERS
        if row["subcommands"]:
            keys.add("subcommand")
            assert set(params["properties"]["subcommand"]["enum"]) == row["subcommands"]
        assert set(params["properties"]) == keys, name
        assert params["additionalProperties"] is False
        assert set(params.get("required", [])) == row["required"], name
        for key, (_, action) in row["parameters"].items():
            prop = params["properties"][key]
            if action.choices is not None:
                assert set(prop["enum"]) == set(action.choices), (name, key)
            if action.type is int:
                assert prop["type"] == "integer", (name, key)
            if key in JSON_PARAMETERS:
                validator = Draft202012Validator(prop)
                validator.validate("/tmp/carrier.json")
                validator.validate({"z": 1, "a": 2})
                assert not validator.is_valid(17)
        Draft202012Validator(params["properties"]["wait_mode"]).validate("sync-real")
        timeout = Draft202012Validator(params["properties"]["timeout_seconds"])
        assert timeout.is_valid(1) and timeout.is_valid(600)
        assert not timeout.is_valid(0) and not timeout.is_valid(601)


def test_parity_registered_schemas_match_cli_parser():
    assert_parity(RegisteredContext().tools)


def test_parity_negative_control_every_deleted_parameter_is_detected():
    tools = RegisteredContext().tools
    assert_parity(tools)
    for name, entry in tools.items():
        for parameter in entry["schema"]["parameters"]["properties"]:
            mutant = deepcopy(tools)
            del mutant[name]["schema"]["parameters"]["properties"][parameter]
            with pytest.raises((AssertionError, KeyError)):
                assert_parity(mutant)


@pytest.fixture
def isolated_profile(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(transport, "get_default_hermes_root", lambda: profile)
    return profile


def build_cli_argv(command, args):
    builder = getattr(transport, "build_argv", None)
    assert callable(builder), "missing mechanical build_argv(command, args) owner"
    argv = builder(command, args)
    assert isinstance(argv, list) and all(isinstance(v, str) for v in argv)
    return argv


LEAVES = list(cli_leaves())


@pytest.mark.parametrize("path,parser", LEAVES, ids=["-".join(p) for p, _ in LEAVES])
def test_argv_all_flags_are_forwarded_without_semantic_checks(path, parser, isolated_profile):
    args = {}
    for key, (flag, action) in cli_parameters(parser).items():
        args[key] = 7 if action.type is int else f"marker-{key}"
    if len(path) > 1:
        args["subcommand"] = path[1]
    args.update(wait_mode="sync-real", timeout_seconds=17)
    argv = build_cli_argv(path[0], args)
    start = argv.index(path[0])
    if len(path) > 1:
        assert argv[start + 1] == path[1]
    for key, (flag, _) in cli_parameters(parser).items():
        if flag is None:
            assert str(args[key]) in argv[start + 1:]
        else:
            assert argv.count(flag) == 1
            assert argv[argv.index(flag) + 1] == str(args[key])
    assert "--wait-mode" not in argv and "--timeout-seconds" not in argv


@pytest.mark.parametrize("command,key", [("run", "request_json"), ("run", "host_config"), ("checkpoint", "state_patch_json"), ("inspect", "backend")])
def test_argv_object_carriers_preserve_bytes_and_permissions(command, key, isolated_profile):
    payload = {"z": "first", "a": {"b": 2, "a": 1}}
    args = {key: payload}
    if command in {"run", "checkpoint"}:
        args["workflow"] = "opaque-workflow.json"
    if command == "checkpoint":
        args["subcommand"] = "fork"
    argv = build_cli_argv(command, args)
    path = Path(argv[argv.index("--" + key.replace("_", "-")) + 1])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == json.dumps(payload, sort_keys=False).encode("utf-8")
    assert path.is_relative_to(isolated_profile)


@pytest.mark.parametrize("command", ["run", "checkpoint", "resume", "restart"])
def test_argv_host_config_default_and_explicit_precedence(command, isolated_profile):
    args = {"workflow": "opaque.json"} if command != "restart" else {}
    if command == "checkpoint":
        args["subcommand"] = "history"
    explicit = build_cli_argv(command, {**args, "host_config": "/explicit/config.json"})
    assert explicit.count("--host-config") == 1
    assert explicit[explicit.index("--host-config") + 1] == "/explicit/config.json"
    default = build_cli_argv(command, args)
    path = Path(default[default.index("--host-config") + 1])
    config = json.loads(path.read_bytes())
    assert config["mode"] == "production"
    assert config["hostAdapter"] == "canon-hermes-profile"
    assert config["hostConfiguration"]["profilesRoot"] == str(isolated_profile)
    durable_root = isolated_profile / "runtime/canon/durable"
    if command == "run":
        assert config["durableRoot"] == str(durable_root)
    else:
        # The real CLI accepts durableRoot only for run; other commands take explicit stores.
        assert "durableRoot" not in config
        assert config["checkpoint"] == {"backend": "sqlite", "db": str(durable_root / "checkpoint.sqlite")}
        assert config["artifact"] == {"backend": "local", "root": str(durable_root / "artifacts")}
        assert config["journal"] == {"backend": "sqlite", "db": str(durable_root / "journal.sqlite")}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    if command == "run":
        durable = build_cli_argv(command, {**args, "durable_root": "/explicit/durable"})
        assert "--host-config" not in durable
        assert durable[durable.index("--durable-root") + 1] == "/explicit/durable"


def test_argv_negative_control_unknown_argument_rejected_by_registered_schema():
    for entry in RegisteredContext().tools.values():
        schema = entry["schema"]["parameters"]
        # Consume the actual registered closed schema, independently of required fields.
        errors = list(Draft202012Validator(schema).iter_errors({"not_a_cli_parameter": "x"}))
        assert any(e.validator == "additionalProperties" for e in errors)


def direct_cli(args):
    return subprocess.run([sys.executable, "-I", "-m", "canon.cli", *args], capture_output=True, text=True, timeout=30)


@pytest.fixture
def real_context(isolated_profile, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    # Import the real built-ins only after the temporary profile is installed.
    import model_tools
    from tools.registry import registry
    assert registry.get_entry("terminal") is not None
    assert registry.get_entry("process_manage") is not None
    return RegisteredContext(real=True)


def minimal_workflow():
    spec = importlib.util.find_spec("canon")
    assert spec is not None and spec.origin is not None
    return Path(spec.origin).resolve().parents[2] / "tests/fixtures/workflows/minimal_pack/workflow.json"


@pytest.mark.parametrize("command", ["validate", "compile", "index", "inspect"])
def test_real_cli_stdout_and_error_are_unchanged(command, real_context, tmp_path):
    if command in {"validate", "compile"}:
        args = {"workflow": str(minimal_workflow())}
        cli_args = [command, args["workflow"]]
    else:
        (tmp_path / "artifacts").mkdir()
        args = {"journal_backend": "sqlite", "journal_db": str(tmp_path / "empty.sqlite"), "artifact_backend": "local", "artifact_root": str(tmp_path / "artifacts"), "mode": "production"}
        cli_args = [command, "--journal-backend", "sqlite", "--journal-db", args["journal_db"], "--artifact-backend", "local", "--artifact-root", args["artifact_root"], "--mode", "production"]
    direct = direct_cli(cli_args)
    if command == "inspect":
        assert direct.returncode != 0 and direct.stderr.startswith("FAIL: ")
    else:
        assert direct.returncode == 0, direct.stderr
    output = real_context.invoke("canon_" + command, {**args, "wait_mode": "sync-real", "timeout_seconds": 30})
    assert any(name == "terminal" for name, _ in real_context.calls)
    assert any(name == "process_manage" for name, _ in real_context.calls)
    if direct.returncode:
        assert json.loads(output) == {"error": transport._redact_cli_text(direct.stderr)[:512]}
    else:
        assert output == direct.stdout
        if command in {"compile", "index"}:
            assert json.loads(output) == json.loads(direct.stdout)


def test_real_cli_negative_control_invalid_workflow_matches_cli_error(real_context, tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}")
    direct = direct_cli(["validate", str(invalid)])
    assert direct.returncode != 0 and direct.stderr.startswith("FAIL: ")
    output = real_context.invoke("canon_validate", {"workflow": str(invalid), "timeout_seconds": 30})
    assert json.loads(output) == {"error": transport._redact_cli_text(direct.stderr)[:512]}


def test_real_cli_direct_negative_control_valid_and_invalid_workflow(tmp_path):
    valid = direct_cli(["validate", str(minimal_workflow())])
    assert valid.returncode == 0 and valid.stdout == "PASS: minimal-pack\n"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}")
    failed = direct_cli(["validate", str(invalid)])
    assert failed.returncode != 0 and failed.stderr.startswith("FAIL: ")


class LaunchRecorder:
    """Capture the real handler's manifest before its async transport returns."""

    def __init__(self):
        self.argv: list[str] = []
        self.carriers = {}

    def dispatch_tool(self, name, args, **kwargs):
        import shlex
        assert name == "terminal"
        manifest = json.loads(Path(shlex.split(args["command"])[-1]).read_text())
        self.argv = manifest["argv"]
        for key in JSON_PARAMETERS:
            flag = "--" + key.replace("_", "-")
            if flag in self.argv:
                path = Path(self.argv[self.argv.index(flag) + 1])
                if path.is_file():
                    self.carriers[key] = (path, path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        return {"session_id": "test-launch", "exit_code": 0, "notify_on_complete": True}


def recorded_launch(command, args, gateway_session_key):
    """Call the production handler, replacing only native process dispatch."""
    ctx = LaunchRecorder()
    output = asyncio.run(transport.handle_command(
        command, {**args, "wait_mode": "async-no-wait"}, ctx=ctx,
        gateway_session_key=gateway_session_key,
    ))
    assert json.loads(output).get("session_id") == "test-launch", output
    return ctx


@pytest.mark.parametrize("args,gateway,expected", [
    ({}, "gateway-key", "gateway-key"),
    ({"host_session": "bitrix-thread"}, "gateway-key", "bitrix-thread"),
    ({"host_session": None}, "gateway-key", None),
    ({}, None, None),
], ids=["gateway-default", "explicit-wins", "explicit-null", "outside-gateway"])
def test_host_session_default_run(args, gateway, expected, isolated_profile):
    ctx = recorded_launch("run", {"workflow": "opaque.json", **args}, gateway)
    if expected is None:
        assert "--host-session" not in ctx.argv
    else:
        assert ctx.argv.count("--host-session") == 1
        assert ctx.argv[ctx.argv.index("--host-session") + 1] == expected
    assert "--human-gate-delivery" not in ctx.argv


@pytest.mark.parametrize("command,explicit", [("inspect", None), ("index", None), ("index", "explicit-index")])
def test_host_session_default_negative_control_other_commands(command, explicit, isolated_profile):
    args = {} if explicit is None else {"host_session": explicit}
    ctx = recorded_launch(command, args, "gateway-key")
    if explicit is None:
        assert "--host-session" not in ctx.argv
    else:
        assert ctx.argv.count("--host-session") == 1
        assert ctx.argv[ctx.argv.index("--host-session") + 1] == explicit


def test_run_argv_workflow_input_object_carrier(isolated_profile):
    payload = {"task": "opaque", "not-a-declared-port": {"z": 1, "a": 2}}
    argv = build_cli_argv("run", {"workflow": "opaque.json", "workflow_input_json": payload})
    path = Path(argv[argv.index("--workflow-input-json") + 1])
    assert path.is_relative_to(isolated_profile)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == json.dumps(payload, sort_keys=False).encode("utf-8")


def test_run_argv_handler_new_parameters(isolated_profile):
    payload = {"task": "opaque", "not-a-declared-port": 17}
    ctx = recorded_launch("run", {
        "workflow": "opaque.json", "workflow_input_json": payload,
        "profile_id": "profile-marker", "max_graph_steps": 23,
        "host_session": "bitrix-thread", "human_gate_delivery": "verbatim delivery text",
    }, "gateway-key")
    for flag, value in [
        ("--profile-id", "profile-marker"), ("--max-graph-steps", "23"),
        ("--host-session", "bitrix-thread"), ("--human-gate-delivery", "verbatim delivery text"),
    ]:
        assert ctx.argv.count(flag) == 1
        assert ctx.argv[ctx.argv.index(flag) + 1] == value
    path, content, mode = ctx.carriers["workflow_input_json"]
    assert path.is_relative_to(isolated_profile)
    assert content == json.dumps(payload, sort_keys=False).encode("utf-8")
    assert mode == 0o600


def test_run_argv_negative_control_deleted_input_parameter_is_rejected():
    schema = deepcopy(RegisteredContext().tools["canon_run"]["schema"]["parameters"])
    del schema["properties"]["workflow_input_json"]
    with pytest.raises(ValidationError) as error:
        Draft202012Validator(schema).validate({
            "workflow": "opaque.json", "workflow_input_json": "/opaque/input.json",
        })
    assert error.value.validator == "additionalProperties"


def test_run_argv_negative_control_scalar_flags_and_input_path(isolated_profile, tmp_path):
    carrier = tmp_path / "input.json"
    carrier.write_text(json.dumps({"task": "test input"}))
    ctx = recorded_launch("run", {
        "workflow": "opaque.json", "workflow_input_json": str(carrier),
        "profile_id": "default", "max_graph_steps": 64,
        "human_gate_delivery": "delivery supplied independently",
    }, None)
    for flag, value in [
        ("--workflow-input-json", str(carrier)), ("--profile-id", "default"),
        ("--max-graph-steps", "64"), ("--human-gate-delivery", "delivery supplied independently"),
    ]:
        assert ctx.argv.count(flag) == 1
        assert ctx.argv[ctx.argv.index(flag) + 1] == value
    assert "--host-session" not in ctx.argv


@pytest.fixture
def binding_context(real_context, tmp_path, monkeypatch):
    """Run the real CLI with only its external phase host replaced; no provider calls."""
    import shutil
    canon_root = minimal_workflow().parents[4]
    pack = tmp_path / "minimal-pack"
    shutil.copytree(minimal_workflow().parent, pack)
    routing_path = pack / "policies/model-routing.json"
    routing = json.loads(routing_path.read_text())
    routing["profiles"]["default"]["hermesProfileId"] = "test-only-profile"
    routing_path.write_text(json.dumps(routing))
    real_context.workflow = str(pack / "workflow.json")
    bootstrap = tmp_path / "binding-bootstrap.py"
    bootstrap.write_text(
        "import sys, types\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"sys.path.insert(0, {str(canon_root / 'src')!r})\n"
        "import profile_runtime\n"
        "tests = types.ModuleType('tests')\n"
        f"tests.__path__ = [{str(canon_root / 'tests')!r}]\n"
        "sys.modules['tests'] = tests\n"
        "from tests.helpers.fake_host import FakeHostAdapter\n"
        "from canon.host_provider_registry import HostAdapterProviderRegistry\n"
        "def resolve(self, selector, request):\n"
        "    assert selector == 'canon-hermes-profile'\n"
        "    host = FakeHostAdapter(capabilities=set(request.capabilities), "
        "checkpoint_backend=request.store.checkpoint_backend, artifact_backend=request.store.artifact_backend)\n"
        "    host.journal = request.store.journal\n"
        "    return host\n"
        "HostAdapterProviderRegistry.resolve = resolve\n"
        "from canon.cli import main\n"
        "raise SystemExit(main(sys.argv[1:]))\n"
    )
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: [sys.executable, "-I", str(bootstrap)])
    return real_context


def test_real_run_binding_object_input_and_launch_binding(binding_context, tmp_path):
    output = binding_context.invoke("canon_run", {
        "workflow": binding_context.workflow, "workflow_input_json": {"task": "Summarize the request."},
        "execution_root": str(tmp_path), "profile_id": "default", "max_graph_steps": 100,
        "host_session": "bitrix-thread", "human_gate_delivery": "Deliver the approval to this thread.",
        "wait_mode": "sync-real", "timeout_seconds": 30,
    })
    result = json.loads(output)
    assert result.get("status") == "completed", result
    assert result["hostSession"] == "bitrix-thread"
    assert result["humanGateDelivery"] == "Deliver the approval to this thread."
    assert any(name == "terminal" for name, _ in binding_context.calls)
    assert any(name == "process_manage" for name, _ in binding_context.calls)


def test_real_run_binding_negative_control_without_binding(binding_context, tmp_path):
    carrier = tmp_path / "control-input.json"
    carrier.write_text(json.dumps({"task": "Summarize the request."}))
    output = binding_context.invoke("canon_run", {
        "workflow": binding_context.workflow, "workflow_input_json": str(carrier),
        "execution_root": str(tmp_path), "wait_mode": "sync-real", "timeout_seconds": 30,
    })
    result = json.loads(output)
    assert result.get("status") == "completed", result
    assert "hostSession" not in result
    assert "humanGateDelivery" not in result
    assert any(name == "terminal" for name, _ in binding_context.calls)
    assert any(name == "process_manage" for name, _ in binding_context.calls)
