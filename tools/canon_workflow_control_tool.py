"""Agent-callable Canon durable workflow control surface.

This tool exposes only workflow-level pause/resume/cancel/restart controls over
current-gateway durable truth. It does not expose phase-level control fields or
parallel launch authority.
"""

from __future__ import annotations

import json
from typing import Any

from tools.registry import registry

_ALLOWED_ACTIONS = ("pause", "resume", "cancel", "restart")
_PUBLIC_RESTART_CHECKPOINT_CLASS = "public-gate-resume-alias"
_CHECKPOINT_IDENTITY_CLASSES = (
    _PUBLIC_RESTART_CHECKPOINT_CLASS,
    "current-gateway-run-checkpoint",
    "current-gateway-callback-token-checkpoint",
    "runtime-checkpoint-evidence",
)
_ALLOWED_ARGUMENTS = frozenset({"action", "origin", "run_id", "checkpoint_id", "checkpoint_identity_class", "reason"})

CANON_WORKFLOW_CONTROL_SCHEMA = {
    "name": "canon_workflow_control",
    "description": (
        "Control an existing Canon workflow through durable current-gateway operator authority. "
        "Supports workflow-level pause, resume, cancel, and restart from checkpoint only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["pause", "resume", "cancel", "restart"],
                "description": "Workflow-level control action.",
            },
            "origin": {
                "type": "string",
                "description": "Required origin scope (for example: telegram:<chat_id>).",
            },
            "run_id": {
                "type": "string",
                "description": "Required run id for pause, resume, and cancel.",
            },
            "checkpoint_id": {
                "type": "string",
                "description": "Required checkpoint id for restart.",
            },
            "checkpoint_identity_class": {
                "type": "string",
                "enum": list(_CHECKPOINT_IDENTITY_CLASSES),
                "description": (
                    "Optional checkpoint identity class for restart. Only public-gate-resume-alias "
                    "is accepted as workflow restart authority; evidence-only classes fail closed."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Optional operator-visible reason for pause, resume, or cancel.",
            },
        },
        "required": ["action", "origin"],
        "additionalProperties": False,
    },
}


def _load_workflow_facade():
    """Load the Canon Hermes workflow facade.

    pre: Canon Hermes integration modules are importable.
    post: returns the public workflow facade module containing workflow-level control helpers.
    raises: ImportError when Canon Hermes integration modules are unavailable.
    """

    from integrations.hermes.canon_hermes import workflow_facade

    return workflow_facade



def _resolve_durable_stores() -> dict[str, Any]:
    """Resolve durable stores from current-gateway configuration.

    pre: Canon current-gateway runner config is importable and exposes journal/artifact/checkpoint paths.
    post: returns journal, artifacts, and checkpoint_backend bound to the current-gateway durable store.
    raises: ImportError when Canon durability/config modules are unavailable.
    """

    from canon.durability import LocalArtifactBackend, SqliteCheckpointBackend
    from canon.journal import SqliteExecutionJournal
    from integrations.hermes.canon_hermes.current_gateway_runner import build_current_gateway_runner_config

    config = build_current_gateway_runner_config()
    return {
        "journal": SqliteExecutionJournal(config.journal_path),
        "artifacts": LocalArtifactBackend(config.artifacts_dir),
        "checkpoint_backend": SqliteCheckpointBackend(config.checkpoint_path),
    }



def _check_requirements() -> bool:
    """Expose the tool only when current-gateway durable config resolves.

    pre: none.
    post: returns True only if current-gateway durable config imports and path resolution work.
    raises: none.
    """

    try:
        from integrations.hermes.canon_hermes.current_gateway_runner import build_current_gateway_runner_config

        config = build_current_gateway_runner_config()
        return bool(
            getattr(config, "journal_path", None)
            and getattr(config, "artifacts_dir", None)
            and getattr(config, "checkpoint_path", None)
        )
    except Exception:
        return False



def _reject_unexpected_args(args: dict[str, Any]) -> None:
    """Fail closed when callers send unsupported public fields.

    pre: args is the raw tool argument mapping.
    post: returns only when every supplied field is part of the public tool contract.
    raises: ValueError when unsupported fields are present.
    """

    unexpected_fields = sorted(name for name in args if name not in _ALLOWED_ARGUMENTS)
    if unexpected_fields:
        raise ValueError(f"unsupported fields: {', '.join(unexpected_fields)}")



def _normalize_action(value: Any) -> str:
    """Normalize one workflow control action.

    pre: value is JSON-like user input.
    post: returns one public workflow-level control action.
    raises: ValueError when the action is missing or outside the public contract.
    """

    action = str(value or "").strip().lower()
    if action not in _ALLOWED_ACTIONS:
        raise ValueError("action must be one of: pause, resume, cancel, restart")
    return action



def _normalize_origin(value: Any) -> str:
    """Normalize and validate one origin string.

    pre: value is JSON-like user input.
    post: returns a non-empty origin string containing a ':' separator.
    raises: ValueError when origin is missing or malformed.
    """

    origin = str(value or "").strip()
    if not origin:
        raise ValueError("origin is required")
    if ":" not in origin:
        raise ValueError("origin must include ':' separator (example: telegram:<chat_id>)")
    return origin



def _normalize_optional_reason(value: Any) -> str | None:
    """Normalize one optional operator reason.

    pre: value is JSON-like user input.
    post: returns None for absent/blank values, otherwise a stripped string.
    raises: none.
    """

    reason = str(value or "").strip()
    return reason or None


def _normalize_checkpoint_identity_class(value: Any) -> str | None:
    """Normalize one optional checkpoint identity class.

    pre: value is JSON-like caller input.
    post: returns a known checkpoint identity class or None when omitted.
    raises: ValueError when the class is outside the public Canon checkpoint taxonomy.
    """

    text = str(value or "").strip()
    if not text:
        return None
    if text not in _CHECKPOINT_IDENTITY_CLASSES:
        raise ValueError("checkpoint_identity_class must be one of: " + ", ".join(_CHECKPOINT_IDENTITY_CLASSES))
    return text


def _validate_checkpoint_identity_for_action(action: str, checkpoint_identity_class: str | None) -> None:
    """Fail closed when checkpoint identity metadata would widen workflow control authority.

    pre: action is normalized and checkpoint_identity_class is either None or a known taxonomy class.
    post: returns only when the selected action can legally carry the supplied class.
    raises: ValueError when non-restart actions carry checkpoint class metadata or restart uses evidence-only classes.
    """

    if checkpoint_identity_class is None:
        return
    if action != "restart":
        raise ValueError("checkpoint_identity_class is only accepted for restart")
    if checkpoint_identity_class != _PUBLIC_RESTART_CHECKPOINT_CLASS:
        raise ValueError("restart accepts only checkpoint_identity_class=public-gate-resume-alias")


def _normalize_required_text(value: Any, field_name: str) -> str:
    """Normalize one required non-empty text field.

    pre: value is JSON-like user input.
    post: returns a stripped non-empty string.
    raises: ValueError when the field is missing or blank.
    """

    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text



def _build_facade_payload(
    *, action: str, origin: str, run_id: Any, checkpoint_id: Any, checkpoint_identity_class: Any, reason: Any
) -> dict[str, Any]:
    """Build one workflow-facade payload from validated public tool inputs.

    pre: action and origin are normalized public control values.
    post: returns only workflow-level facade fields needed for the selected action.
    raises: ValueError when the selected action is missing its required run/checkpoint identifier or widens checkpoint authority.
    """

    payload: dict[str, Any] = {"origin": origin}
    normalized_reason = _normalize_optional_reason(reason)
    normalized_checkpoint_class = _normalize_checkpoint_identity_class(checkpoint_identity_class)
    _validate_checkpoint_identity_for_action(action, normalized_checkpoint_class)
    if action == "restart":
        payload["checkpointId"] = _normalize_required_text(checkpoint_id, "checkpoint_id")
        payload["checkpointIdentityClass"] = normalized_checkpoint_class or _PUBLIC_RESTART_CHECKPOINT_CLASS
        return payload
    payload["runId"] = _normalize_required_text(run_id, "run_id")
    if normalized_reason is not None:
        payload["reason"] = normalized_reason
    return payload



def _dispatch_action(*, action: str, payload: dict[str, Any], stores: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one validated workflow control action to the Canon workflow facade.

    pre: action is normalized and payload/stores already passed public validation.
    post: calls exactly one matching workflow-level facade method and returns its detached result.
    raises: Exception from the underlying facade call.
    """

    workflow_facade = _load_workflow_facade()
    if action == "pause":
        return workflow_facade.pause_workflow(payload, stores)
    if action == "resume":
        return workflow_facade.resume_workflow(payload, stores)
    if action == "cancel":
        return workflow_facade.cancel_workflow(payload, stores)
    return workflow_facade.restart_workflow_from_checkpoint(payload, stores)



def _success_payload(*, action: str, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Build one tool-success payload with bounded public identity fields.

    pre: payload is the selected facade payload and result is the detached facade result.
    post: returns JSON-serializable tool output containing success, action, origin, scoped identity, and result.
    raises: none.
    """

    response: dict[str, Any] = {
        "success": True,
        "action": action,
        "origin": str(payload["origin"]),
        "result": result,
    }
    if "runId" in payload:
        response["run_id"] = str(payload["runId"])
    if "checkpointId" in payload:
        response["checkpoint_id"] = str(payload["checkpointId"])
    if "checkpointIdentityClass" in payload:
        response["checkpoint_identity_class"] = str(payload["checkpointIdentityClass"])
    return response



def _handle_tool(args: dict[str, Any], **_: Any) -> str:
    """Run one Canon workflow control request as a JSON-returning Hermes tool.

    pre: args matches CANON_WORKFLOW_CONTROL_SCHEMA or is a close variant from a caller.
    post: returns JSON object with success/action/origin plus run/checkpoint identity and detached result.
    raises: none; failures are encoded in the JSON payload.
    """

    try:
        _reject_unexpected_args(args)
        action = _normalize_action(args.get("action"))
        origin = _normalize_origin(args.get("origin"))
        payload = _build_facade_payload(
            action=action,
            origin=origin,
            run_id=args.get("run_id"),
            checkpoint_id=args.get("checkpoint_id"),
            checkpoint_identity_class=args.get("checkpoint_identity_class"),
            reason=args.get("reason"),
        )
        stores = _resolve_durable_stores()
        result = _dispatch_action(action=action, payload=payload, stores=stores)
        return json.dumps(_success_payload(action=action, payload=payload, result=result), ensure_ascii=False)
    except Exception as exc:
        action = str(args.get("action") or "").strip().lower() or "unknown"
        failure: dict[str, Any] = {
            "success": False,
            "action": action,
            "error": str(exc),
        }
        origin = str(args.get("origin") or "").strip()
        if origin:
            failure["origin"] = origin
        run_id = str(args.get("run_id") or "").strip()
        if run_id:
            failure["run_id"] = run_id
        checkpoint_id = str(args.get("checkpoint_id") or "").strip()
        if checkpoint_id:
            failure["checkpoint_id"] = checkpoint_id
        return json.dumps(failure, ensure_ascii=False)


registry.register(
    name="canon_workflow_control",
    toolset="messaging",
    schema=CANON_WORKFLOW_CONTROL_SCHEMA,
    handler=_handle_tool,
    check_fn=_check_requirements,
    emoji="⏯️",
)
