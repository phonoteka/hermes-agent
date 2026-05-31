"""Regression tests for Canon current-gateway Telegram review resolver."""

from __future__ import annotations

import sys
import types

from tools.canon_gateway_review import resolve_telegram_canon_review


def test_resolver_formats_completed_closeout_with_summary_plan_and_artifacts(monkeypatch):
    """Completed callbacks must render Canon closeout content, not generic status-only text.

    pre: recorder returns status=completed with operatorCloseout payload from current-gateway.
    post: resolver response includes run/status plus concise summary, plan excerpt, and artifact index.
    raises: AssertionError while resolver still emits only `Canon review recorded: completed` text.
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
                "implementationPlan": {
                    "objective": "Ship exact /ping handler.",
                    "steps": ["Locate boundary", "Add handler"],
                },
                "artifactIndex": [
                    {
                        "label": "Implementation plan",
                        "ref": "artifacts.solution-modeling.implementation-plan",
                        "path": "/home/hermes/.hermes/canon-current-gateway/artifacts/artifacts.solution-modeling.implementation-plan/payload.json",
                    }
                ],
            },
        }

    module = types.ModuleType("integrations.hermes.canon_hermes.current_gateway_runner")
    module.build_current_gateway_runner_config = build_current_gateway_runner_config
    module.record_current_gateway_human_response = record_current_gateway_human_response
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
    assert "Ship exact /ping handler." in result
    assert "artifacts.solution-modeling.implementation-plan" in result
    assert "/home/hermes/.hermes/canon-current-gateway/artifacts/" in result
    assert calls


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
