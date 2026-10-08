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
JSON_PARAMETERS = {"request_json", "state_patch_json", "host_config", "backend"}


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
