"""Closed public schemas for the four ordinary Canon plugin tools."""

from __future__ import annotations

CANON_GATEWAY_LAUNCH_SCHEMA = {
    "name": "canon_gateway_launch",
    "description": (
        "Launch one existing Canon workflow through Hermes terminal and native process tracking. "
        "Synchronous modes wait for the native process; async-no-wait returns native acceptance only."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["workflow_name", "workflow_input", "execution_root"],
        "properties": {
            "workflow_name": {"type": "string", "minLength": 1, "description": "Absolute path to an existing Canon workflow JSON file."},
            "workflow_input": {"type": "object", "minProperties": 1, "description": "Non-empty workflow-owned input forwarded to Canon unchanged."},
            "execution_root": {"type": "string", "minLength": 1, "description": "Absolute path to an existing execution directory."},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600, "description": "Bounded native wait; defaults to 90 or 600 for sync-real."},
            "wait_mode": {"type": "string", "enum": ["sync-short", "sync-real", "async-no-wait"], "description": "Native Hermes wait mode."},
        },
    },
}

CANON_GATEWAY_CHECKPOINT_SCHEMA = {
    "name": "canon_gateway_checkpoint",
    "description": (
        "Perform Canon checkpoint-control through the active Hermes transport. "
        "For continue_paused, payload is one complete checkpoint-control-request.schema.v1 "
        "object forwarded to Canon unchanged; replay and inspection selectors remain explicit."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workflow_name": {"type": "string"},
            "mode": {"type": "string", "enum": ["history", "inspect", "continue_paused", "replay_from_checkpoint", "replay_from_node", "fork_from_checkpoint", "fork_from_node"]},
            "run_id": {"type": "string"},
            "thread_id": {"type": "string", "description": "Canon durable thread id for checkpoint authority."},
            "checkpoint_id": {"type": "string"},
            "node_id": {"type": "string"},
            "payload": {"type": "object"},
            "state_patch": {"type": "object"},
            "checkpoint_selector": {"type": "object", "additionalProperties": False, "properties": {"checkpointId": {"type": "string"}, "matchIndex": {"type": "integer", "minimum": 0}}},
            "lineage_mode": {"type": "string", "enum": ["fork", "same_run_replay"]},
            "reason": {"type": "string"},
            "safe_mode": {"type": "string", "enum": ["allow_replay_side_effects"]},
            "safeMode": {"type": "string", "enum": ["allow_replay_side_effects"]},
            "expected_gateway_git_head": {"type": "string"},
            "expectedGatewayGitHead": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
            "wait_mode": {"type": "string", "enum": ["sync-short", "sync-real", "async-no-wait"]},

        },
        "required": ["workflow_name", "mode", "run_id", "thread_id"],
        "additionalProperties": False,
        "allOf": [
            {
                "if": {"properties": {"mode": {"const": "continue_paused"}}, "required": ["mode"]},
                "then": {"required": ["payload"]},
            },
            {
                "if": {"properties": {"mode": {"enum": [
                    "history", "inspect", "replay_from_checkpoint", "fork_from_checkpoint",
                    "replay_from_node", "fork_from_node",
                ]}}, "required": ["mode"]},
                "then": {"not": {"required": ["payload"]}},
            },
        ],
    },
}

CANON_WORKFLOW_CONTROL_SCHEMA = {
    "name": "canon_workflow_control",
    "description": "Control an existing Canon workflow through durable operator authority.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["pause", "cancel", "restart"]},
            "run_id": {"type": "string", "description": "Durable Canon run id required for pause and cancel."},
            "thread_id": {"type": "string", "description": "Durable Canon thread id required for pause and cancel."},
            "checkpoint_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

CANON_WORKFLOW_OBSERVE_SCHEMA = {
    "name": "canon_workflow_observe",
    "description": "Read bounded Canon durable observability for an explicit origin.",
    "parameters": {
        "type": "object",
        "properties": {
            "surface": {"type": "string", "enum": ["latest", "list", "full", "inspect", "report", "timeline", "artifacts", "events"]},
            "run_id": {"type": "string", "minLength": 1},
            "after_sequence": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 0},
            "offset": {"type": "integer", "minimum": 0},
        },
        "required": ["surface"],
        "additionalProperties": False,
        "allOf": [
            {
                "if": {"properties": {"surface": {"const": "latest"}}, "required": ["surface"]},
                "then": {"properties": {"limit": {"const": 1}}},
            },
            {
                "if": {"properties": {"surface": {"enum": ["latest", "list"]}}, "required": ["surface"]},
                "then": {"not": {"anyOf": [
                    {"required": ["run_id"]},
                    {"required": ["after_sequence"]},
                ]}},
            },
            {
                "if": {"properties": {"surface": {"enum": ["full", "inspect", "report", "artifacts"]}}, "required": ["surface"]},
                "then": {"required": ["run_id"], "not": {"anyOf": [
                    {"required": ["after_sequence"]},
                    {"required": ["limit"]},
                    {"required": ["offset"]},
                ]}},
            },
            {
                "if": {"properties": {"surface": {"enum": ["timeline", "events"]}}, "required": ["surface"]},
                "then": {"required": ["run_id"], "not": {"required": ["offset"]}},
            },
        ],
    },
}

__all__ = [
    "CANON_GATEWAY_CHECKPOINT_SCHEMA",
    "CANON_GATEWAY_LAUNCH_SCHEMA",
    "CANON_WORKFLOW_CONTROL_SCHEMA",
    "CANON_WORKFLOW_OBSERVE_SCHEMA",
]
