"""Behavior tests for per-agent gateway session context in plugin dispatch."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from agent.agent_runtime_helpers import invoke_tool
from agent.tool_executor import execute_tool_calls_sequential
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from run_agent import AIAgent


@pytest.fixture
def registered_probe():
    """Register a probe through the real PluginContext ownership path."""
    name = f"_test_gateway_context_probe_{uuid4().hex}"
    calls = []
    manager = PluginManager()
    context = PluginContext(
        PluginManifest(name=name, key=name, source="user"),
        manager,
    )

    def handler(args, **kwargs):
        calls.append(dict(kwargs))
        return json.dumps({"ok": True})

    registration = context.register_tool(
        name=name,
        toolset="debugging",
        schema={
            "name": name,
            "description": "Test plugin context probe",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=handler,
    )
    assert registration is not None
    try:
        yield name, calls
    finally:
        registration.dispose()


def _agent(tool_name, **attrs):
    defaults = {
        "valid_tool_names": {tool_name},
        "session_id": "session-for-test",
        "_memory_manager": None,
        "enabled_toolsets": None,
        "disabled_toolsets": None,
        "_current_turn_id": "",
        "_current_api_request_id": "",
    }
    defaults.update(attrs)
    agent = AIAgent.__new__(AIAgent)
    for name, value in defaults.items():
        setattr(agent, name, value)
    return agent


def _tool_call(name):
    return SimpleNamespace(
        id=f"call-{uuid4().hex}",
        function=SimpleNamespace(name=name, arguments="{}"),
    )


@pytest.fixture
def runtime_agent():
    """A real AIAgent with network/model setup replaced."""
    name = "_gateway_context_runtime_probe"
    tool_def = {
        "type": "function",
        "function": {
            "name": name,
            "description": "Test tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    with (
        patch("run_agent.get_tool_definitions", return_value=[tool_def]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    return agent


def test_registered_plugin_handler_receives_exact_gateway_session_key(registered_probe):
    tool_name, calls = registered_probe
    agent = _agent(tool_name, _gateway_session_key="gateway:exact-key")

    result = invoke_tool(agent, tool_name, {}, "task-exact")

    assert json.loads(result) == {"ok": True}
    assert calls[0]["gateway_session_key"] == "gateway:exact-key"


def test_non_gateway_agent_passes_none_to_registered_plugin_handler(registered_probe):
    tool_name, calls = registered_probe
    agent = _agent(tool_name)

    invoke_tool(agent, tool_name, {}, "task-none")

    assert calls[0]["gateway_session_key"] is None


def test_distinct_agents_keep_gateway_session_keys_isolated(registered_probe):
    tool_name, calls = registered_probe
    first = _agent(tool_name, _gateway_session_key="gateway:first")
    second = _agent(tool_name, _gateway_session_key="gateway:second")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(invoke_tool, first, tool_name, {}, "task-first"),
            executor.submit(invoke_tool, second, tool_name, {}, "task-second"),
        ]
        for future in futures:
            assert json.loads(future.result()) == {"ok": True}

    observed = {call["task_id"]: call["gateway_session_key"] for call in calls}
    assert observed == {
        "task-first": "gateway:first",
        "task-second": "gateway:second",
    }


def test_quiet_sequential_executor_preserves_gateway_session_key(
    registered_probe, runtime_agent
):
    tool_name, calls = registered_probe
    runtime_agent.valid_tool_names = {tool_name}
    runtime_agent._gateway_session_key = "gateway:quiet-sequential"
    runtime_agent._execute_tool_calls_sequential(
        SimpleNamespace(tool_calls=[_tool_call(tool_name)]),
        [],
        "task-quiet-sequential",
    )

    assert calls[0]["gateway_session_key"] == "gateway:quiet-sequential"


def test_nonquiet_sequential_executor_preserves_gateway_session_key(
    registered_probe, runtime_agent
):
    tool_name, calls = registered_probe
    runtime_agent.valid_tool_names = {tool_name}
    runtime_agent.quiet_mode = False
    runtime_agent._gateway_session_key = "gateway:nonquiet-sequential"
    runtime_agent._execute_tool_calls_sequential(
        SimpleNamespace(tool_calls=[_tool_call(tool_name)]),
        [],
        "task-nonquiet-sequential",
    )

    assert calls[0]["gateway_session_key"] == "gateway:nonquiet-sequential"


def test_deferred_tool_call_bridge_preserves_gateway_session_key(registered_probe):
    tool_name, calls = registered_probe
    result = __import__("model_tools").handle_function_call(
        "tool_call",
        {"name": tool_name, "arguments": {}},
        task_id="task-bridge",
        session_id="session-bridge",
        gateway_session_key="gateway:bridge",
    )

    assert json.loads(result) == {"ok": True}
    assert calls[0]["gateway_session_key"] == "gateway:bridge"
