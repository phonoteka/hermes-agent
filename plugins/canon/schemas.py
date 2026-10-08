"""Closed command schemas mirroring Canon's CLI parser, without semantic rules."""

from __future__ import annotations

_JSON_PARAMETERS = {"request_json", "state_patch_json", "host_config", "backend"}
_BACKENDS = "checkpoint_backend checkpoint_db artifact_backend artifact_root journal_backend journal_db"
_CHECKPOINT = "mode host_adapter host_config request_json request_file " + _BACKENDS + " run_id thread_id reason workflow"
_COMMAND_PARAMETERS = {
    "validate": "workflow",
    "compile": "workflow out",
    "run": "mode host_adapter host_config durable_root request_json input workflow_input_json profile_id max_graph_steps execution_root host_session human_gate_delivery workflow",
    "stream": "mode journal_backend journal_db run_id",
    "checkpoint": _CHECKPOINT + " checkpoint_id node_id state_patch_json subcommand",
    "resume": "checkpoint_mode " + _CHECKPOINT + " checkpoint_id resume_node_id state_patch_json",
    "pause": "mode " + _BACKENDS + " run_id thread_id",
    "cancel": "mode " + _BACKENDS + " run_id thread_id",
    "restart": "mode host_adapter host_config " + _BACKENDS + " checkpoint_id",
    "inspect": "mode backend artifact_backend artifact_root journal_backend journal_db surface run_id after_sequence limit workflow",
    "index": "mode backend artifact_backend artifact_root journal_backend journal_db host_session limit offset",
}
_ENUMS = {
    "checkpoint_backend": ["none", "sqlite"],
    "journal_backend": ["none", "sqlite"],
    "artifact_backend": ["none", "local"],
    "surface": ["full", "inspect", "report", "timeline", "artifacts", "events"],
    "subcommand": ["history", "inspect", "replay", "replay-node", "fork", "fork-node", "continue-paused"],
    "checkpoint_mode": ["continue_paused", "replay_from_checkpoint", "replay_from_node", "fork_from_checkpoint", "fork_from_node"],
}
_INTEGER_PARAMETERS = {"max_graph_steps", "after_sequence", "limit", "offset"}

TOOL_SCHEMAS = {}
for _command, _parameters in _COMMAND_PARAMETERS.items():
    _properties = {}
    for _key in _parameters.split():
        _property = {"type": "integer" if _key in _INTEGER_PARAMETERS else "string"}
        if _key in _JSON_PARAMETERS:
            _property = {"anyOf": [{"type": "string"}, {"type": "object"}]}
        if _key in _ENUMS:
            _property["enum"] = _ENUMS[_key]
        if _key == "mode":
            _property["enum"] = ["production"] if _command in {"run", "checkpoint", "resume", "restart"} else ["development", "production"]
        _properties[_key] = _property
    _properties.update({
        "wait_mode": {"type": "string", "enum": ["sync-short", "sync-real", "async-no-wait"]},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
    })
    _required = ["workflow"] if _command in {"validate", "compile", "run", "checkpoint", "resume"} else []
    if _command == "checkpoint":
        _required.append("subcommand")
    TOOL_SCHEMAS[_command] = {
        "name": "canon_" + _command,
        "description": "Execute Canon CLI " + _command + "; Canon owns validation. Returns CLI stdout or a redacted error.",
        "parameters": {"type": "object", "properties": _properties, "required": _required, "additionalProperties": False},
    }

__all__ = ["TOOL_SCHEMAS"]
