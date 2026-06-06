"""Public Hermes workflow facade backed by durable current-gateway authorities.

This module exposes only the docs/03 workflow-level command surface. It does not
export phase-level launch/control helpers and does not mutate Canon core.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from canon.durability import LocalArtifactBackend, SqliteCheckpointBackend
from canon.journal import SqliteExecutionJournal

# canon_workflows.registry is only available inside the Canon repo tree.
# When running tests from the Hermes checkout, it will not be importable.
# The facade still supports workflowRef-based resolution, which is the
# primary path for the /canon run gateway command.
try:
    from canon_workflows.registry import get_workflow_pack_path
except ModuleNotFoundError:
    get_workflow_pack_path = None  # type: ignore[assignment]

# Static mapping of known workflow names to repo-relative workflowRef paths.
# This allows the facade to resolve workflow names when canon_workflows.registry
# is unavailable (e.g., imported from the Hermes checkout) without hardcoding
# any workflow as the sole production path in the command handler.
_KNOWN_WORKFLOW_REFS: dict[str, str] = {
    "autonomous-development-pack": "src/canon_workflows/packs/autonomous_development_pack/workflow.json",
    "solution-modeling": "src/canon_workflows/packs/solution_modeling_pack/workflow.json",
    "solution-modeling-pack": "src/canon_workflows/packs/solution_modeling_pack/workflow.json",
}

from integrations.hermes.canon_hermes.current_gateway import (
    build_current_gateway_profile,
    build_current_gateway_request,
)
from integrations.hermes.canon_hermes.current_gateway_operator_commands import (
    cancel_workflow_for_operator,
    inspect_run_for_operator,
    pause_workflow_for_operator,
    restart_workflow_for_operator,
    resume_workflow_for_operator,
)
from integrations.hermes.canon_hermes.current_gateway_runner import (
    CurrentGatewayRunnerConfig,
    build_current_gateway_profile_schema_resource_resolver,
    build_current_gateway_runner_config,
    run_current_gateway_request,
)
from integrations.hermes.canon_hermes.workflow_index import inspect_run

_SECRET_KEY_PARTS = ("token", "secret", "password", "api_key", "apikey", "private_key")
_REPO_ROOT = Path(__file__).resolve().parents[3]


def start_workflow(payload: dict[str, Any], stores: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start one public workflow run through the durable current-gateway runner seam.

    pre: payload names a workflow plus gatewaySource/hostMode authority; when private requestAuthority
         is supplied, its run/input authority must agree with any duplicated public fields.
    post: current-gateway-owned workflow/thread/host/gateway selectors override stale request-owned
          values while safe request-owned runtime/model routing/context authority is preserved.
    post: stores may carry private phase backend, tool host, phase profile, and explicit external
          schema-resource authority that is threaded into CurrentGatewayRunnerConfig.
    raises: ValueError when mixed public/private authorities conflict or preserved request authority
            is malformed for fail-closed runtime execution.
    """
    normalized_payload = _require_payload(payload)
    _reject_secret_bearing_mapping(normalized_payload, label="payload")
    _reject_phase_level_payload(normalized_payload)
    workflow_ref = _resolve_workflow_ref(normalized_payload)
    request = _normalized_current_gateway_request(
        normalized_payload,
        workflow_ref=workflow_ref,
        stores=stores,
    )
    config = _build_gateway_config(normalized_payload, stores)
    _bind_phase_backend_artifacts_dir(config)
    host_mode = _optional_payload_text(normalized_payload, "hostMode", "host_mode")
    if host_mode is None:
        raise ValueError("hostMode is required; public workflow facade must not default to dry-run")
    host_config = _selected_host_config(normalized_payload, stores)
    return run_current_gateway_request(
        request,
        config=config,
        host_mode=host_mode,
        host_config=host_config,
    )


def _selected_host_config(payload: dict[str, Any], stores: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the private current-gateway host config for one workflow launch.

    pre: payload may carry public hostConfig and stores may carry private review_sender authority.
    post: public hostConfig is preserved, while a private callable review_sender from stores is merged
          into the host config without widening the public workflow payload schema.
    raises: ValueError when the private review_sender authority is present but not callable.
    """

    selected = payload.get("hostConfig")
    private_review_sender = None if stores is None else stores.get("review_sender")
    if private_review_sender is None:
        return selected
    if not callable(private_review_sender):
        raise ValueError("stores.review_sender must be callable")
    if selected is None:
        return {"review_sender": private_review_sender}
    if not isinstance(selected, dict):
        raise ValueError("hostConfig must be an object")
    merged = dict(selected)
    merged.setdefault("review_sender", private_review_sender)
    return merged


def pause_workflow(payload: dict[str, Any], stores: dict[str, Any] | None = None) -> dict[str, Any]:
    """Request one origin-visible workflow pause through durable operator authority."""
    normalized_payload = _require_payload(payload)
    _reject_secret_bearing_mapping(normalized_payload, label="payload")
    _reject_phase_level_payload(normalized_payload)
    durable = _durable_stores(normalized_payload, stores)
    return pause_workflow_for_operator(
        run_id=_payload_text(normalized_payload, "runId", "run_id"),
        origin=_origin(normalized_payload),
        journal=durable["journal"],
        artifacts=durable["artifacts"],
        checkpoint_backend=durable["checkpoint_backend"],
        reason=_optional_payload_text(normalized_payload, "reason"),
    )


def resume_workflow(payload: dict[str, Any], stores: dict[str, Any] | None = None) -> dict[str, Any]:
    """Request one origin-visible workflow resume through durable operator authority."""
    normalized_payload = _require_payload(payload)
    _reject_secret_bearing_mapping(normalized_payload, label="payload")
    _reject_phase_level_payload(normalized_payload)
    durable = _durable_stores(normalized_payload, stores)
    return resume_workflow_for_operator(
        run_id=_payload_text(normalized_payload, "runId", "run_id"),
        origin=_origin(normalized_payload),
        journal=durable["journal"],
        artifacts=durable["artifacts"],
        checkpoint_backend=durable["checkpoint_backend"],
        reason=_optional_payload_text(normalized_payload, "reason"),
    )


def cancel_workflow(payload: dict[str, Any], stores: dict[str, Any] | None = None) -> dict[str, Any]:
    """Request one origin-visible workflow cancellation through durable operator authority."""
    normalized_payload = _require_payload(payload)
    _reject_secret_bearing_mapping(normalized_payload, label="payload")
    _reject_phase_level_payload(normalized_payload)
    durable = _durable_stores(normalized_payload, stores)
    return cancel_workflow_for_operator(
        run_id=_payload_text(normalized_payload, "runId", "run_id"),
        origin=_origin(normalized_payload),
        journal=durable["journal"],
        artifacts=durable["artifacts"],
        checkpoint_backend=durable["checkpoint_backend"],
        active_handle=_optional_payload_text(normalized_payload, "activeHandle", "active_handle"),
        cancel_active_handle=normalized_payload.get("cancelActiveHandle"),
        reason=_optional_payload_text(normalized_payload, "reason"),
    )


def inspect_workflow(payload: dict[str, Any], stores: dict[str, Any] | None = None) -> dict[str, Any]:
    """Inspect one origin-visible workflow run from durable journal/artifact truth."""
    normalized_payload = _require_payload(payload)
    _reject_secret_bearing_mapping(normalized_payload, label="payload")
    _reject_phase_level_payload(normalized_payload)
    durable = _durable_stores(normalized_payload, stores)
    return inspect_run_for_operator(
        run_id=_payload_text(normalized_payload, "runId", "run_id"),
        origin=_origin(normalized_payload),
        journal=durable["journal"],
        artifacts=durable["artifacts"],
    )


def restart_workflow_from_checkpoint(
    payload: dict[str, Any], stores: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Restart one origin-visible workflow from checkpoint authority only."""
    normalized_payload = _require_payload(payload)
    _reject_secret_bearing_mapping(normalized_payload, label="payload")
    _reject_phase_level_payload(normalized_payload)
    durable = _durable_stores(normalized_payload, stores)
    return restart_workflow_for_operator(
        checkpoint_id=_payload_text(normalized_payload, "checkpointId", "checkpoint_id"),
        origin=_origin(normalized_payload),
        journal=durable["journal"],
        artifacts=durable["artifacts"],
        checkpoint_backend=durable["checkpoint_backend"],
    )


def stream_workflow_events(payload: dict[str, Any], stores: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return durable run summary plus journal rows for one origin-visible workflow run."""
    normalized_payload = _require_payload(payload)
    _reject_secret_bearing_mapping(normalized_payload, label="payload")
    _reject_phase_level_payload(normalized_payload)
    durable = _durable_stores(normalized_payload, stores)
    run_id = _payload_text(normalized_payload, "runId", "run_id")
    summary = inspect_run_for_operator(
        run_id=run_id,
        origin=_origin(normalized_payload),
        journal=durable["journal"],
        artifacts=durable["artifacts"],
    )
    return {
        "run": summary,
        "events": durable["journal"].list_run(run_id),
        "index": inspect_run(run_id=run_id, journal=durable["journal"], artifacts=durable["artifacts"]),
    }


def _require_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    return payload


def resolve_workflow_ref_for_command(
    *,
    workflow_name: str,
    workflow_ref: str | None = None,
) -> str:
    """Resolve a workflow name to a repo-relative workflowRef path.

    pre: workflow_name is a non-empty workflow identifier.
    post: returns a repo-relative path string suitable for build_current_gateway_request.
    raises: ValueError when the workflow cannot be resolved.
    """
    if workflow_ref:
        return workflow_ref

    # Try the known static mapping first (works even without canon_workflows.registry).
    known = _KNOWN_WORKFLOW_REFS.get(workflow_name)
    if known:
        return known

    # Try the dynamic registry (only available inside the Canon repo tree).
    if get_workflow_pack_path is not None:
        try:
            pack_root = get_workflow_pack_path(workflow_name)
        except ValueError as exc:
            raise ValueError(
                f"workflow '{workflow_name}' is not a known workflow: {exc}"
            ) from exc
        workflow_path = pack_root / "workflow.json"
        try:
            return str(workflow_path.resolve().relative_to(_REPO_ROOT))
        except ValueError:
            return str(workflow_path.resolve())

    raise ValueError(
        f"workflow '{workflow_name}' is not a known workflow and "
        f"canon_workflows.registry is unavailable; use workflowRef instead"
    )


def _resolve_workflow_ref(payload: dict[str, Any]) -> str:
    workflow_ref = _optional_payload_text(payload, "workflowRef", "workflow_ref")
    if workflow_ref:
        return workflow_ref
    workflow_id = _optional_payload_text(payload, "workflowId", "workflow_id")
    if not workflow_id:
        raise ValueError("workflowRef or workflowId is required")
    return resolve_workflow_ref_for_command(workflow_name=workflow_id)


def _normalized_current_gateway_request(
    payload: dict[str, Any],
    *,
    workflow_ref: str,
    stores: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the live current-gateway request while preserving only safe request-owned fields.

    pre: payload is the normalized public workflow facade payload and workflow_ref is the resolved
         current-gateway-owned workflow selection.
    post: returns a detached run-request.schema.v2 payload whose workflow/thread/host/gateway
          selectors come from current-gateway truth, while safe request-owned runtime/model/context
          authority is preserved when supplied through private requestAuthority.
    raises: ValueError when requestAuthority conflicts with duplicated public fields or injects
            request-owned gateway selectors.
    """

    request_authority = _optional_request_authority(payload)
    public_run_id = _payload_text(payload, "runId", "run_id")
    public_inputs = _payload_mapping(payload, "inputs", default={})
    run_id = public_run_id
    inputs = public_inputs
    workflow_version = _optional_payload_text(payload, "workflowVersion", "workflow_version")

    if request_authority is not None:
        _reject_secret_bearing_mapping(request_authority, label="payload.requestAuthority")
        request_run_id = _mapping_text(request_authority, "runId")
        request_inputs = _mapping_mapping(request_authority, "inputs")
        if request_run_id != public_run_id:
            raise ValueError("requestAuthority.runId conflicts with payload.runId")
        if request_inputs != public_inputs:
            raise ValueError("requestAuthority.inputs conflicts with payload.inputs")
        run_id = request_run_id
        inputs = request_inputs
        workflow_version = _optional_mapping_text(request_authority, "workflowVersion") or workflow_version

    request = build_current_gateway_request(
        workflow_ref=workflow_ref,
        run_id=run_id,
        inputs=inputs,
        gateway_source=_gateway_source(payload),
        profile=_profile_from_payload(payload),
        workflow_version=workflow_version,
    )
    if request_authority is None:
        return request

    normalized_request = _preserve_request_authority(request, request_authority)
    _reject_conflicting_phase_profile_authority(normalized_request, stores)
    return normalized_request


def _build_gateway_config(
    payload: dict[str, Any], stores: dict[str, Any] | None
) -> CurrentGatewayRunnerConfig:
    """Build one current-gateway runner config from public payload plus private stores.

    pre: stores may provide explicit private runtime authorities for backend/tool/schema seams.
    post: when stores omit external_schema_resource_resolver, the config uses Canon's explicit
          current-gateway resolver factory instead of launcher-owned workflow-name heuristics.
    raises: ValueError from delegated authority validators.
    """

    explicit_root = _explicit_gateway_root(payload, stores)
    phase_profiles = _selected_phase_profiles(payload, stores)
    phase_backend_client = _store_authority(stores, "phase_backend_client", "phaseBackendClient")
    tool_host = _store_authority(stores, "tool_host", "toolHost")
    external_schema_resource_resolver = _store_authority(
        stores,
        "external_schema_resource_resolver",
        "externalSchemaResourceResolver",
    )
    if external_schema_resource_resolver is None:
        external_schema_resource_resolver = build_current_gateway_profile_schema_resource_resolver()
    external_schema_resources = _store_authority(
        stores,
        "external_schema_resources",
        "externalSchemaResources",
    )
    if explicit_root is not None:
        return CurrentGatewayRunnerConfig(
            explicit_root,
            phase_backend_client=phase_backend_client,
            tool_host=tool_host,
            phase_profile_map=phase_profiles,
            external_schema_resource_resolver=external_schema_resource_resolver,
            external_schema_resources=external_schema_resources,
        )
    return build_current_gateway_runner_config(
        phase_backend_client=phase_backend_client,
        tool_host=tool_host,
        phase_profile_map=phase_profiles,
        external_schema_resource_resolver=external_schema_resource_resolver,
        external_schema_resources=external_schema_resources,
    )


def _profile_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Build a default current-gateway profile when public payload declares phaseProfiles.

    post: returns None when the caller did not request per-phase Hermes profile binding; otherwise
          returns a fail-closed current-gateway profile carrying the phaseProfiles map.
    raises: ValueError when phaseProfiles is present but malformed.
    """

    phase_profiles = _phase_profiles_from_payload(payload)
    if phase_profiles is None:
        return None
    return build_current_gateway_profile(phase_profiles=phase_profiles)


def _phase_profiles_from_payload(payload: dict[str, Any]) -> dict[str, str] | None:
    raw = payload.get("hermesPhaseProfiles")
    if raw is None:
        raw = payload.get("hermes_phase_profiles")
    if raw is None:
        raw = payload.get("phaseProfiles")
    if raw is None:
        raw = payload.get("phase_profiles")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("phaseProfiles must be an object mapping phase ids to Hermes profile ids")
    phase_profiles: dict[str, str] = {}
    for phase_id, hermes_profile_id in raw.items():
        if not isinstance(phase_id, str) or not phase_id.strip():
            raise ValueError("phaseProfiles phase ids must be non-empty strings")
        if not isinstance(hermes_profile_id, str) or not hermes_profile_id.strip():
            raise ValueError(f"phaseProfiles.{phase_id} must name a non-empty Hermes profile id")
        phase_profiles[phase_id] = hermes_profile_id
    return phase_profiles


def _selected_phase_profiles(payload: dict[str, Any], stores: dict[str, Any] | None) -> dict[str, str] | None:
    """Return one fail-closed phase-profile map from payload/store authority.

    pre: payload may declare public phaseProfiles and stores may carry private phase_profile_map.
    post: returns the shared mapping when both sources agree, otherwise the only present source.
    raises: ValueError when public and private phase-profile authorities disagree.
    """

    payload_phase_profiles = _phase_profiles_from_payload(payload)
    store_phase_profiles = _store_phase_profiles(stores)
    if payload_phase_profiles is not None and store_phase_profiles is not None and payload_phase_profiles != store_phase_profiles:
        raise ValueError("phaseProfiles conflicts with private store phase_profile_map")
    return payload_phase_profiles if payload_phase_profiles is not None else store_phase_profiles


def _durable_stores(payload: dict[str, Any], stores: dict[str, Any] | None) -> dict[str, Any]:
    normalized_stores = stores if isinstance(stores, dict) else {}
    journal = normalized_stores.get("journal")
    artifacts = normalized_stores.get("artifacts")
    checkpoint_backend = normalized_stores.get("checkpoint_backend") or normalized_stores.get("checkpointBackend")
    if journal is not None and artifacts is not None and checkpoint_backend is not None:
        return {
            "journal": journal,
            "artifacts": artifacts,
            "checkpoint_backend": checkpoint_backend,
        }
    config = _build_gateway_config(payload, stores)
    return {
        "journal": SqliteExecutionJournal(config.journal_path),
        "artifacts": LocalArtifactBackend(config.artifacts_dir),
        "checkpoint_backend": SqliteCheckpointBackend(config.checkpoint_path),
    }


def _gateway_source(payload: dict[str, Any]) -> dict[str, Any]:
    gateway_source = payload.get("gatewaySource")
    if gateway_source is None:
        gateway_source = payload.get("gateway_source")
    if not isinstance(gateway_source, dict):
        raise ValueError("gatewaySource is required")
    return gateway_source


def _origin(payload: dict[str, Any]) -> str:
    explicit_origin = _optional_payload_text(payload, "origin")
    if explicit_origin:
        return explicit_origin
    gateway_source = _gateway_source(payload)
    session_key = _optional_mapping_text(gateway_source, "session_key", "sessionKey")
    if session_key:
        return session_key
    session_id = _optional_mapping_text(gateway_source, "session_id", "sessionId")
    if session_id:
        return session_id
    platform = _optional_mapping_text(gateway_source, "platform")
    chat_id = _optional_mapping_text(gateway_source, "chat_id", "chatId")
    thread_id = _optional_mapping_text(gateway_source, "thread_id", "threadId")
    if platform and chat_id and thread_id:
        return f"{platform}:{chat_id}:{thread_id}"
    if platform and chat_id:
        return f"{platform}:{chat_id}"
    raise ValueError("origin or gatewaySource identity is required")


def _explicit_gateway_root(payload: dict[str, Any], stores: dict[str, Any] | None) -> str | Path | None:
    store_config = payload.get("storeConfig")
    if isinstance(store_config, dict):
        for key in ("root", "gatewayRoot", "gateway_root"):
            value = store_config.get(key)
            if isinstance(value, (str, Path)) and str(value):
                return value
    for key in ("root", "gatewayRoot", "gateway_root"):
        value = payload.get(key)
        if isinstance(value, (str, Path)) and str(value):
            return value
    if isinstance(stores, dict):
        for key in ("root", "gatewayRoot", "gateway_root"):
            value = stores.get(key)
            if isinstance(value, (str, Path)) and str(value):
                return value
    return None


def _store_phase_profiles(stores: dict[str, Any] | None) -> dict[str, str] | None:
    raw = _store_authority(stores, "phase_profile_map", "phaseProfileMap")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("phase_profile_map must be an object mapping phase ids to Hermes profile ids")
    phase_profiles: dict[str, str] = {}
    for phase_id, hermes_profile_id in raw.items():
        if not isinstance(phase_id, str) or not phase_id.strip():
            raise ValueError("phase_profile_map phase ids must be non-empty strings")
        if not isinstance(hermes_profile_id, str) or not hermes_profile_id.strip():
            raise ValueError(f"phase_profile_map.{phase_id} must name a non-empty Hermes profile id")
        phase_profiles[phase_id] = hermes_profile_id
    return phase_profiles


def _store_authority(stores: dict[str, Any] | None, *keys: str) -> Any:
    if not isinstance(stores, dict):
        return None
    for key in keys:
        if key in stores:
            return stores[key]
    return None


def _bind_phase_backend_artifacts_dir(config: CurrentGatewayRunnerConfig) -> None:
    """Bind the concrete phase backend client to the config-selected artifact root when supported.

    post: calls bind_artifacts_dir(config.artifacts_dir) exactly once when the private backend client
          exposes that method; otherwise leaves the config unchanged.
    raises: any exception surfaced by the concrete backend binding method.
    """

    phase_backend_client = getattr(config, "phase_backend_client", None)
    bind_artifacts_dir = getattr(phase_backend_client, "bind_artifacts_dir", None)
    if callable(bind_artifacts_dir):
        bind_artifacts_dir(getattr(config, "artifacts_dir", None))


def _payload_text(payload: dict[str, Any], *keys: str) -> str:
    value = _optional_payload_text(payload, *keys)
    if value is None:
        raise ValueError(f"required text field missing: {'/'.join(keys)}")
    return value


def _optional_payload_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _optional_mapping_text(mapping: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _mapping_text(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"requestAuthority.{key} is required")
    return value


def _mapping_mapping(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"requestAuthority.{key} must be an object")
    return value


def _optional_request_authority(payload: dict[str, Any]) -> dict[str, Any] | None:
    raw = payload.get("requestAuthority")
    if raw is None:
        raw = payload.get("request_authority")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("requestAuthority must be an object")
    return raw


def _preserve_request_authority(base_request: dict[str, Any], request_authority: dict[str, Any]) -> dict[str, Any]:
    """Preserve safe request-owned authority while keeping gateway-owned selectors authoritative.

    pre: base_request is the current-gateway-owned request projection and request_authority is the
         preloaded runtime request object from Hermes.
    post: returns a detached request that preserves request-owned schema/runtime/model/context fields
          but keeps workflowRef/threadId/hostIntegration/runtimeContext.gateway from base_request.
    raises: ValueError when request_authority carries malformed preserved fields or request-owned
            gateway context.
    """

    normalized = deepcopy(base_request)
    schema_version = request_authority.get("schemaVersion")
    if schema_version is not None:
        if schema_version != "run-request.schema.v2":
            raise ValueError("requestAuthority.schemaVersion must be 'run-request.schema.v2'")
        normalized["schemaVersion"] = schema_version
    workflow_version = _optional_mapping_text(request_authority, "workflowVersion")
    if workflow_version is not None:
        normalized["workflowVersion"] = workflow_version
    runtime = request_authority.get("runtime")
    if runtime is not None:
        if not isinstance(runtime, dict):
            raise ValueError("requestAuthority.runtime must be an object")
        normalized["runtime"] = deepcopy(runtime)
    model_routing = request_authority.get("modelRouting")
    if model_routing is not None:
        if not isinstance(model_routing, dict):
            raise ValueError("requestAuthority.modelRouting must be an object")
        normalized["modelRouting"] = deepcopy(model_routing)
    config_profile_id = request_authority.get("configProfileId")
    if config_profile_id is not None:
        if not isinstance(config_profile_id, str) or not config_profile_id:
            raise ValueError("requestAuthority.configProfileId must be a non-empty string")
        normalized["configProfileId"] = config_profile_id
    overlay_ids = request_authority.get("overlayIds")
    if overlay_ids is not None:
        if not isinstance(overlay_ids, list):
            raise ValueError("requestAuthority.overlayIds must be an array")
        normalized["overlayIds"] = deepcopy(overlay_ids)
    runtime_context = request_authority.get("runtimeContext")
    if runtime_context is not None:
        if not isinstance(runtime_context, dict):
            raise ValueError("requestAuthority.runtimeContext must be an object")
        if "gateway" in runtime_context:
            raise ValueError("requestAuthority.runtimeContext.gateway is forbidden; gateway identity is current-gateway-owned")
        merged_runtime_context = deepcopy(normalized.get("runtimeContext") or {})
        if not isinstance(merged_runtime_context, dict):
            merged_runtime_context = {}
        for key, value in runtime_context.items():
            merged_runtime_context[key] = deepcopy(value)
        normalized["runtimeContext"] = merged_runtime_context
    return normalized


def _reject_conflicting_phase_profile_authority(request: dict[str, Any], stores: dict[str, Any] | None) -> None:
    """Reject request/store phase-profile authority drift before runtime execution.

    pre: request is the normalized current-gateway run request and stores may carry private
         phase_profile_map authority for runner config.
    post: returns only when request/runtimeContext.hermes.phaseProfiles is absent or matches the
          private store mapping exactly.
    raises: ValueError when both authorities are present but disagree.
    """

    store_phase_profiles = _store_phase_profiles(stores)
    if store_phase_profiles is None:
        return
    runtime_context = request.get("runtimeContext")
    if not isinstance(runtime_context, dict):
        return
    hermes_context = runtime_context.get("hermes")
    if hermes_context is None:
        return
    if not isinstance(hermes_context, dict):
        raise ValueError("request.runtimeContext.hermes must be an object")
    request_phase_profiles = hermes_context.get("phaseProfiles")
    if request_phase_profiles is None:
        return
    if request_phase_profiles != store_phase_profiles:
        raise ValueError("requestAuthority.runtimeContext.hermes.phaseProfiles conflicts with private store phase_profile_map")


def _payload_mapping(payload: dict[str, Any], key: str, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    value = payload.get(key)
    if value is None:
        return dict(default or {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _reject_secret_bearing_mapping(mapping: dict[str, Any], label: str) -> None:
    for key, value in mapping.items():
        normalized = str(key).lower().replace("-", "_")
        if any(part in normalized for part in _SECRET_KEY_PARTS):
            raise PermissionError(f"public workflow payload contains secret-bearing field: {key}")
        if isinstance(value, dict):
            _reject_secret_bearing_mapping(value, f"{label}.{key}")


def _reject_phase_level_payload(payload: dict[str, Any]) -> None:
    forbidden_keys = {
        key
        for key in payload
        if isinstance(key, str)
        and (
            key.lower().startswith("phase")
            or key.lower().endswith("phase")
            or key in {"compiledGraphId", "nodeId", "node_id"}
        )
    }
    if forbidden_keys:
        raise PermissionError("phase-level public command payload is forbidden")


__all__ = [
    "cancel_workflow",
    "cancelWorkflow",
    "inspect_workflow",
    "inspectWorkflow",
    "pause_workflow",
    "pauseWorkflow",
    "restart_workflow_from_checkpoint",
    "restartWorkflowFromCheckpoint",
    "resolve_workflow_ref_for_command",
    "resume_workflow",
    "resumeWorkflow",
    "start_workflow",
    "startWorkflow",
    "stream_workflow_events",
    "streamWorkflowEvents",
]

# Public camelCase aliases matching the adapter contract docs (03-hermes-adapter-contract.md).
startWorkflow = start_workflow
pauseWorkflow = pause_workflow
resumeWorkflow = resume_workflow
cancelWorkflow = cancel_workflow
inspectWorkflow = inspect_workflow
restartWorkflowFromCheckpoint = restart_workflow_from_checkpoint
streamWorkflowEvents = stream_workflow_events
