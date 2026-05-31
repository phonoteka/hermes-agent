"""Gateway-side Canon current-gateway review mechanics.

This module is deliberately non-agentic: Telegram button callbacks call it directly
from the gateway adapter, and it records the operator decision in Canon's durable
current-gateway store without creating a Hermes agent turn.
"""

from __future__ import annotations

from typing import Any


_CHOICE_TO_DECISION = {
    "y": "approved",
    "n": "rejected",
    "e": "corrections_requested",
}

_ACTION_TO_DECISION = {
    "approve": "approved",
    "reject": "rejected",
    "revise": "corrections_requested",
}


def _normalize_decision(*, choice: str = "", action_id: str = "") -> str:
    """Resolve one Canon decision from either legacy choice or declared action id.

    pre: at least one of ``choice`` or ``action_id`` names a supported Canon review action.
    post: returns the durable Canon decision string used by current-gateway recorder.
    raises: ValueError when neither input maps to a supported review action.
    """

    normalized_choice = str(choice or "").strip().lower()
    if normalized_choice:
        decision = _CHOICE_TO_DECISION.get(normalized_choice)
        if decision is None:
            raise ValueError("unknown Canon review choice")
        return decision
    normalized_action_id = str(action_id or "").strip().lower()
    decision = _ACTION_TO_DECISION.get(normalized_action_id)
    if decision is None:
        raise ValueError("unknown Canon review action")
    return decision


def _derive_run_id(*, run_id: str = "", gate_id: str = "") -> str:
    """Return one durable run id from explicit run_id or projected gate identity.

    pre: either ``run_id`` is non-empty or ``gate_id`` starts with Canon's checkpoint id prefix
         ``<runId>:...``.
    post: returns the normalized run id and rejects mismatched dual authority.
    raises: ValueError when no run id authority is available or the authorities disagree.
    """

    normalized_run_id = str(run_id or "").strip()
    normalized_gate_id = str(gate_id or "").strip()
    derived_run_id = normalized_gate_id.split(":", 1)[0] if normalized_gate_id else ""
    if normalized_run_id and derived_run_id and normalized_run_id != derived_run_id:
        raise ValueError("run_id does not match gate_id")
    resolved_run_id = normalized_run_id or derived_run_id
    if not resolved_run_id:
        raise ValueError("run_id or gate_id is required")
    return resolved_run_id


def resolve_telegram_canon_review(
    *,
    run_id: str = "",
    choice: str = "",
    gate_id: str = "",
    action_id: str = "",
    actor_id: str,
    actor_name: str = "",
    chat_id: str = "",
    thread_id: str | None = None,
    message_id: str = "",
    revision_instructions: str = "",
    sender: Any | None = None,
) -> str:
    """Persist one Telegram review decision for a Canon current-gateway run.

    pre: either legacy ``run_id``/``choice`` or declared ``gate_id``/``action_id`` identifies
         one Canon current-gateway live-human pause; actor/message identity describe the
         Telegram callback or revise follow-up origin.
    post: Canon's durable current-gateway journal/checkpoint/artifact store records the
          operator decision and returns a concise operator-facing status line.
    post: ``sender`` is forwarded as the optional current-gateway review-card delivery seam for
          revise loops; phase execution still uses the gateway-owned private backend client.
    raises: ValueError/RuntimeError when Canon integration is unavailable or the run cannot
            accept this decision.
    """
    normalized_gate_id = str(gate_id or "").strip()
    normalized_action_id = str(action_id or "").strip().lower()
    if normalized_gate_id or normalized_action_id:
        if not normalized_gate_id:
            raise ValueError("gate_id is required for declared Canon review callbacks")
        if not normalized_action_id:
            raise ValueError("action_id is required for declared Canon review callbacks")
    decision = _normalize_decision(choice=choice, action_id=normalized_action_id)
    normalized_run_id = _derive_run_id(run_id=run_id, gate_id=normalized_gate_id)
    if not str(actor_id or "").strip():
        raise ValueError("actor_id is required")
    if not str(message_id or "").strip():
        raise ValueError("message_id is required")

    from integrations.hermes.canon_hermes.current_gateway_runner import (
        build_current_gateway_runner_config,
        record_current_gateway_human_response,
    )
    from tools.canon_workflow_command import create_gateway_phase_backend_client

    phase_backend_client = create_gateway_phase_backend_client()
    config = build_current_gateway_runner_config(phase_backend_client=phase_backend_client)
    phase_backend_client.bind_artifacts_dir(getattr(config, "artifacts_dir", None))

    result: dict[str, Any] = record_current_gateway_human_response(
        run_id=normalized_run_id,
        decision=decision,
        actor={"actorId": str(actor_id), "actorName": str(actor_name or "")},
        origin={
            "kind": "telegram",
            "chatId": str(chat_id or ""),
            "threadId": str(thread_id or "") if thread_id else None,
            "messageId": str(message_id),
            "gateId": normalized_gate_id or None,
            "actionId": normalized_action_id or None,
            "revisionInstructions": str(revision_instructions or "").strip() if decision == "corrections_requested" else "",
        },
        config=config,
        sender=sender,
    )
    status = result.get("status", decision)
    artifact_ref = result.get("artifactRef")
    closeout = result.get("operatorCloseout")
    if isinstance(closeout, dict) and str(status).strip().lower() == "completed":
        lines = [
            f"Canon review recorded: `{status}` for `{normalized_run_id}`.",
        ]
        summary = closeout.get("summary")
        if isinstance(summary, str) and summary.strip():
            lines.append(f"Кратко: {summary.strip()}")
        spec_package_dir = closeout.get("specPackageDirectory")
        if isinstance(spec_package_dir, str) and spec_package_dir.strip():
            lines.append(f"Пакет спеков: `{spec_package_dir.strip()}`")
        implementation_plan_file = closeout.get("implementationPlanFile")
        if isinstance(implementation_plan_file, str) and implementation_plan_file.strip():
            lines.append(f"План: `{implementation_plan_file.strip()}`")
        return "\n".join(lines)

    suffix = f"\nАртефакт: `{artifact_ref}`" if artifact_ref else ""
    return f"Canon review recorded: `{status}` for `{normalized_run_id}`.{suffix}"
