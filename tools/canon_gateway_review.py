"""Gateway-side Canon current-gateway review mechanics.

This module is deliberately non-agentic: Telegram button callbacks call it directly
from the gateway adapter, and it records the operator decision in Canon's durable
current-gateway store without creating a Hermes agent turn.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
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

_INLINE_ONLY_OPERATOR_STATUSES = {
    "approved",
    "rejected",
    "corrections_requested",
    "resolved",
    "recorded",
}

_GATEWAY_REVIEW_CONTEXT: ContextVar[bool] = ContextVar("telegram_canon_review_gateway_context", default=False)


def enter_gateway_review_context() -> object:
    """Mark this execution context as the live Telegram gateway review seam.

    pre: caller is inside TelegramAdapter while handling a real callback or revise follow-up.
    post: returns a token that must be passed to ``exit_gateway_review_context``.
    raises: none.
    """

    return _GATEWAY_REVIEW_CONTEXT.set(True)


def exit_gateway_review_context(token: object) -> None:
    """Reset the live Telegram gateway review seam marker.

    pre: token was returned by ``enter_gateway_review_context`` in the same logical context.
    post: gateway review seam marker is restored to its previous value.
    raises: ValueError from ContextVar when token belongs to another context.
    """

    _GATEWAY_REVIEW_CONTEXT.reset(token)


def _is_gateway_process() -> bool:
    """Return whether the current context is allowed to mutate Telegram review state.

    pre: none.
    post: returns True only for the TelegramAdapter-owned review context or pytest unit tests.
    raises: none.
    """

    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    return bool(_GATEWAY_REVIEW_CONTEXT.get())


def _require_gateway_process_context() -> None:
    """Fail closed when Telegram review resolution is attempted outside gateway runtime.

    pre: caller is about to mutate Canon durable human-review state.
    post: returns only for TelegramAdapter-owned review execution or pytest harness.
    raises: RuntimeError when a non-gateway context attempts to resolve a review.
    """

    if not _is_gateway_process():
        raise RuntimeError("Telegram Canon review resolution is gateway-only")


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


def _format_operator_result_text(*, status: str, run_id: str, artifact_ref: Any, closeout: Any) -> str:
    """Render one operator-facing Telegram status line bundle from structured Canon result.

    pre: ``status`` and ``run_id`` identify one recorded review decision outcome.
    post: returns the exact human-facing text that Telegram can show inline or as a fresh message.
    raises: none.
    """

    normalized_status = str(status or "").strip().lower()
    if isinstance(closeout, dict) and normalized_status == "completed":
        lines = [
            f"Canon review recorded: `{status}` for `{run_id}`.",
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
    return f"Canon review recorded: `{status}` for `{run_id}`.{suffix}"


def _should_send_operator_followup(*, status: str, decision: str) -> bool:
    """Return whether Telegram should emit a fresh chat message for this structured outcome.

    pre: ``status`` comes from Canon's structured recorder result; ``decision`` is the normalized
         durable human response that triggered the resolver.
    post: returns True for workflow-status updates that should appear as a new chat message,
          and False for inline-only acknowledgement states.
    raises: none.
    """

    normalized_status = str(status or "").strip().lower()
    normalized_decision = str(decision or "").strip().lower()
    if not normalized_status:
        return False
    if normalized_status == normalized_decision:
        return False
    return normalized_status not in _INLINE_ONLY_OPERATOR_STATUSES


def _resolve_operator_delivery(*, status: str, decision: str, sender: Any | None) -> dict[str, Any]:
    """Project one explicit Telegram delivery mode from structured review outcome state.

    pre: ``status`` comes from Canon's structured recorder result; ``decision`` is the normalized
         durable human response that triggered the resolver.
    post: returns a ``delivery_mode`` authority plus ``notify_chat`` so Telegram transport can
          route inline-only acknowledgements, fresh closeouts, fresh status messages, and
          sender-delivered review-card loops without inspecting human-readable text.
    raises: none.
    """

    normalized_status = str(status or "").strip().lower()
    if not normalized_status:
        return {"delivery_mode": "inline_only", "notify_chat": False}
    if normalized_status == "completed":
        return {"delivery_mode": "fresh_closeout", "notify_chat": True}
    if normalized_status == "awaiting-human-review" and sender is not None:
        return {"delivery_mode": "review_card_only", "notify_chat": False}
    return {
        "delivery_mode": "fresh_status" if _should_send_operator_followup(status=status, decision=decision) else "inline_only",
        "notify_chat": _should_send_operator_followup(status=status, decision=decision),
    }


def resolve_telegram_canon_review_outcome(
    *,
    run_id: str = "",
    choice: str = "",
    gate_id: str = "",
    callback_token: str = "",
    action_id: str = "",
    actor_id: str,
    actor_name: str = "",
    chat_id: str = "",
    thread_id: str | None = None,
    message_id: str = "",
    revision_instructions: str = "",
    sender: Any | None = None,
) -> dict[str, Any]:
    """Persist one Telegram review decision and return structured operator delivery instructions.

    pre: either legacy ``run_id``/``choice``, declared ``gate_id``/``action_id``, or opaque
         ``callback_token``/``action_id`` identifies one Canon current-gateway live-human pause;
         actor/message identity describe the Telegram callback or revise follow-up origin.
    post: returns structured operator-facing outcome with ``text`` plus explicit ``notify_chat``
          delivery authority so Telegram transport does not infer workflow meaning from text shape.
    raises: ValueError/RuntimeError when Canon integration is unavailable or the run cannot
            accept this decision.
    """

    _require_gateway_process_context()
    normalized_gate_id = str(gate_id or "").strip()
    normalized_callback_token = str(callback_token or "").strip()
    normalized_action_id = str(action_id or "").strip().lower()
    if normalized_gate_id or normalized_callback_token or normalized_action_id:
        if not normalized_gate_id and not normalized_callback_token:
            raise ValueError("gate_id or callback_token is required for declared Canon review callbacks")
        if not normalized_action_id:
            raise ValueError("action_id is required for declared Canon review callbacks")
    decision = _normalize_decision(choice=choice, action_id=normalized_action_id)
    if not str(actor_id or "").strip():
        raise ValueError("actor_id is required")
    if not str(message_id or "").strip():
        raise ValueError("message_id is required")

    from integrations.hermes.canon_hermes import current_gateway_runner as cg_runner
    from tools.canon_workflow_command import create_gateway_phase_backend_client

    phase_backend_client = create_gateway_phase_backend_client()
    resolver_factory = getattr(cg_runner, "build_current_gateway_profile_schema_resource_resolver", None)
    external_schema_resource_resolver = resolver_factory() if callable(resolver_factory) else None
    config = cg_runner.build_current_gateway_runner_config(
        phase_backend_client=phase_backend_client,
        external_schema_resource_resolver=external_schema_resource_resolver,
    )
    bind_artifacts_dir = getattr(phase_backend_client, "bind_artifacts_dir", None)
    if callable(bind_artifacts_dir):
        bind_artifacts_dir(getattr(config, "artifacts_dir", None))
    if normalized_callback_token:
        callback_authority = cg_runner.resolve_current_gateway_review_callback_token(
            callback_token=normalized_callback_token,
            config=config,
        )
        token_run_id = callback_authority["runId"]
        token_gate_id = callback_authority["gateId"]
        token_thread_id = str(callback_authority.get("threadId") or "").strip()
        if normalized_gate_id and normalized_gate_id != token_gate_id:
            raise ValueError("gate_id does not match callback_token")
        if str(run_id or "").strip() and str(run_id or "").strip() != token_run_id:
            raise ValueError("run_id does not match callback_token")
        normalized_gate_id = token_gate_id
        normalized_run_id = token_run_id
    else:
        token_thread_id = ""
        normalized_run_id = _derive_run_id(run_id=run_id, gate_id=normalized_gate_id)

    resolved_thread_id = str(thread_id or "").strip() or token_thread_id or None

    result: dict[str, Any] = cg_runner.record_current_gateway_human_response(
        run_id=normalized_run_id,
        decision=decision,
        actor={"actorId": str(actor_id), "actorName": str(actor_name or "")},
        origin={
            "kind": "telegram",
            "chatId": str(chat_id or ""),
            "threadId": resolved_thread_id,
            "messageId": str(message_id),
            "gateId": normalized_gate_id or None,
            "actionId": normalized_action_id or None,
            "revisionInstructions": str(revision_instructions or "").strip() if decision == "corrections_requested" else "",
        },
        config=config,
        sender=sender,
    )
    status = str(result.get("status", decision) or decision)
    artifact_ref = result.get("artifactRef")
    closeout = result.get("operatorCloseout")
    text = _format_operator_result_text(
        status=status,
        run_id=normalized_run_id,
        artifact_ref=artifact_ref,
        closeout=closeout,
    )
    delivery = _resolve_operator_delivery(status=status, decision=decision, sender=sender)
    return {
        "run_id": normalized_run_id,
        "decision": decision,
        "status": status,
        "text": text,
        "notify_chat": delivery["notify_chat"],
        "delivery_mode": delivery["delivery_mode"],
        "artifact_ref": artifact_ref,
        "operator_closeout": closeout,
    }


def resolve_telegram_canon_review(
    *,
    run_id: str = "",
    choice: str = "",
    gate_id: str = "",
    callback_token: str = "",
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

    pre: either legacy ``run_id``/``choice``, declared ``gate_id``/``action_id``, or opaque
         ``callback_token``/``action_id`` identifies one Canon current-gateway live-human pause;
         actor/message identity describe the Telegram callback or revise follow-up origin.
    post: Canon's durable current-gateway journal/checkpoint/artifact store records the
          operator decision and returns a concise operator-facing status line.
    post: ``sender`` is forwarded as the optional current-gateway review-card delivery seam for
          revise loops; phase execution still uses the gateway-owned private backend client.
    raises: ValueError/RuntimeError when Canon integration is unavailable or the run cannot
            accept this decision.
    """

    return resolve_telegram_canon_review_outcome(
        run_id=run_id,
        choice=choice,
        gate_id=gate_id,
        callback_token=callback_token,
        action_id=action_id,
        actor_id=actor_id,
        actor_name=actor_name,
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_id,
        revision_instructions=revision_instructions,
        sender=sender,
    )["text"]
