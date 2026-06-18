"""Tests for the agent-facing Canon workflow control tool surface."""
from __future__ import annotations

import importlib
import json
from typing import Any


def test_canon_workflow_control_tool_registers_and_exposes_workflow_actions() -> None:
    """Tool import must register the workflow control surface with the bounded public schema.

    pre: canon_workflow_control is a core Hermes tool for agent-facing workflow control only.
    post: schema exposes only workflow-level action/origin/run/checkpoint/reason fields and core toolset exposure.
    raises: AssertionError when the tool is missing, unregistered, or leaks forbidden control knobs.
    """

    tool_module = importlib.import_module("tools.canon_workflow_control_tool")
    registry_module = importlib.import_module("tools.registry")
    toolsets_module = importlib.import_module("toolsets")

    entry = registry_module.registry.get_entry("canon_workflow_control")
    assert tool_module is not None
    assert entry is not None
    assert entry.name == "canon_workflow_control"
    assert "canon_workflow_control" in getattr(toolsets_module, "_HERMES_CORE_TOOLS")

    parameters = tool_module.CANON_WORKFLOW_CONTROL_SCHEMA["parameters"]
    properties = parameters["properties"]
    assert set(properties) == {"action", "origin", "run_id", "checkpoint_id", "reason"}
    assert parameters["required"] == ["action", "origin"]
    assert parameters["additionalProperties"] is False
    assert properties["action"]["enum"] == ["pause", "resume", "cancel", "restart"]
    assert "phaseId" not in properties
    assert "nodeId" not in properties
    assert "start_phase" not in properties
    assert "resume_phase" not in properties
    assert "cancel_phase" not in properties
    assert "inspect_phase" not in properties
    assert "active_handle" not in properties
    assert "cancel_active_handle" not in properties


def test_canon_workflow_control_tool_dispatches_workflow_actions(monkeypatch) -> None:
    """Handler must dispatch each public action to the matching workflow facade method."""

    module = importlib.import_module("tools.canon_workflow_control_tool")
    observed: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    durable_stores = {"journal": object(), "artifacts": object(), "checkpoint_backend": object()}

    class _Facade:
        @staticmethod
        def pause_workflow(payload: dict[str, Any], stores: dict[str, Any] | None = None) -> dict[str, Any]:
            observed.append(("pause", payload, stores or {}))
            return {"runId": payload["runId"], "eventKind": "workflow.pause.requested"}

        @staticmethod
        def resume_workflow(payload: dict[str, Any], stores: dict[str, Any] | None = None) -> dict[str, Any]:
            observed.append(("resume", payload, stores or {}))
            return {"runId": payload["runId"], "eventKind": "workflow.resume.requested"}

        @staticmethod
        def cancel_workflow(payload: dict[str, Any], stores: dict[str, Any] | None = None) -> dict[str, Any]:
            observed.append(("cancel", payload, stores or {}))
            return {"runId": payload["runId"], "eventKind": "workflow.cancel.requested"}

        @staticmethod
        def restart_workflow_from_checkpoint(
            payload: dict[str, Any], stores: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            observed.append(("restart", payload, stores or {}))
            return {"checkpointId": payload["checkpointId"], "eventKind": "workflow.restart.requested"}

    monkeypatch.setattr(module, "_load_workflow_facade", lambda: _Facade)
    monkeypatch.setattr(module, "_resolve_durable_stores", lambda: durable_stores)

    pause = json.loads(
        module._handle_tool({"action": "pause", "origin": "telegram:-100123", "run_id": "run-1", "reason": "operator"})
    )
    resume = json.loads(module._handle_tool({"action": "resume", "origin": "telegram:-100123", "run_id": "run-1"}))
    cancel = json.loads(module._handle_tool({"action": "cancel", "origin": "telegram:-100123", "run_id": "run-1"}))
    restart = json.loads(
        module._handle_tool({"action": "restart", "origin": "telegram:-100123", "checkpoint_id": "checkpoint-1"})
    )

    assert [item[0] for item in observed] == ["pause", "resume", "cancel", "restart"]
    assert all(item[2] is durable_stores for item in observed)
    assert observed[0][1] == {"origin": "telegram:-100123", "runId": "run-1", "reason": "operator"}
    assert observed[1][1] == {"origin": "telegram:-100123", "runId": "run-1"}
    assert observed[2][1] == {"origin": "telegram:-100123", "runId": "run-1"}
    assert observed[3][1] == {"origin": "telegram:-100123", "checkpointId": "checkpoint-1"}

    assert pause == {
        "success": True,
        "action": "pause",
        "origin": "telegram:-100123",
        "run_id": "run-1",
        "result": {"runId": "run-1", "eventKind": "workflow.pause.requested"},
    }
    assert resume["success"] is True
    assert resume["action"] == "resume"
    assert resume["run_id"] == "run-1"
    assert cancel["success"] is True
    assert cancel["action"] == "cancel"
    assert cancel["run_id"] == "run-1"
    assert restart == {
        "success": True,
        "action": "restart",
        "origin": "telegram:-100123",
        "checkpoint_id": "checkpoint-1",
        "result": {"checkpointId": "checkpoint-1", "eventKind": "workflow.restart.requested"},
    }


def test_canon_workflow_control_tool_fails_closed_for_invalid_input_without_facade_calls(monkeypatch) -> None:
    """Invalid public inputs must fail closed before any facade control call occurs."""

    module = importlib.import_module("tools.canon_workflow_control_tool")
    call_count = {"count": 0}

    class _Facade:
        @staticmethod
        def pause_workflow(payload: dict[str, Any], stores: dict[str, Any] | None = None) -> dict[str, Any]:
            call_count["count"] += 1
            return {}

    monkeypatch.setattr(module, "_load_workflow_facade", lambda: _Facade)
    monkeypatch.setattr(module, "_resolve_durable_stores", lambda: {"journal": object(), "artifacts": object(), "checkpoint_backend": object()})

    invalid_action = json.loads(module._handle_tool({"action": "inspect", "origin": "telegram:-100123", "run_id": "run-1"}))
    missing_run = json.loads(module._handle_tool({"action": "pause", "origin": "telegram:-100123"}))
    unexpected_field = json.loads(
        module._handle_tool({"action": "cancel", "origin": "telegram:-100123", "run_id": "run-1", "active_handle": "forbidden"})
    )

    assert invalid_action["success"] is False
    assert "action" in invalid_action["error"]
    assert missing_run["success"] is False
    assert "run_id" in missing_run["error"]
    assert unexpected_field["success"] is False
    assert "unsupported" in unexpected_field["error"]
    assert call_count["count"] == 0
