"""P3 behavioural falsifiers for notification delivery through the real webhook."""

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest
import hashlib
import hmac
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from canon.schema_registry import SchemaRegistry
from gateway.config import Platform, PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource
from plugins.canon import transport


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO_ROOT / "plugins" / "canon" / "human_gate_hook.py"


async def invoke_hook(payload, home):
    """Run the actual hook with isolated profile state and stdin notification."""
    bootstrap = (
        "import runpy,sys;sys.path.insert(0,sys.argv[1]);"
        "runpy.run_path(sys.argv[2],run_name='__main__')"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", bootstrap, str(REPO_ROOT), str(HOOK_PATH),
        env={**os.environ, "HERMES_HOME": str(home)},
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        process.communicate(json.dumps(payload).encode()), timeout=30,
    )
    return process.returncode, stdout.decode(), stderr.decode()


SECRET = "isolated-canon-wake-secret"
KEY = "agent:main:local:dm:p3"
IDENTITY_KEYS = ("runId", "threadId", "checkpointId", "nodeId")
PROMPT_KEYS = ("gateId", "subjectRevision", "subjectDigest", "decisionAttempt", "actions")


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """Keep all runtime stores and config inside this test's profile."""
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("P3_WAKE_SECRET", SECRET)
    monkeypatch.setattr(transport, "get_default_hermes_root", lambda: home)
    return home


@pytest.fixture
def notification():
    identity = dict(runId="p3-run", threadId="p3-thread", checkpointId="p3-checkpoint", nodeId="review")
    card = "# Review pause\n\n  Preserve these spaces.\nПример карточки\n"
    payload = {
        "schemaVersion": "human-gate-notification.schema.v1",
        **identity, "workflowRef": "p3-workflow", "hostSession": KEY,
        "humanGateDelivery": "тест",
        "humanGatePrompt": {
            "type": "canon_human_gate_prompt", "status": "paused",
            "workflowRef": "p3-workflow", "gateId": "review",
            "actions": ["approve", "revise", "abort"], "resumeHandle": identity,
            "subjectRevision": 1, "subjectDigest": "sha256:" + "a" * 64,
            "decisionAttempt": 1, "cardBody": card,
            "cardBodyDigest": "sha256:" + hashlib.sha256(card.encode()).hexdigest(),
        },
    }
    SchemaRegistry().validator("human-gate-notification.schema.v1.json").validate(payload)
    return payload


@asynccontextmanager
async def wake_listener(home, *, route="canon-human-gate", secret_ref="${P3_WAKE_SECRET}"):
    """Use real HMAC, HTTP and session injection; only stored session/delivery are doubles."""
    now = datetime.now()
    entry = SessionEntry(
        session_key=KEY, session_id="p3-session", created_at=now, updated_at=now,
        platform=Platform.LOCAL,
        origin=SessionSource(platform=Platform.LOCAL, chat_id="p3", chat_type="dm", user_id="p3"),
    )
    store = SimpleNamespace(
        get=MagicMock(side_effect={KEY: entry}.get),
        lookup_by_session_key=MagicMock(side_effect={KEY: entry}.get),
        _is_session_ended_in_db=MagicMock(return_value=False), get_or_create_session=MagicMock(),
    )
    target = SimpleNamespace(handle_message=AsyncMock())
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = SimpleNamespace(
        _store=store, lookup_by_session_key=AsyncMock(side_effect={KEY: entry}.get),
        get_or_create_session=AsyncMock(),
    )
    runner._running = True
    runner._draining = False
    runner._background_tasks = set()
    runner.adapters = {Platform.LOCAL: target}
    runner._profile_adapters = {}
    runner._is_user_authorized_for_source = MagicMock(return_value=True)
    runner._adapter_for_source = MagicMock(return_value=target)
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
        "host": "127.0.0.1", "port": 0,
        "routes": {route: {"secret": SECRET, "wake_session": True}},
    }))
    adapter.gateway_runner = runner
    requests = []

    async def receive(request):
        requests.append((dict(request.headers), await request.read()))
        return await adapter._handle_webhook(request)

    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", receive)
    async with TestClient(TestServer(app)) as client:
        config = {
            "platforms": {"webhook": {"extra": {
                "host": "127.0.0.1", "port": client.server.port,
                "routes": {route: {"secret": secret_ref, "wake_session": True}},
            }}},
            "plugins": {"entries": {"canon": {"wake_route": route}}},
        }
        (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        try:
            yield client, target, runner, requests
        finally:
            if adapter._background_tasks:
                await asyncio.gather(*tuple(adapter._background_tasks))


def assert_wake_text(text, payload):
    """Verify delivered content and parse pause identity, not source or document markers."""
    prompt = payload["humanGatePrompt"]
    assert prompt["cardBody"] in text
    assert payload.get("humanGateDelivery", "не указан: спроси пользователя этой сессии") in text
    expected = {**{k: payload[k] for k in IDENTITY_KEYS}, **{k: prompt[k] for k in PROMPT_KEYS}}
    identities = []
    decoder = json.JSONDecoder()
    for start, character in enumerate(text):
        if character == "{":
            try:
                value, _ = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                continue
            identities.append(value)
    assert expected in identities
    assert "вызови clarify" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("route,delivery", [("canon-human-gate", "тест"), ("custom-p3", None)])
async def test_hook_signed_wake_delivers_card_and_identity(profile, notification, route, delivery):
    if delivery is None:
        del notification["humanGateDelivery"]
    async with wake_listener(profile, route=route) as (_, target, runner, requests):
        code, _, stderr = await invoke_hook(notification, profile)
        assert code == 0, stderr
        target.handle_message.assert_awaited_once()
        event = target.handle_message.await_args.args[0]
        assert_wake_text(event.text, notification)
        assert event.metadata["gateway_session_key"] == KEY
        assert event.metadata["hermes_wake_origin"] == "webhook:" + route
        assert event.internal is True
        assert len(requests) == 1
        headers, body = requests[0]
        assert headers["X-Request-ID"] == "p3-checkpoint:1"
        assert headers["X-Hub-Signature-256"] == "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        runner._async_session_store.get_or_create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_hook_no_host_session_rejects_without_request(profile, notification):
    del notification["hostSession"]
    async with wake_listener(profile) as (_, target, _, requests):
        code, _, stderr = await invoke_hook(notification, profile)
        assert code == 2
        assert stderr.strip() == "no host session bound"
        assert requests == []
        target.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_hook_wrong_secret_control_rejects_401(profile, notification):
    async with wake_listener(profile, secret_ref="wrong-p3-secret") as (_, target, _, requests):
        code, _, stderr = await invoke_hook(notification, profile)
        assert code == 1
        assert stderr.strip() == "HTTP 401"
        assert len(requests) == 1
        target.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_hook_adapter_existing_auth_control(profile):
    async with wake_listener(profile) as (client, target, _, _):
        body = json.dumps({"session_key": KEY, "text": "existing wake control"}).encode()
        for signature, status in [("0" * 64, 401), (hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(), 202)]:
            response = await client.post("/webhooks/canon-human-gate", data=body, headers={
                "X-Hub-Signature-256": "sha256=" + signature, "X-Request-ID": "existing-control",
            })
            await response.read()
            assert response.status == status
        target.handle_message.assert_awaited_once()
        assert target.handle_message.await_args.args[0].text == "existing wake control"


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["run", "checkpoint", "resume", "restart"])
async def test_host_config_generated_command_executes_hook(command, profile, notification):
    argv = transport.build_argv(command, {})
    config = json.loads(Path(argv[argv.index("--host-config") + 1]).read_bytes())
    hook = config.get("humanGateNotification")
    assert hook is not None, "generated host config does not configure humanGateNotification"
    assert hook["timeoutSeconds"] == 30
    assert hook["command"][:3] == [sys.executable, "-I", "-c"]
    assert hook["command"][-1] == str(HOOK_PATH)
    async with wake_listener(profile) as (_, target, _, _):
        process = await asyncio.create_subprocess_exec(
            *hook["command"], env={**os.environ, "HERMES_HOME": str(profile)},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(json.dumps(notification).encode()), 30)
        assert process.returncode == 0, stderr.decode()
        target.handle_message.assert_awaited_once()
        assert_wake_text(target.handle_message.await_args.args[0].text, notification)


@pytest.mark.parametrize("command", ["run", "checkpoint", "resume", "restart"])
def test_host_config_explicit_control_preserves_bytes(command, profile, tmp_path):
    explicit = tmp_path / "explicit.json"
    original = b'{\n  "mode": "production", "custom": [2, 1]\n}\n'
    explicit.write_bytes(original)
    argv = transport.build_argv(command, {"host_config": str(explicit)})
    assert argv[argv.index("--host-config") + 1] == str(explicit)
    assert explicit.read_bytes() == original


def test_host_config_empty_durable_root_keeps_notification(profile):
    # An empty, provisioned store must not control whether notification is configured.
    durable = profile / "runtime/canon/durable"
    durable.mkdir(parents=True)
    argv = transport.build_argv("run", {})
    config = json.loads(Path(argv[argv.index("--host-config") + 1]).read_bytes())
    hook = config.get("humanGateNotification")
    assert hook is not None, "empty durable store must still configure notification"
    assert hook["command"][-1] == str(HOOK_PATH)


@pytest.fixture
def end_to_end_context(profile, tmp_path, monkeypatch):
    """Keep Canon real; replace only its external phase-host provider in the child."""
    from tests.plugins.test_canon_plugin_cli_parity import RegisteredContext, minimal_workflow
    import model_tools  # Register the real native terminal/process tools.

    canon_root = minimal_workflow().parents[4]
    bootstrap = tmp_path / "p3-phase-host-bootstrap.py"
    bootstrap.write_text(
        "import sys, types\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
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
        "raise SystemExit(main(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: [sys.executable, "-I", str(bootstrap)])
    return RegisteredContext(real=True)


async def run_and_continue(end_to_end_context, tmp_path):
    """Exercise canon_run and consume its real pause identity in continue-paused."""
    from tests.plugins.test_canon_plugin_journal_e2e import _human_gate_pack, _checkpoint_payload

    pack = _human_gate_pack(tmp_path)
    (tmp_path / "execution").mkdir(exist_ok=True)
    launch = json.loads(await asyncio.to_thread(end_to_end_context.invoke, "canon_run", {
        "workflow": str(pack / "workflow.json"),
        "workflow_input_json": {"task": "P3 notification delivery"},
        "execution_root": str(tmp_path / "execution"),
        "host_session": KEY, "human_gate_delivery": "тест",
        "wait_mode": "sync-real", "timeout_seconds": 30,
    }))
    assert launch.get("status") == "paused", launch
    assert launch["hostSession"] == KEY
    assert launch["humanGateDelivery"] == "тест"
    prompt = launch["humanGatePrompt"]
    handle = prompt["resumeHandle"]
    payload = _checkpoint_payload(pack, prompt, {"action": "approve"}, "p3-continue")
    # Use the same input/execution authority as the actual launch, not fixture defaults.
    payload["workflowInput"] = {"task": "P3 notification delivery"}
    payload["runConfiguration"]["executionRoot"] = str(tmp_path / "execution")
    continued = json.loads(await asyncio.to_thread(end_to_end_context.invoke, "canon_checkpoint", {
        "workflow": str(pack / "workflow.json"), "subcommand": "continue-paused",
        "run_id": handle["runId"], "thread_id": handle["threadId"],
        "request_json": payload, "wait_mode": "sync-real", "timeout_seconds": 30,
    }))
    assert continued.get("status") == "completed", continued
    assert continued["runId"] == handle["runId"]
    assert continued["threadId"] == handle["threadId"]
    return launch


@pytest.mark.asyncio
async def test_end_to_end_delivered_pause_and_continue(profile, tmp_path, end_to_end_context):
    """Generated config must execute the real hook and deliver one authenticated wake."""
    async with wake_listener(profile) as (_, target, runner, requests):
        launch = await run_and_continue(end_to_end_context, tmp_path)
        assert launch["humanGateNotification"]["status"] == "delivered", launch["humanGateNotification"]
        target.handle_message.assert_awaited_once()
        event = target.handle_message.await_args.args[0]
        prompt = launch["humanGatePrompt"]
        payload = {
            **prompt["resumeHandle"], "humanGatePrompt": prompt,
            "humanGateDelivery": "тест",
        }
        assert_wake_text(event.text, payload)
        assert event.metadata["gateway_session_key"] == KEY
        assert event.internal is True
        assert len(requests) == 1
        headers, body = requests[0]
        assert headers["X-Request-ID"] == f"{prompt['resumeHandle']['checkpointId']}:{prompt['decisionAttempt']}"
        assert headers["X-Hub-Signature-256"] == "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        runner._async_session_store.get_or_create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_end_to_end_stopped_adapter_control_still_continues(profile, tmp_path, end_to_end_context):
    """Notification failure must not invalidate a durable pause or its continuation."""
    async with wake_listener(profile) as (client, target, _, requests):
        await client.close()
        launch = await run_and_continue(end_to_end_context, tmp_path)
        target.handle_message.assert_not_awaited()
        assert requests == []
        assert launch["humanGateNotification"]["status"] == "failed", launch["humanGateNotification"]
