"""Focused regressions for the live Hermes workflow facade."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_solution_modeling_pack_alias_resolves_in_live_facade():
    """The live facade must resolve the production solution-modeling-pack selector.

    pre: registry-based workflow resolution is unavailable and the caller uses the live
         `solution-modeling-pack` workflow id.
    post: the facade returns the static solution-modeling workflowRef instead of failing closed.
    raises: AssertionError while the live facade still omits the alias present in Canon.
    """

    from integrations.hermes.canon_hermes.workflow_facade import resolve_workflow_ref_for_command

    assert resolve_workflow_ref_for_command(workflow_name="solution-modeling-pack") == (
        "src/canon_workflows/packs/solution_modeling_pack/workflow.json"
    )


def test_autonomous_development_pack_resolves_via_static_mapping_without_registry(monkeypatch):
    """The live facade must resolve the production autonomous-development-pack selector.

    pre: Canon registry importability is unavailable in the active Hermes runtime.
    post: the facade resolves `autonomous-development-pack` through explicit static mapping without
          requiring any public workflowRef field.
    raises: AssertionError while Hermes still blocks live launch on Canon registry availability.
    """

    from integrations.hermes.canon_hermes import workflow_facade

    monkeypatch.setattr(workflow_facade, "get_workflow_pack_path", None)

    assert workflow_facade.resolve_workflow_ref_for_command(
        workflow_name="autonomous-development-pack"
    ) == "src/canon_workflows/packs/autonomous_development_pack/workflow.json"


def test_start_workflow_preserves_private_request_authority_and_threads_private_stores(monkeypatch, tmp_path: Path):
    """Live facade launches must preserve safe request authority and private gateway stores.

    pre: payload carries public workflow/gateway selectors plus a private requestAuthority object,
         and stores carry private runtime delivery/config authorities.
    post: requestAuthority preserves only request-owned runtime/model/context fields, the facade
          keeps gateway-owned selectors authoritative, and private stores are threaded into config
          plus hostConfig.
    raises: AssertionError while the live facade still drops private request/store authorities.
    """

    from integrations.hermes.canon_hermes import workflow_facade

    captured: dict[str, object] = {}
    review_sender = lambda delivery: delivery

    class FakeConfig:
        def __init__(self, root, **kwargs):
            self.root = root
            self.journal_path = Path(root) / "journal.sqlite3"
            self.checkpoint_path = Path(root) / "checkpoints.sqlite3"
            self.artifacts_dir = Path(root) / "artifacts"
            self.phase_backend_client = kwargs.get("phase_backend_client")
            self.tool_host = kwargs.get("tool_host")
            self.phase_profile_map = kwargs.get("phase_profile_map")
            self.external_schema_resource_resolver = kwargs.get("external_schema_resource_resolver")
            self.external_schema_resources = kwargs.get("external_schema_resources")

    class FakePhaseBackendClient:
        def __init__(self):
            self.bound_artifacts_dir = None

        def bind_artifacts_dir(self, artifacts_dir):
            self.bound_artifacts_dir = artifacts_dir

    phase_backend_client = FakePhaseBackendClient()
    tool_host = object()
    phase_profile_map = {"model_solution": "proofloopworker"}
    external_schema_resource_resolver = object()
    external_schema_resources = [{"resource": "solution-modeling-pack"}]

    def fake_build_current_gateway_request(**kwargs):
        return {
            "schemaVersion": "run-request.schema.v2",
            "workflowRef": kwargs["workflow_ref"],
            "runId": kwargs["run_id"],
            "inputs": kwargs["inputs"],
            "workflowVersion": kwargs["workflow_version"],
            "threadId": "gateway-thread",
            "hostIntegration": {"kind": "gateway-owned"},
            "runtimeContext": {
                "gateway": kwargs["gateway_source"],
                "hermes": {"phaseProfiles": phase_profile_map},
            },
        }

    def fake_run_current_gateway_request(request, *, config, host_mode, host_config):
        captured["request"] = request
        captured["config"] = config
        captured["host_mode"] = host_mode
        captured["host_config"] = host_config
        return {"status": "ok"}

    monkeypatch.setattr(workflow_facade, "CurrentGatewayRunnerConfig", FakeConfig)
    monkeypatch.setattr(workflow_facade, "build_current_gateway_request", fake_build_current_gateway_request)
    monkeypatch.setattr(workflow_facade, "run_current_gateway_request", fake_run_current_gateway_request)

    payload = {
        "workflowId": "solution-modeling-pack",
        "runId": "run-live-1",
        "inputs": {"brief": {"task": "Freeze package."}},
        "gatewaySource": {"platform": "telegram", "chat_id": "-100123456", "thread_id": "777"},
        "hostMode": "live",
        "hostConfig": {"public": True},
        "requestAuthority": {
            "schemaVersion": "run-request.schema.v2",
            "runId": "run-live-1",
            "inputs": {"brief": {"task": "Freeze package."}},
            "workflowVersion": "1.2.3",
            "runtime": {"mode": "durable-production", "recursionLimit": 8},
            "modelRouting": {"provider": "openai", "model": "gpt-5.4"},
            "configProfileId": "proofloopworker",
            "overlayIds": ["tenant-default"],
            "runtimeContext": {
                "hermes": {"phaseProfiles": phase_profile_map},
                "trace": {"request": "preserved"},
            },
            "workflowRef": "workflow://stale-request-owned-ref",
            "threadId": "stale-thread",
            "hostIntegration": {"kind": "stale-request-owned"},
        },
    }
    stores = {
        "root": str(tmp_path / "gateway-root"),
        "phase_backend_client": phase_backend_client,
        "tool_host": tool_host,
        "phase_profile_map": phase_profile_map,
        "external_schema_resource_resolver": external_schema_resource_resolver,
        "external_schema_resources": external_schema_resources,
        "review_sender": review_sender,
    }

    assert workflow_facade.start_workflow(payload, stores) == {"status": "ok"}

    request = captured["request"]
    assert request["workflowRef"] == "src/canon_workflows/packs/solution_modeling_pack/workflow.json"
    assert request["runId"] == "run-live-1"
    assert request["inputs"] == {"brief": {"task": "Freeze package."}}
    assert request["workflowVersion"] == "1.2.3"
    assert request["runtime"] == {"mode": "durable-production", "recursionLimit": 8}
    assert request["modelRouting"] == {"provider": "openai", "model": "gpt-5.4"}
    assert request["configProfileId"] == "proofloopworker"
    assert request["overlayIds"] == ["tenant-default"]
    assert request["threadId"] == "gateway-thread"
    assert request["hostIntegration"] == {"kind": "gateway-owned"}
    assert request["runtimeContext"] == {
        "gateway": {"platform": "telegram", "chat_id": "-100123456", "thread_id": "777"},
        "hermes": {"phaseProfiles": phase_profile_map},
        "trace": {"request": "preserved"},
    }

    config = captured["config"]
    assert config.phase_backend_client is phase_backend_client
    assert config.tool_host is tool_host
    assert config.phase_profile_map == phase_profile_map
    assert config.external_schema_resource_resolver is external_schema_resource_resolver
    assert config.external_schema_resources == external_schema_resources
    assert phase_backend_client.bound_artifacts_dir == config.artifacts_dir

    assert captured["host_mode"] == "live"
    assert captured["host_config"] == {"public": True, "review_sender": review_sender}


def test_start_workflow_builds_canon_owned_schema_resolver_when_stores_omit_it(monkeypatch, tmp_path: Path):
    """The live facade must consume Canon's explicit resolver seam by default.

    pre: stores provide gateway-owned runtime authorities but omit explicit schema-resource resolver.
    post: _build_gateway_config uses build_current_gateway_profile_schema_resource_resolver() and keeps
          explicit external_schema_resources/review_sender threading intact.
    raises: AssertionError while the facade still leaves resolver authority unset.
    """

    from integrations.hermes.canon_hermes import workflow_facade

    captured: dict[str, object] = {}
    review_sender = lambda delivery: delivery
    resolver_sentinel = object()

    class FakeConfig:
        def __init__(self, root, **kwargs):
            self.root = root
            self.journal_path = Path(root) / "journal.sqlite3"
            self.checkpoint_path = Path(root) / "checkpoints.sqlite3"
            self.artifacts_dir = Path(root) / "artifacts"
            self.phase_backend_client = kwargs.get("phase_backend_client")
            self.tool_host = kwargs.get("tool_host")
            self.phase_profile_map = kwargs.get("phase_profile_map")
            self.external_schema_resource_resolver = kwargs.get("external_schema_resource_resolver")
            self.external_schema_resources = kwargs.get("external_schema_resources")

    class FakePhaseBackendClient:
        def __init__(self):
            self.bound_artifacts_dir = None

        def bind_artifacts_dir(self, artifacts_dir):
            self.bound_artifacts_dir = artifacts_dir

    def fake_build_current_gateway_request(**kwargs):
        return {
            "schemaVersion": "run-request.schema.v2",
            "workflowRef": kwargs["workflow_ref"],
            "runId": kwargs["run_id"],
            "inputs": kwargs["inputs"],
            "workflowVersion": kwargs["workflow_version"],
            "threadId": "gateway-thread",
            "hostIntegration": {"kind": "gateway-owned"},
            "runtimeContext": {"gateway": kwargs["gateway_source"]},
        }

    def fake_run_current_gateway_request(request, *, config, host_mode, host_config):
        captured["config"] = config
        captured["host_mode"] = host_mode
        captured["host_config"] = host_config
        return {"status": "ok"}

    monkeypatch.setattr(workflow_facade, "CurrentGatewayRunnerConfig", FakeConfig)
    monkeypatch.setattr(workflow_facade, "build_current_gateway_request", fake_build_current_gateway_request)
    monkeypatch.setattr(workflow_facade, "run_current_gateway_request", fake_run_current_gateway_request)
    monkeypatch.setattr(
        workflow_facade,
        "build_current_gateway_profile_schema_resource_resolver",
        lambda: resolver_sentinel,
        raising=False,
    )

    phase_backend_client = FakePhaseBackendClient()
    tool_host = object()
    external_schema_resources = {"writing-plans-proof-loop-plan-model-solution": []}
    payload = {
        "workflowId": "solution-modeling-pack",
        "runId": "run-live-2",
        "inputs": {"brief": {"task": "Freeze package."}},
        "gatewaySource": {"platform": "telegram", "chat_id": "-100123456", "thread_id": "777"},
        "hostMode": "live",
    }
    stores = {
        "root": str(tmp_path / "gateway-root"),
        "phase_backend_client": phase_backend_client,
        "tool_host": tool_host,
        "external_schema_resources": external_schema_resources,
        "review_sender": review_sender,
    }

    assert workflow_facade.start_workflow(payload, stores) == {"status": "ok"}

    config = captured["config"]
    assert config.phase_backend_client is phase_backend_client
    assert config.tool_host is tool_host
    assert config.external_schema_resource_resolver is resolver_sentinel
    assert config.external_schema_resources == external_schema_resources
    assert phase_backend_client.bound_artifacts_dir == config.artifacts_dir
    assert captured["host_mode"] == "live"
    assert captured["host_config"] == {"review_sender": review_sender}
