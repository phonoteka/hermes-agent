"""Public Hermes workflow facade backed by durable current-gateway authorities.

This module exposes only the docs/03 workflow-level command surface. It does not
export phase-level launch/control helpers and does not mutate Canon core.
"""

from __future__ import annotations

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
    "solution-modeling": "src/canon_workflows/packs/solution_modeling_pack/workflow.json",
}

from integrations.hermes.canon_hermes.current_gateway import build_current_gateway_request
from integrations.hermes.canon_hermes.current_gateway_operator_commands import (
    cancel_workflow_for_operator,
    inspect_run_for_operator,
    pause_workflow_for_operator,
    restart_workflow_for_operator,
    resume_workflow_for_operator,
)
from integrations.hermes.canon_hermes.current_gateway_runner import (
    CurrentGatewayRunnerConfig,
    build_current_gateway_runner_config,
    run_current_gateway_request,
)
from integrations.hermes.canon_hermes.workflow_index import inspect_run

_SECRET_KEY_PARTS = ("token", "secret", "password", "api_key", "apikey", "private_key")
_REPO_ROOT = Path(__file__).resolve().parents[3]


def start_workflow(payload: dict[str, Any], stores: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start one public workflow run through the durable current-gateway runner seam."""
    normalized_payload = _require_payload(payload)
    _reject_secret_bearing_mapping(normalized_payload, label="payload")
    _reject_phase_level_payload(normalized_payload)
    workflow_ref = _resolve_workflow_ref(normalized_payload)
    request = build_current_gateway_request(
        workflow_ref=workflow_ref,
        run_id=_payload_text(normalized_payload, "runId", "run_id"),
        inputs=_payload_mapping(normalized_payload, "inputs", default={}),
        gateway_source=_gateway_source(normalized_payload),
        workflow_version=_optional_payload_text(normalized_payload, "workflowVersion", "workflow_version"),
    )
    config = _build_gateway_config(normalized_payload, stores)
    host_mode = _optional_payload_text(normalized_payload, "hostMode", "host_mode")
    if host_mode is None:
        raise ValueError("hostMode is required; public workflow facade must not default to dry-run")
    host_config = normalized_payload.get("hostConfig")
    return run_current_gateway_request(
        request,
        config=config,
        host_mode=host_mode,
        host_config=host_config,
    )


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
    work_id = _optional_payload_text(payload, "workflowId", "workflow_id")
    if not workflow_ref and not work_id:
        raise ValueError("workflowRef or workflowId is required")
    return resolve_workflow_ref_for_command(workflow_name=work_id or "", workflow_ref=workflow_ref)


def _build_gateway_config(
    payload: dict[str, Any], stores: dict[str, Any] | None
) -> CurrentGatewayRunnerConfig:
    explicit_root = _explicit_gateway_root(payload, stores)
    if explicit_root is not None:
        return CurrentGatewayRunnerConfig(explicit_root)
    return build_current_gateway_runner_config()


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
