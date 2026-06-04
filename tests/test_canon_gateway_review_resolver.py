"""Regression tests for Canon current-gateway Telegram review resolver."""

from __future__ import annotations

import os
import sys
import types

import pytest

from tools.canon_gateway_review import (
    enter_gateway_review_context,
    exit_gateway_review_context,
    resolve_telegram_canon_review,
    resolve_telegram_canon_review_outcome,
)


def test_resolver_formats_completed_closeout_with_spec_package_dir_and_plan_file(monkeypatch):
    """Completed callbacks must render operator-facing files, not internal artifact bureaucracy.

    pre: recorder returns status=completed with operatorCloseout payload from current-gateway.
    post: resolver response includes run/status/summary plus spec-package directory and plan file only.
    raises: AssertionError while resolver still dumps internal payload refs into Telegram.
    """

    calls = []

    def build_current_gateway_runner_config(**kwargs):
        del kwargs
        return {"config": "ok"}

    def record_current_gateway_human_response(**kwargs):
        calls.append(kwargs)
        return {
            "status": "completed",
            "artifactRef": "current-gateway/run-1/human-response",
            "operatorCloseout": {
                "runId": "run-1",
                "status": "completed",
                "summary": "Frozen package summary.",
                "specPackageDirectory": "/home/hermes/.hermes/canon-current-gateway/artifacts/current-gateway/run-1/spec-package",
                "implementationPlanFile": "/home/hermes/.hermes/canon-current-gateway/artifacts/current-gateway/run-1/implementation-plan.md",
            },
        }

    module = types.ModuleType("integrations.hermes.canon_hermes.current_gateway_runner")
    module.build_current_gateway_runner_config = build_current_gateway_runner_config
    module.record_current_gateway_human_response = record_current_gateway_human_response
    module.resolve_current_gateway_review_callback_token = lambda **kwargs: (_ for _ in ()).throw(AssertionError(kwargs))
    monkeypatch.setitem(sys.modules, "integrations", types.ModuleType("integrations"))
    monkeypatch.setitem(sys.modules, "integrations.hermes", types.ModuleType("integrations.hermes"))
    monkeypatch.setitem(sys.modules, "integrations.hermes.canon_hermes", types.ModuleType("integrations.hermes.canon_hermes"))
    monkeypatch.setitem(sys.modules, "integrations.hermes.canon_hermes.current_gateway_runner", module)

    result = resolve_telegram_canon_review(
        run_id="run-1",
        choice="y",
        actor_id="333",
        actor_name="Operator",
        chat_id="-100123",
        thread_id="14312",
        message_id="31614",
    )

    assert "run-1" in result
    assert "completed" in result
    assert "Frozen package summary." in result
    assert "Пакет спеков:" in result
    assert "План:" in result
    assert "/home/hermes/.hermes/canon-current-gateway/artifacts/current-gateway/run-1/spec-package" in result
    assert "/home/hermes/.hermes/canon-current-gateway/artifacts/current-gateway/run-1/implementation-plan.md" in result
    assert "artifacts.solution-modeling" not in result
    assert "Ответ оператора" not in result
    assert calls


def test_resolver_outcome_marks_nonterminal_status_for_fresh_chat_delivery(monkeypatch):
    """Structured resolver outcome must carry explicit notify_chat authority for paused-again states.

    pre: recorder returns a nonterminal workflow status after a review decision is recorded.
    post: resolver outcome exposes ``notify_chat=True`` from structured status, independent of text.
    raises: AssertionError while Telegram would have to inspect human-readable text to decide delivery.
    """

    def build_current_gateway_runner_config(**kwargs):
        del kwargs
        return {"config": "ok"}

    def record_current_gateway_human_response(**kwargs):
        del kwargs
        return {
            "status": "awaiting-human-review",
            "artifactRef": "current-gateway/run-1/human-response",
        }

    module = types.ModuleType("integrations.hermes.canon_hermes.current_gateway_runner")
    module.build_current_gateway_runner_config = build_current_gateway_runner_config
    module.record_current_gateway_human_response = record_current_gateway_human_response
    module.resolve_current_gateway_review_callback_token = lambda **kwargs: (_ for _ in ()).throw(AssertionError(kwargs))
    monkeypatch.setitem(sys.modules, "integrations", types.ModuleType("integrations"))
    monkeypatch.setitem(sys.modules, "integrations.hermes", types.ModuleType("integrations.hermes"))
    monkeypatch.setitem(sys.modules, "integrations.hermes.canon_hermes", types.ModuleType("integrations.hermes.canon_hermes"))
    monkeypatch.setitem(sys.modules, "integrations.hermes.canon_hermes.current_gateway_runner", module)

    outcome = resolve_telegram_canon_review_outcome(
        run_id="run-1",
        choice="y",
        actor_id="333",
        actor_name="Operator",
        chat_id="-100123",
        thread_id="14312",
        message_id="31614",
    )

    assert outcome["status"] == "awaiting-human-review"
    assert outcome["delivery_mode"] == "fresh_status"
    assert outcome["notify_chat"] is True
    assert outcome["text"] == "Canon review recorded: `awaiting-human-review` for `run-1`.\nАртефакт: `current-gateway/run-1/human-response`"


def test_resolver_outcome_suppresses_generic_followup_when_sender_delivers_next_review_card(monkeypatch):
    """Revise-loop paused states must expose explicit review-card-only delivery authority.

    pre: recorder returns a paused-again status while a sender seam was supplied for revise-loop
         review-card delivery.
    post: resolver outcome marks the result as ``review_card_only`` and suppresses generic fresh
          chat text so Telegram transport does not infer behavior from the rendered status string.
    raises: AssertionError while paused revise loops still require Telegram text heuristics.
    """

    def build_current_gateway_runner_config(**kwargs):
        del kwargs
        return {"config": "ok"}

    def record_current_gateway_human_response(**kwargs):
        del kwargs
        return {
            "status": "awaiting-human-review",
            "artifactRef": "current-gateway/run-1/human-response",
        }

    module = types.ModuleType("integrations.hermes.canon_hermes.current_gateway_runner")
    module.build_current_gateway_runner_config = build_current_gateway_runner_config
    module.record_current_gateway_human_response = record_current_gateway_human_response
    module.resolve_current_gateway_review_callback_token = lambda **kwargs: (_ for _ in ()).throw(AssertionError(kwargs))
    monkeypatch.setitem(sys.modules, "integrations", types.ModuleType("integrations"))
    monkeypatch.setitem(sys.modules, "integrations.hermes", types.ModuleType("integrations.hermes"))
    monkeypatch.setitem(sys.modules, "integrations.hermes.canon_hermes", types.ModuleType("integrations.hermes.canon_hermes"))
    monkeypatch.setitem(sys.modules, "integrations.hermes.canon_hermes.current_gateway_runner", module)

    outcome = resolve_telegram_canon_review_outcome(
        run_id="run-1",
        choice="e",
        actor_id="333",
        actor_name="Operator",
        chat_id="-100123",
        thread_id="14312",
        message_id="31614",
        revision_instructions="Уточнить acceptance criteria.",
        sender=object(),
    )

    assert outcome["status"] == "awaiting-human-review"
    assert outcome["delivery_mode"] == "review_card_only"
    assert outcome["notify_chat"] is False


def test_resolver_passes_sender_to_current_gateway_recorder(monkeypatch):
    """Telegram resolver must forward sender so revise loops can deliver the next review card.

    pre: Telegram callback resolver handles a compact `cg:y:<run>` button click with a sender seam.
    post: it calls record_current_gateway_human_response with the sender kwarg, while phase backend
          binding still comes from build_current_gateway_runner_config.
    raises: AssertionError while live revise loops cannot deliver a newly paused review card.
    """

    calls = []
    config_kwargs = []

    def build_current_gateway_runner_config(**kwargs):
        config_kwargs.append(kwargs)
        return {"config": "ok"}

    def record_current_gateway_human_response(**kwargs):
        calls.append(kwargs)
        assert kwargs.get("sender") is sender
        return {"status": "completed", "artifactRef": "current-gateway/run-1/human-response"}

    module = types.ModuleType("integrations.hermes.canon_hermes.current_gateway_runner")
    module.build_current_gateway_runner_config = build_current_gateway_runner_config
    module.record_current_gateway_human_response = record_current_gateway_human_response
    module.resolve_current_gateway_review_callback_token = lambda **kwargs: (_ for _ in ()).throw(AssertionError(kwargs))
    monkeypatch.setitem(sys.modules, "integrations", types.ModuleType("integrations"))
    monkeypatch.setitem(sys.modules, "integrations.hermes", types.ModuleType("integrations.hermes"))
    monkeypatch.setitem(sys.modules, "integrations.hermes.canon_hermes", types.ModuleType("integrations.hermes.canon_hermes"))
    monkeypatch.setitem(sys.modules, "integrations.hermes.canon_hermes.current_gateway_runner", module)

    sender = object()
    result = resolve_telegram_canon_review(
        run_id="run-1",
        choice="y",
        actor_id="333",
        actor_name="Operator",
        chat_id="-100123",
        thread_id="14312",
        message_id="31614",
        sender=sender,
    )

    assert "completed" in result
    assert calls == [
        {
            "run_id": "run-1",
            "decision": "approved",
            "actor": {"actorId": "333", "actorName": "Operator"},
            "origin": {
                "kind": "telegram",
                "chatId": "-100123",
                "threadId": "14312",
                "messageId": "31614",
                "gateId": None,
                "actionId": None,
                "revisionInstructions": "",
            },
            "config": {"config": "ok"},
            "sender": sender,
        }
    ]
    assert len(config_kwargs) == 1
    phase_backend_client = config_kwargs[0].get("phase_backend_client")
    assert phase_backend_client is not None
    assert callable(getattr(phase_backend_client, "start_scoped_session", None))


def test_resolver_expands_callback_token_before_recording_declared_response(monkeypatch):
    """Opaque Telegram callback tokens must resolve to full gate identity before mutation.

    pre: callback_data carried only a durable callback token plus action id.
    post: resolver loads full run/gate authority from Canon, then records the decision with the
          existing gate-bound origin verification payload.
    raises: AssertionError while token callbacks bypass durable authority lookup.
    """

    calls = []
    token_calls = []

    def build_current_gateway_runner_config(**kwargs):
        del kwargs
        return {"config": "ok"}

    def resolve_current_gateway_review_callback_token(**kwargs):
        token_calls.append(kwargs)
        return {
            "runId": "sm-writing-plans-schema-authority-20260603",
            "threadId": "telegram:-1003351905082:21676",
            "gateId": "sm-writing-plans-schema-authority-20260603:telegram:-1003351905082:21676:review",
            "nodeId": "review",
        }

    def record_current_gateway_human_response(**kwargs):
        calls.append(kwargs)
        return {"status": "resolved", "artifactRef": "current-gateway/sm-writing-plans-schema-authority-20260603/human-response"}

    module = types.ModuleType("integrations.hermes.canon_hermes.current_gateway_runner")
    module.build_current_gateway_runner_config = build_current_gateway_runner_config
    module.record_current_gateway_human_response = record_current_gateway_human_response
    module.resolve_current_gateway_review_callback_token = resolve_current_gateway_review_callback_token
    monkeypatch.setitem(sys.modules, "integrations", types.ModuleType("integrations"))
    monkeypatch.setitem(sys.modules, "integrations.hermes", types.ModuleType("integrations.hermes"))
    monkeypatch.setitem(sys.modules, "integrations.hermes.canon_hermes", types.ModuleType("integrations.hermes.canon_hermes"))
    monkeypatch.setitem(sys.modules, "integrations.hermes.canon_hermes.current_gateway_runner", module)

    result = resolve_telegram_canon_review(
        callback_token="cgcb_abc123",
        action_id="approve",
        actor_id="333",
        actor_name="Operator",
        chat_id="-1003351905082",
        thread_id="21676",
        message_id="457",
    )

    assert "resolved" in result
    assert token_calls == [{"callback_token": "cgcb_abc123", "config": {"config": "ok"}}]
    assert calls == [
        {
            "run_id": "sm-writing-plans-schema-authority-20260603",
            "decision": "approved",
            "actor": {"actorId": "333", "actorName": "Operator"},
            "origin": {
                "kind": "telegram",
                "chatId": "-1003351905082",
                "threadId": "21676",
                "messageId": "457",
                "gateId": "sm-writing-plans-schema-authority-20260603:telegram:-1003351905082:21676:review",
                "actionId": "approve",
                "revisionInstructions": "",
            },
            "config": {"config": "ok"},
            "sender": None,
        }
    ]


def test_resolver_backfills_thread_identity_from_callback_token_for_dm_no_topic(monkeypatch):
    """DM token callbacks must recover Canon thread authority even when Telegram exposes no topic id.

    pre: callback_data carried only callback_token/action_id and the Telegram DM callback has no
         message_thread_id to pass into the resolver.
    post: resolver records origin.threadId from durable callback-token authority so Canon review
          verification can resume the paused run.
    raises: AssertionError while DM callbacks still reach Canon with threadId=None.
    """

    calls = []
    token_calls = []

    def build_current_gateway_runner_config(**kwargs):
        del kwargs
        return {"config": "ok"}

    def resolve_current_gateway_review_callback_token(**kwargs):
        token_calls.append(kwargs)
        return {
            "runId": "sm-dm-no-topic-20260604",
            "threadId": "telegram:5558998798",
            "gateId": "sm-dm-no-topic-20260604:telegram:5558998798:review",
            "nodeId": "review",
        }

    def record_current_gateway_human_response(**kwargs):
        calls.append(kwargs)
        return {"status": "resolved", "artifactRef": "current-gateway/sm-dm-no-topic-20260604/human-response"}

    module = types.ModuleType("integrations.hermes.canon_hermes.current_gateway_runner")
    module.build_current_gateway_runner_config = build_current_gateway_runner_config
    module.record_current_gateway_human_response = record_current_gateway_human_response
    module.resolve_current_gateway_review_callback_token = resolve_current_gateway_review_callback_token
    monkeypatch.setitem(sys.modules, "integrations", types.ModuleType("integrations"))
    monkeypatch.setitem(sys.modules, "integrations.hermes", types.ModuleType("integrations.hermes"))
    monkeypatch.setitem(sys.modules, "integrations.hermes.canon_hermes", types.ModuleType("integrations.hermes.canon_hermes"))
    monkeypatch.setitem(sys.modules, "integrations.hermes.canon_hermes.current_gateway_runner", module)

    result = resolve_telegram_canon_review(
        callback_token="cgcb_dm123",
        action_id="approve",
        actor_id="5558998798",
        actor_name="Breanainn",
        chat_id="5558998798",
        thread_id=None,
        message_id="8805",
    )

    assert "resolved" in result
    assert token_calls == [{"callback_token": "cgcb_dm123", "config": {"config": "ok"}}]
    assert calls == [
        {
            "run_id": "sm-dm-no-topic-20260604",
            "decision": "approved",
            "actor": {"actorId": "5558998798", "actorName": "Breanainn"},
            "origin": {
                "kind": "telegram",
                "chatId": "5558998798",
                "threadId": "telegram:5558998798",
                "messageId": "8805",
                "gateId": "sm-dm-no-topic-20260604:telegram:5558998798:review",
                "actionId": "approve",
                "revisionInstructions": "",
            },
            "config": {"config": "ok"},
            "sender": None,
        }
    ]


def test_resolver_builds_current_gateway_config_with_profile_schema_resolver(monkeypatch):
    """Telegram review resolver must pass explicit external schema authority into current-gateway resume.

    pre: solution-modeling/current-gateway resume needs workflow.externalSchemaResources resolution and
         the review tool is about to build its Canon runner config from Hermes code.
    post: build_current_gateway_runner_config receives a non-null explicit resolver instead of falling
          back to a bare config that blocks resume with missing external schema authority.
    raises: AssertionError while review callbacks still construct resolver-less runtime configs.
    """

    build_calls = []

    def build_current_gateway_runner_config(**kwargs):
        build_calls.append(kwargs)
        return {"config": "ok"}

    def build_current_gateway_profile_schema_resource_resolver():
        return {"resolver": "profile-schema"}

    def record_current_gateway_human_response(**kwargs):
        return {"status": "resolved", "artifactRef": "current-gateway/run/human-response"}

    module = types.ModuleType("integrations.hermes.canon_hermes.current_gateway_runner")
    module.build_current_gateway_runner_config = build_current_gateway_runner_config
    module.build_current_gateway_profile_schema_resource_resolver = build_current_gateway_profile_schema_resource_resolver
    module.record_current_gateway_human_response = record_current_gateway_human_response
    module.resolve_current_gateway_review_callback_token = lambda **kwargs: {
        "runId": "run-with-resolver",
        "threadId": "telegram:5558998798",
        "gateId": "run-with-resolver:telegram:5558998798:review",
        "nodeId": "review",
    }
    monkeypatch.setitem(sys.modules, "integrations", types.ModuleType("integrations"))
    monkeypatch.setitem(sys.modules, "integrations.hermes", types.ModuleType("integrations.hermes"))
    monkeypatch.setitem(sys.modules, "integrations.hermes.canon_hermes", types.ModuleType("integrations.hermes.canon_hermes"))
    monkeypatch.setitem(sys.modules, "integrations.hermes.canon_hermes.current_gateway_runner", module)
    monkeypatch.setattr(
        "tools.canon_workflow_command.create_gateway_phase_backend_client",
        lambda: object(),
    )

    result = resolve_telegram_canon_review(
        callback_token="cgcb_cfg123",
        action_id="approve",
        actor_id="5558998798",
        actor_name="Breanainn",
        chat_id="5558998798",
        thread_id=None,
        message_id="8806",
    )

    assert "resolved" in result
    assert build_calls == [
        {
            "phase_backend_client": build_calls[0]["phase_backend_client"],
            "external_schema_resource_resolver": {"resolver": "profile-schema"},
        }
    ]


def test_resolver_rejects_non_gateway_process_invocation(monkeypatch):
    """Telegram review resolver must not be callable from arbitrary non-gateway Python processes.

    pre: caller is outside the live gateway process and pytest bypass is explicitly disabled.
    post: resolver fails closed before any Canon mutation/import side effects.
    raises: AssertionError while local scripts can still spoof human-review completion.
    """

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sys, "argv", ["python", "-c", "pass"])

    with pytest.raises(RuntimeError, match="gateway-only"):
        resolve_telegram_canon_review(
            run_id="run-1",
            choice="y",
            actor_id="333",
            actor_name="Operator",
            chat_id="-100123",
            thread_id="14312",
            message_id="31614",
        )


def test_resolver_allows_explicit_gateway_review_context_without_pytest_env(monkeypatch):
    """TelegramAdapter-owned review context must pass while arbitrary local scripts stay blocked.

    pre: pytest bypass is disabled and caller explicitly enters gateway review context.
    post: resolver records via current-gateway seam instead of failing the gateway-only guard.
    raises: AssertionError while real Telegram callbacks are blocked by the anti-self-approval guard.
    """

    calls = []

    def build_current_gateway_runner_config(**kwargs):
        return {"config": "ok", "kwargs": kwargs}

    def record_current_gateway_human_response(**kwargs):
        calls.append(kwargs)
        return {"status": "resolved", "artifactRef": "current-gateway/run-1/human-response"}

    module = types.ModuleType("integrations.hermes.canon_hermes.current_gateway_runner")
    module.build_current_gateway_runner_config = build_current_gateway_runner_config
    module.record_current_gateway_human_response = record_current_gateway_human_response
    module.resolve_current_gateway_review_callback_token = lambda **kwargs: (_ for _ in ()).throw(AssertionError(kwargs))
    monkeypatch.setitem(sys.modules, "integrations", types.ModuleType("integrations"))
    monkeypatch.setitem(sys.modules, "integrations.hermes", types.ModuleType("integrations.hermes"))
    monkeypatch.setitem(sys.modules, "integrations.hermes.canon_hermes", types.ModuleType("integrations.hermes.canon_hermes"))
    monkeypatch.setitem(sys.modules, "integrations.hermes.canon_hermes.current_gateway_runner", module)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    token = enter_gateway_review_context()
    try:
        result = resolve_telegram_canon_review(
            run_id="run-1",
            choice="e",
            actor_id="333",
            actor_name="Operator",
            chat_id="-100123",
            thread_id="14312",
            message_id="31614",
            revision_instructions="fix scope",
        )
    finally:
        exit_gateway_review_context(token)

    assert "resolved" in result
    assert calls[0]["decision"] == "corrections_requested"
    assert calls[0]["origin"]["revisionInstructions"] == "fix scope"
