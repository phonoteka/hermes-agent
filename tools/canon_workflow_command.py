"""Gateway-facing `/canon` command parsing and fail-closed operator responses.

This module intentionally keeps `/canon` parsing local to Hermes gateway while
delegating durable latest/inspect reads to Canon integration authority.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Awaitable, Callable

from hermes_constants import get_hermes_home
from tools.canon_workflow_observe import to_markdown as _observe_to_markdown


def handle_gateway_canon_command(event: Any) -> str:
    """Parse `/canon` command text and return a deterministic fail-closed response.

    pre: event has `.text` and optional `.source` with chat/thread/user identity attributes.
    post: `/canon run <workflow> --inputs-json <json-object>` validates operator input and returns
          acceptance/guidance only; it does not imply execution success.
    post: `/canon latest` and `/canon inspect <run-id>` query Canon durable stores when available.
    post: blocked/dry-run/non-terminal statuses are labeled non-production in operator output.
    raises: none.
    """

    text = (getattr(event, "text", "") or "").strip()
    args = text[len("/canon") :].strip() if text.startswith("/canon") else text
    if not args:
        return "Usage: /canon <run|list|full|timeline|artifacts|events|report|control|latest|inspect> ..."

    subcommand = _extract_canon_subcommand(args)
    source = getattr(event, "source", None)
    if subcommand == "run":
        return _handle_run(command_text=args, source=source)
    tokens = shlex.split(args)
    if subcommand in {"latest", "inspect"}:
        if subcommand == "latest":
            return _handle_latest(source=source)
        if len(tokens) < 2 or not tokens[1].strip():
            return "`/canon inspect` requires an explicit run id: /canon inspect <run-id>."
        return _handle_inspect(run_id=tokens[1].strip(), source=source)
    if subcommand == "list":
        return _handle_list(source=source)
    if subcommand in {"full", "timeline", "artifacts", "events", "report", "control"}:
        if len(tokens) < 2 or not tokens[1].strip():
            return f"`/canon {subcommand}` failed closed: missing run id. Usage: /canon {subcommand} <run-id>."
        run_id = tokens[1].strip()
        if subcommand == "events":
            try:
                after_sequence, limit = _parse_events_cursor_args(tokens[2:])
            except ValueError as exc:
                return (
                    f"`/canon events {run_id}` failed closed: {exc}. "
                    "Usage: /canon events <run-id> [--after-sequence N] [--limit N]."
                )
            return _handle_observability_subcommand(
                subcommand=subcommand,
                run_id=run_id,
                source=source,
                after_sequence=after_sequence,
                limit=limit,
            )
        return _handle_observability_subcommand(subcommand=subcommand, run_id=run_id, source=source)
    return "Unsupported /canon subcommand. Usage: /canon <run|list|full|timeline|artifacts|events|report|control|latest|inspect> ..."


async def handle_gateway_canon_command_live(
    event: Any,
    *,
    send_review_prompt: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> str:
    """Execute `/canon` with live current-gateway wiring for `/canon run`.

    pre: event contains message text and source identity; `send_review_prompt` is provided for run path.
    post: non-run commands keep deterministic sync behavior.
    post: run path forwards workflow/input authority through the shared gateway-owned
          workflow facade using the active Hermes current-gateway durable root.
    raises: none.
    """

    text = (getattr(event, "text", "") or "").strip()
    args = text[len("/canon") :].strip() if text.startswith("/canon") else text
    if not args:
        return "Usage: /canon <run|list|full|timeline|artifacts|events|report|control|latest|inspect> ..."

    if _extract_canon_subcommand(args) != "run":
        return handle_gateway_canon_command(event)

    try:
        if send_review_prompt is None:
            raise ValueError("live /canon run requires gateway review sender authority")
        parsed = parse_gateway_run_command(raw_command_text=args)
        return await _run_gateway_canon_cli_command(
            workflow=parsed["workflow"],
            inputs=parsed["inputs"],
            gateway_source=_gateway_source_from_live_event(event),
            gateway_root=_canon_gateway_durable_root(),
            send_review_prompt=send_review_prompt,
        )
    except Exception as exc:
        return f"`/canon run` failed closed: {exc}"


def _extract_canon_subcommand(args: str) -> str:
    """Return the first `/canon` subcommand token from raw command text.

    pre: args is the raw `/canon` tail with the command word first when present.
    post: returns the lower-cased first token or the empty string when missing.
    raises: none.
    """

    normalized = str(args or "").strip()
    if not normalized:
        return ""
    return normalized.split(None, 1)[0].strip().lower()


def _handle_run(*, command_text: str, source: Any) -> str:
    """Handle `/canon run` argument validation and topic identity echo.

    pre: command_text is the raw `/canon` tail beginning with `run`.
    post: requires explicit workflow selector plus `--inputs-json <json-object>` authority.
    post: response includes source chat/thread identity when available.
    raises: none.
    """

    try:
        parsed = parse_gateway_run_command(raw_command_text=command_text)
    except ValueError as exc:
        return str(exc)

    workflow = parsed["workflow"]
    inputs = parsed["inputs"]

    chat_id = str(getattr(source, "chat_id", "") or "")
    thread_id = str(getattr(source, "thread_id", "") or "")
    origin_bits = []
    if chat_id:
        origin_bits.append(f"chat_id={chat_id}")
    if thread_id:
        origin_bits.append(f"message_thread_id={thread_id}")
    origin_suffix = f" ({', '.join(origin_bits)})" if origin_bits else ""

    return (
        "`/canon run` accepted for Canon current-gateway dispatch"
        f"{origin_suffix}: workflow={workflow}, inputs={json.dumps(inputs, ensure_ascii=False, sort_keys=True)}."
        " Execution happens only through the live gateway-owned workflow facade path."
    )


def parse_gateway_run_command(
    tokens: list[str] | None = None,
    *,
    raw_command_text: str | None = None,
) -> dict[str, Any]:
    """Parse one `/canon run` command into workflow-neutral execution authority.

    pre: caller provides either tokenized `tokens` or raw `/canon` tail text beginning with `run`.
    post: returns only `workflow` and one JSON-object `inputs` mapping.
    raises: ValueError when workflow/input authority is missing, malformed, or mixed with
            unsupported free-form tokens.
    """

    if raw_command_text is not None:
        return _parse_gateway_run_command_text(raw_command_text)
    if tokens is None:
        raise ValueError("`/canon run` parser requires tokens or raw command text")
    return _parse_gateway_run_command_tokens(tokens)


def _parse_gateway_run_command_tokens(tokens: list[str]) -> dict[str, Any]:
    """Parse tokenized `/canon run` arguments.

    pre: tokens is the shlex-split tail of a `/canon` command and tokens[0] == `run`.
    post: returns only workflow selector plus one JSON-object `inputs` mapping.
    raises: ValueError when workflow/input authority is missing or malformed.
    """

    if len(tokens) < 2 or not tokens[1].strip():
        raise ValueError(
            "`/canon run` requires workflow selector and input authority: "
            "/canon run <workflow> --inputs-json <json-object>."
        )

    workflow = tokens[1].strip()
    inputs = _consume_inputs_json_flag(tokens[2:])
    return {"workflow": workflow, "inputs": inputs}


def _parse_gateway_run_command_text(raw_command_text: str) -> dict[str, Any]:
    """Parse raw Telegram `/canon run` text without shell-tokenizing the JSON substring.

    pre: raw_command_text is the raw `/canon` tail beginning with `run`.
    post: preserves the exact substring after `--inputs-json` for JSON decoding.
    raises: ValueError when free-form text is used or the JSON-object authority is missing.
    """

    normalized = str(raw_command_text or "").strip()
    match = re.match(r"^run\s+(\S+)(?:\s+(.*))?$", normalized, flags=re.DOTALL)
    if match is None:
        raise ValueError(
            "`/canon run` requires workflow selector and input authority: "
            "/canon run <workflow> --inputs-json <json-object>."
        )

    workflow = match.group(1).strip()
    remainder = (match.group(2) or "").strip()
    if not remainder.startswith("--inputs-json"):
        raise ValueError(
            "`/canon run` requires `--inputs-json <json-object>`; "
            "free-form task text is not accepted."
        )
    raw_inputs = remainder[len("--inputs-json") :].strip()
    if not raw_inputs:
        raise ValueError("`--inputs-json` requires a JSON object.")
    return {"workflow": workflow, "inputs": _parse_inputs_json_object(raw_inputs)}


def _consume_inputs_json_flag(tokens: list[str]) -> dict[str, Any]:
    """Return the only supported `/canon run` execution-authority flag value.

    pre: tokens contains only the post-workflow portion of one `/canon run` command.
    post: returns the `--inputs-json` object when present exactly once and rejects free-form extras.
    raises: ValueError when the flag/value is missing or when unsupported extra tokens are present.
    """

    if not tokens or tokens[0] != "--inputs-json":
        raise ValueError(
            "`/canon run` requires `--inputs-json <json-object>`; "
            "free-form task text is not accepted."
        )
    if len(tokens) < 2 or not str(tokens[1] or "").strip():
        raise ValueError("`--inputs-json` requires a JSON object.")
    if len(tokens) > 2:
        raise ValueError(
            "`/canon run` accepts only workflow selector plus `--inputs-json <json-object>` authority."
        )
    raw_inputs = str(tokens[1]).strip()
    return _parse_inputs_json_object(raw_inputs)


def _parse_inputs_json_object(raw_inputs: str) -> dict[str, Any]:
    """Decode one `--inputs-json` payload as a JSON object.

    pre: raw_inputs is the exact string supplied after `--inputs-json`.
    post: returns the decoded JSON object.
    raises: ValueError when the payload is invalid JSON or not an object.
    """

    candidates = [raw_inputs]
    if len(raw_inputs) >= 2 and raw_inputs[0] == raw_inputs[-1] and raw_inputs[0] in {"'", '"'}:
        candidates.append(raw_inputs[1:-1])
    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if not isinstance(parsed, dict):
            raise ValueError("`--inputs-json` must be a JSON object")
        return parsed
    assert last_error is not None
    raise ValueError(f"`--inputs-json` must contain valid JSON: {last_error}") from last_error


def _require_non_empty_text(value: Any, label: str) -> str:
    """Return one stripped non-empty string value or fail closed.

    pre: value is any candidate text field and label names that field for diagnostics.
    post: returns the stripped string when non-empty.
    raises: ValueError when the value is blank after string coercion.
    """

    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    return normalized


def _canon_gateway_durable_root() -> Path:
    """Return the active Hermes current-gateway durable root for workflow facade runs.

    pre: Hermes home resolves for the active process.
    post: __return__ points at `<hermes_home>/canon-current-gateway` without creating it.
    raises: none.
    """

    return Path(get_hermes_home()) / "canon-current-gateway"


def _gateway_source_from_live_event(event: Any) -> dict[str, Any]:
    """Project one live gateway message into Canon-visible gateway source facts.

    pre: event has `.source` and `.message_id` fields from the active gateway adapter.
    post: returns public origin facts only; no request-json data is read here.
    raises: ValueError when the live event lacks source identity required by the Canon facade.
    """

    source = getattr(event, "source", None)
    if source is None:
        raise ValueError("live /canon run requires gateway source identity")
    gateway_source = {
        "platform": str(getattr(source, "platform", "") or ""),
        "chat_id": str(getattr(source, "chat_id", "") or ""),
        "thread_id": str(getattr(source, "thread_id", "") or ""),
        "user_id": str(getattr(source, "user_id", "") or ""),
        "user_name": str(getattr(source, "user_name", "") or ""),
        "session_id": str(getattr(source, "session_id", "") or ""),
        "session_key": str(getattr(source, "session_key", "") or ""),
        "message_id": str(getattr(event, "message_id", "") or ""),
    }
    if not gateway_source["platform"]:
        raise ValueError("live /canon run requires gateway platform identity")
    if not gateway_source["user_id"]:
        raise ValueError("live /canon run requires gateway user identity")
    if not gateway_source["message_id"]:
        raise ValueError("live /canon run requires gateway message identity")
    return gateway_source


def _gateway_source_from_launch_target(*, target: Mapping[str, Any], request_id: str) -> dict[str, Any]:
    """Build synthetic gateway source facts for one gateway-owned local launch request.

    pre: target is the validated launch target object and request_id is non-empty.
    post: returns a current-gateway origin envelope that preserves platform/chat/thread provenance
          while using gateway-owned synthetic user/message identities required by the Canon facade.
    raises: ValueError when required target fields are missing.
    """

    platform = str(target.get("platform") or "").strip().lower()
    chat_id = str(target.get("chatId") or "").strip()
    thread_id = str(target.get("threadId") or "").strip()
    if platform != "telegram":
        raise ValueError("target.platform must be telegram")
    if not chat_id:
        raise ValueError("target.chatId is required")
    if not request_id:
        raise ValueError("request_id is required")
    gateway_source = {
        "platform": platform,
        "chat_id": chat_id,
        "user_id": "canon-gateway-launcher",
        "user_name": "canon-gateway-launcher",
        "message_id": request_id,
        "session_key": f"canon-gateway-launch:{platform}:{chat_id}" + (f":{thread_id}" if thread_id else ""),
    }
    if thread_id:
        gateway_source["thread_id"] = thread_id
    return gateway_source


def _load_runtime_request_authority(request_json: str) -> dict[str, Any]:
    """Load one run request file only at execution time and validate its authority shape.

    pre: request_json is an absolute existing JSON file path already accepted by parser/builder code.
    post: returns the full request object exactly as loaded from disk once runId/inputs authority is
          validated; parser/builder code does not synthesize or rewrite it before this seam.
    raises: ValueError when the file is unreadable, not JSON, not an object, lacks a non-empty runId,
            or carries non-object inputs.
    """

    request_path = Path(request_json)
    try:
        raw_payload = json.loads(request_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"request_json could not be read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"request_json must contain valid JSON: {exc}") from exc
    if not isinstance(raw_payload, dict):
        raise ValueError("request_json must contain a JSON object")
    run_id = raw_payload.get("runId")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("request_json runId is required")
    inputs = raw_payload.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("request_json inputs must be an object")
    return raw_payload


def _build_runtime_facade_stores(*, workflow: str, gateway_root: Path, request_authority: Mapping[str, Any]) -> dict[str, Any]:
    """Build the private stores/config authority passed to Canon's workflow facade.

    pre: gateway_root is the current-gateway durable root for this launch.
    post: returns only the launcher-owned private runtime authorities needed to reach the Canon-owned
          execution seam; workflow-specific schema/profile authority is not derived here.
    raises: none.
    """

    from integrations.hermes.canon_hermes.tool_host import HermesToolHost

    stores: dict[str, Any] = {
        "root": str(gateway_root),
        "phase_backend_client": create_gateway_phase_backend_client(),
        "tool_host": HermesToolHost(),
    }
    return stores


def _parse_gateway_review_target(target: Any) -> tuple[str, str | None]:
    """Parse one Canon review-delivery target into Telegram chat/thread ids.

    pre: target is the Canon current-gateway delivery target string `telegram:<chat_id>` or
         `telegram:<chat_id>:<thread_id>`.
    post: returns the exact chat/thread ids required by Hermes gateway adapter delivery.
    raises: ValueError when the target is blank, malformed, or not Telegram-owned.
    """

    normalized_target = str(target or "").strip()
    if not normalized_target:
        raise ValueError("current-gateway review sender target is required")
    platform, separator, remainder = normalized_target.partition(":")
    chat_id, separator_2, thread_id = remainder.partition(":")
    if platform != "telegram" or not separator or not chat_id:
        raise ValueError(
            "current-gateway review sender target must be `telegram:<chat_id>` or `telegram:<chat_id>:<thread_id>`"
        )
    return chat_id, thread_id or None


def _build_gateway_review_sender_bridge(
    send_review_prompt: Callable[..., Awaitable[dict[str, Any]]] | None,
) -> Callable[[dict[str, Any]], dict[str, Any]] | None:
    """Adapt one Hermes async review sender to Canon's synchronous sender contract.

    pre: send_review_prompt is the active Hermes gateway review-delivery coroutine or None.
    post: returns None when no live sender is available; otherwise returns a synchronous callable
          that parses Canon target authority and blocks for the gateway adapter delivery result.
    raises: RuntimeError when the bridge is created outside an active asyncio loop.
    raises: ValueError when Canon delivery payload is malformed.
    """

    if send_review_prompt is None:
        return None
    loop = asyncio.get_running_loop()

    def _sender(payload: dict[str, Any]) -> dict[str, Any]:
        """Deliver one Canon review card through the active Hermes gateway adapter.

        pre: payload is the Canon current-gateway sender payload with target/run/message authority.
        post: returns the exact gateway adapter delivery result for durable evidence persistence.
        raises: ValueError when payload is malformed.
        raises: Exception from the underlying Hermes gateway sender.
        """

        if not isinstance(payload, dict):
            raise ValueError("current-gateway review sender payload must be an object")
        chat_id, thread_id = _parse_gateway_review_target(payload.get("target"))
        run_id = _require_non_empty_text(payload.get("runId"), "review_sender.runId")
        text = _require_non_empty_text(payload.get("message"), "review_sender.message")
        future = asyncio.run_coroutine_threadsafe(
            send_review_prompt(
                chat_id=chat_id,
                thread_id=thread_id,
                run_id=run_id,
                text=text,
                callbacks=payload.get("callbacks"),
                gate_identity=payload.get("gateIdentity"),
                downloadable_artifacts=payload.get("downloadableArtifacts"),
            ),
            loop,
        )
        result = future.result()
        if not isinstance(result, dict):
            raise ValueError("gateway review sender must return a delivery object")
        message_id = _require_non_empty_text(
            result.get("messageId") or result.get("message_id"),
            "gateway review sender messageId",
        )
        delivery_result: dict[str, Any] = {"messageId": message_id, "chatId": str(chat_id)}
        if thread_id:
            delivery_result["threadId"] = str(thread_id)
        return delivery_result

    return _sender


def _build_runtime_facade_payload(
    *,
    workflow: str,
    inputs: Mapping[str, Any],
    gateway_source: Mapping[str, Any],
    gateway_root: Path,
    run_id: str | None = None,
    request_id: str | None = None,
    target: Mapping[str, Any] | None = None,
    request_authority: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the workflow-neutral Hermes->Canon facade payload for one runtime launch.

    pre: workflow/inputs identify the operator-approved launch request; gateway_source is the
         gateway-owned origin identity; gateway_root points at the active current-gateway durable root.
    post: returns a public workflow-facade payload with explicit or Hermes-minted runId, live host-mode
          authority, durable-root, and optional requestAuthority provenance attached.
    raises: ValueError when runtime input authority is invalid.
    """

    if not isinstance(inputs, Mapping):
        raise ValueError("inputs must be an object")
    if request_authority is not None and not isinstance(request_authority, Mapping):
        raise ValueError("request_authority must be an object")
    payload: dict[str, Any] = {
        "workflowId": workflow,
        "runId": str(run_id).strip() if run_id is not None else f"hermes-canon-{uuid.uuid4().hex}",
        "inputs": dict(inputs),
        "gatewaySource": dict(gateway_source),
        "hostMode": "live",
        "storeConfig": {"root": str(gateway_root)},
    }
    if not payload["runId"]:
        raise ValueError("run_id must be non-empty when supplied")
    if request_id is not None:
        payload["requestId"] = request_id
    if target is not None:
        payload["target"] = dict(target)
    if request_authority is not None:
        payload["requestAuthority"] = dict(request_authority)
    return payload


async def execute_gateway_workflow_facade_run(
    *,
    workflow: str,
    inputs: Mapping[str, Any],
    gateway_source: Mapping[str, Any],
    gateway_root: Path,
    run_id: str | None = None,
    request_id: str | None = None,
    target: Mapping[str, Any] | None = None,
    request_authority: Mapping[str, Any] | None = None,
    send_review_prompt: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Execute one gateway-owned Canon workflow run through the public facade seam.

    pre: workflow/inputs identify the requested run; gateway_source carries Hermes-owned origin
         identity; gateway_root selects the durable current-gateway store root.
    post: Canon is called through `workflow_facade.startWorkflow(...)`, never through `canon.cli`.
    raises: Exception from payload validation or the Canon workflow facade.
    """

    from integrations.hermes.canon_hermes import workflow_facade

    facade_payload = _build_runtime_facade_payload(
        workflow=workflow,
        inputs=inputs,
        gateway_source=gateway_source,
        gateway_root=gateway_root,
        run_id=run_id,
        request_id=request_id,
        target=target,
        request_authority=request_authority,
    )
    facade_stores = _build_runtime_facade_stores(
        workflow=workflow,
        gateway_root=gateway_root,
        request_authority=(
            dict(request_authority)
            if request_authority is not None
            else {"inputs": facade_payload["inputs"]}
        ),
    )
    review_sender = _build_gateway_review_sender_bridge(send_review_prompt)
    if review_sender is not None:
        facade_stores["review_sender"] = review_sender
    return await asyncio.to_thread(workflow_facade.startWorkflow, facade_payload, facade_stores)


def _run_payload_has_review_delivery_evidence(payload: dict[str, Any]) -> bool:
    """Return True when awaiting-review status includes durable checkpoint and delivery evidence.

    pre: payload is one Canon workflow facade result object.
    post: returns True only when both checkpoint and delivery carry non-empty object values.
    raises: none.
    """

    checkpoint = payload.get("checkpoint")
    delivery = payload.get("delivery")
    return isinstance(checkpoint, dict) and bool(checkpoint) and isinstance(delivery, dict) and bool(delivery)


def _canon_run_response_ok(payload: dict[str, Any]) -> bool:
    """Classify one Canon workflow-facade result as acceptable or fail-closed.

    pre: payload is the dict returned by `workflow_facade.startWorkflow(...)`.
    post: returns False for blocked/failed/cancelled/invalid statuses at either top level or nested
          result level, preventing false-green operator responses.
    post: `awaiting-human-review`/pending wait statuses require checkpoint and delivery evidence.
    raises: none.
    """

    statuses: list[str] = []
    top_status = str(payload.get("status") or "").strip().lower()
    if top_status:
        statuses.append(top_status)
    result = payload.get("result")
    if isinstance(result, dict):
        nested_status = str(result.get("status") or "").strip().lower()
        if nested_status:
            statuses.append(nested_status)
    if not statuses:
        return False
    for status in statuses:
        if status.startswith("blocked") or status in {"failed", "error", "cancelled", "invalid"}:
            return False
    accepted = any(
        status in {"completed", "success", "paused", "awaiting-human-review", "waiting", "pending-human-review"}
        for status in statuses
    )
    if not accepted:
        return False
    if any(status in {"awaiting-human-review", "waiting", "pending-human-review"} for status in statuses):
        return _run_payload_has_review_delivery_evidence(payload)
    return True


def _canon_run_reason(payload: dict[str, Any]) -> str | None:
    """Return the most useful human-readable reason from one Canon workflow-facade payload.

    pre: payload is the dict returned by `workflow_facade.startWorkflow(...)`.
    post: returns the first non-empty reason/message/error/status string from nested result or top-level payload.
    raises: none.
    """

    candidates: list[dict[str, Any]] = []
    result = payload.get("result")
    if isinstance(result, dict):
        candidates.append(result)
    candidates.append(payload)
    for candidate in candidates:
        for key in ("reason", "message", "error", "status"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _format_canon_run_response(*, workflow: str, payload: dict[str, Any]) -> str:
    """Render one honest operator-visible `/canon run` result from workflow-facade truth.

    pre: payload came from `workflow_facade.startWorkflow(...)`.
    post: success/accepted-wait statuses report the run id and durable status; blocked/failed states
          are returned as explicit fail-closed messages with the best available reason.
    raises: none.
    """

    run_id = str(payload.get("runId") or "").strip() or "<unknown>"
    status = str(payload.get("status") or "").strip() or "unknown"
    reason = _canon_run_reason(payload)
    if _canon_run_response_ok(payload):
        return f"`/canon run` live run `{run_id}` status `{status}` for workflow `{workflow}`."
    suffix = f": {reason}" if reason else ""
    return f"`/canon run` failed closed for run `{run_id}` status `{status}`{suffix}"


async def _run_gateway_canon_cli_command(
    *,
    workflow: str,
    inputs: Mapping[str, Any],
    gateway_source: Mapping[str, Any],
    gateway_root: Path,
    send_review_prompt: Callable[..., Awaitable[dict[str, Any]]] | None,
) -> str:
    """Execute one live `/canon run` through the public workflow facade seam.

    pre: workflow is a non-empty Canon workflow selector.
    pre: inputs is a JSON-object workflow input mapping.
    pre: gateway_source/gateway_root identify the live current-gateway origin and durable root.
    post: returns an honest operator-visible status string.
    raises: any exception surfaced by the workflow facade.
    """

    payload = await execute_gateway_workflow_facade_run(
        workflow=workflow,
        inputs=inputs,
        gateway_source=gateway_source,
        gateway_root=gateway_root,
        send_review_prompt=send_review_prompt,
    )
    return _format_canon_run_response(workflow=workflow, payload=payload)


def create_gateway_phase_backend_client() -> "_GatewayHermesPhaseBackendClient":
    """Create the concrete gateway-owned Canon phase backend client.

    pre: caller is a live current-gateway launch or callback-resume path that already has
         Canon-minted phase execution authority.
    post: returns a client implementing start_scoped_session; no fake/test host is created.
    raises: none.
    """

    return _GatewayHermesPhaseBackendClient()


class _GatewayHermesPhaseBackendClient:
    """Concrete gateway-owned Canon phase backend using the configured Hermes model.

    pre: used only from the live gateway `/canon run` path after Canon has minted a
         phase-execution envelope and resolved model-route authority.
    post: start_scoped_session returns a session that performs a real Hermes model call,
          parses strict JSON, and writes declared handoff refs into the Canon artifact store.
    raises: none here; scoped session raises fail-closed errors during phase execution.
    """

    def __init__(self) -> None:
        self._artifacts_dir: Path | None = None

    def bind_artifacts_dir(self, artifacts_dir: Any) -> None:
        """Bind the Canon artifact directory selected by current-gateway config.

        pre: artifacts_dir may be any path-like value or None.
        post: a valid path is stored for later artifact writes; invalid/None leaves writes disabled.
        raises: none.
        """

        if artifacts_dir is None:
            return
        self._artifacts_dir = Path(artifacts_dir)

    def start_scoped_session(self, envelope_projection: dict[str, Any]) -> "_GatewayHermesScopedPhaseSession":
        """Return one scoped phase session bound to Canon envelope projection.

        pre: envelope_projection is supplied by Canon's phase_backend projection seam.
        post: returned session keeps only the projection and configured artifact directory.
        raises: ValueError when the projection is malformed.
        """

        if not isinstance(envelope_projection, dict):
            raise ValueError("Canon phase backend projection must be an object")
        return _GatewayHermesScopedPhaseSession(
            envelope_projection=envelope_projection,
            artifacts_dir=self._artifacts_dir,
        )


class _GatewayHermesScopedPhaseSession:
    """Run one Canon phase as a bounded Hermes session with explicit toolset authority.

    pre: envelope_projection is Canon-minted identity/scope authority for one phase.
    post: run_phase returns a backend payload with the parsed JSON object when available, otherwise
          returns the raw model response string so Canon validation can classify schema mismatches.
    raises: RuntimeError when artifact persistence or backend authority checks fail.
    """

    def __init__(self, *, envelope_projection: dict[str, Any], artifacts_dir: Path | None) -> None:
        self._projection = dict(envelope_projection)
        self._artifacts_dir = artifacts_dir

    def run_phase(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Execute one live model call and return Canon phase backend output.

        pre: envelope carries objective, inputs, outputSchema, and Canon identity fields.
        post: returns {"status":"succeeded", "output": <dict|raw-string>} while preserving
              top-level backend continuation refs for Canon retry/validation flow.
        post: solution-modeling handoff refs are persisted only when output contains a modelPackage object.
        raises: RuntimeError when artifact writes or backend authority checks fail.
        """

        if not isinstance(envelope, dict):
            raise RuntimeError("Canon phase envelope must be an object")
        workdir = self._phase_workdir(envelope)
        prompt = self._build_phase_prompt(envelope, workdir=workdir)
        from hermes_cli.oneshot import _run_agent

        model_route = self._phase_model_route()
        model = model_route["model"] if model_route else None
        provider = model_route["provider"] if model_route else None
        hermes_profile_id = self._hermes_profile_id()
        toolsets = self._derive_authorized_toolsets(envelope, hermes_profile_id=hermes_profile_id)
        skills = self._derive_authorized_skills()
        use_config_toolsets = toolsets is None and hermes_profile_id is not None
        if self._phase_tool_use_disabled():
            toolsets = []
            use_config_toolsets = False
        response = _run_agent(
            prompt,
            model=model,
            provider=provider,
            toolsets=toolsets,
            use_config_toolsets=use_config_toolsets,
            profile=hermes_profile_id,
            skills=skills,
            workdir=workdir,
        )
        output = self._parse_phase_output_or_raw(response)
        artifact_refs = self._persist_solution_modeling_handoff(output, envelope=envelope)
        result: dict[str, Any] = {"status": "succeeded", "output": output}
        if artifact_refs:
            result["artifactRefs"] = artifact_refs
        agent_session_ref = self._agent_session_ref(envelope)
        result["agentSessionRef"] = agent_session_ref
        result["agentCheckpointRef"] = self._agent_checkpoint_ref(envelope)
        result["eventCursorRefs"] = self._event_cursor_refs(envelope, agent_session_ref=agent_session_ref)
        return result

    def _parse_phase_output_or_raw(self, response_text: Any) -> dict[str, Any] | str:
        """Return a parsed object result when possible, else preserve the raw model payload.

        pre: response_text is the raw final response from Hermes oneshot execution.
        post: returns the decoded object when the response contains one JSON object.
        post: returns the original string payload when the response is non-JSON or decodes to a non-object.
        raises: none.
        """

        raw_text = str(response_text or "")
        try:
            return _extract_json_object(raw_text)
        except RuntimeError:
            return raw_text

    def _phase_model_route(self) -> dict[str, str] | None:
        """Return the explicit Canon provider/model override for this phase.

        pre: self._projection may carry modelRoute minted by Canon routing.
        post: returns None when no route override is present; otherwise returns a complete provider/model pair.
        raises: RuntimeError when modelRoute is malformed or only partially specifies provider/model.
        """

        raw = self._projection.get("modelRoute")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise RuntimeError("Canon phase backend projection modelRoute must be an object")
        provider = str(raw.get("provider") or "").strip()
        model = str(raw.get("model") or "").strip()
        if not provider and not model:
            return None
        if not provider or not model:
            raise RuntimeError("Canon phase backend projection modelRoute must include both provider and model")
        return {"provider": provider, "model": model}

    def _phase_tool_use_disabled(self) -> bool:
        """Return whether Canon explicitly disabled tool use for this phase route.

        pre: self._projection may carry a Canon-minted modelRoute object.
        post: returns True only when modelRoute.toolUse is explicitly False.
        raises: RuntimeError when modelRoute is present but not an object.
        """

        raw = self._projection.get("modelRoute")
        if raw is None:
            return False
        if not isinstance(raw, dict):
            raise RuntimeError("Canon phase backend projection modelRoute must be an object")
        return raw.get("toolUse") is False

    def _hermes_profile_id(self) -> str | None:
        """Return the optional Hermes execution profile selected for this Canon phase.

        pre: self._projection is the private Canon->Hermes scoped projection.
        post: returns a stripped profile id when configured; otherwise None.
        raises: RuntimeError when the projection carries a malformed blank/non-string id.
        """

        raw = self._projection.get("hermesProfileId")
        if raw is None:
            return None
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError("Canon phase backend projection hermesProfileId must be a non-empty string")
        return raw.strip()

    def _derive_authorized_toolsets(self, envelope: dict[str, Any], *, hermes_profile_id: str | None) -> list[str] | None:
        """Resolve explicit toolset overrides or defer to the selected Hermes profile.

        pre: envelope is the active Canon phase envelope and self._projection is Canon-minted authority.
        post: returns a normalized ordered toolset list when Canon sent explicit allowedScopes.toolsetRefs.
        post: returns None for profile-owned tool defaults when hermes_profile_id is configured.
        raises: RuntimeError when a tool-requiring phase has neither explicit toolsets nor a Hermes profile.
        """

        allowed_scopes = self._projection.get("allowedScopes")
        refs = allowed_scopes.get("toolsetRefs") if isinstance(allowed_scopes, dict) else None
        authorized = self._normalize_refs(refs, authority_name="allowedScopes.toolsetRefs")
        if authorized:
            return authorized
        if hermes_profile_id is not None:
            return None
        if self._phase_requires_tools(envelope):
            raise RuntimeError(
                "Canon phase backend projection is missing required allowedScopes.toolsetRefs authority "
                "or hermesProfileId for a tool-requiring phase"
            )
        return None

    def _derive_authorized_skills(self) -> list[str] | None:
        """Resolve explicit skill preload overrides from Canon allowed scopes.

        pre: self._projection may carry allowedScopes.skillRefs minted by Canon.
        post: returns normalized skill ids for explicit preload overrides, or None to use profile defaults only.
        """

        allowed_scopes = self._projection.get("allowedScopes")
        refs = allowed_scopes.get("skillRefs") if isinstance(allowed_scopes, dict) else None
        return self._normalize_refs(refs, authority_name="allowedScopes.skillRefs")

    def _phase_requires_tools(self, envelope: dict[str, Any]) -> bool:
        """Return whether this phase contract explicitly requires tool access.

        pre: envelope is a Canon phase envelope object.
        post: returns True when phase inputs request mandatory skills; otherwise False.
        raises: none.
        """

        inputs = envelope.get("inputs") if isinstance(envelope.get("inputs"), dict) else {}
        mandatory_skills = inputs.get("mandatorySkills")
        return isinstance(mandatory_skills, list) and any(isinstance(item, str) and item.strip() for item in mandatory_skills)

    def _normalize_refs(self, refs: Any, *, authority_name: str) -> list[str] | None:
        """Normalize projection refs to bounded unique names.

        pre: refs is a potential Canon projection list.
        post: returns ordered unique non-empty strings when refs is list-like; otherwise None.
        raises: RuntimeError when refs is present but malformed.
        """

        if refs is None:
            return None
        if not isinstance(refs, list):
            raise RuntimeError(f"Canon phase backend projection {authority_name} must be a list")
        normalized: list[str] = []
        for entry in refs:
            value = str(entry).strip() if isinstance(entry, str) else ""
            if not value:
                continue
            if value not in normalized:
                normalized.append(value)
        return normalized or None

    def _agent_checkpoint_ref(self, envelope: dict[str, Any]) -> str:
        """Build an opaque bounded checkpoint ref for Canon backend-linked authority.

        pre: envelope may carry runId and phaseId strings.
        post: returns a deterministic non-empty bounded checkpoint ref.
        raises: none.
        """

        run_id = str(envelope.get("runId") or "run").strip() or "run"
        phase_id = str(envelope.get("phaseId") or "phase").strip() or "phase"
        return self._bounded_ref(f"hermes-current-gateway:checkpoint:{run_id}:{phase_id}:1")

    def _event_cursor_refs(self, envelope: dict[str, Any], *, agent_session_ref: str) -> list[str]:
        """Build opaque event cursor refs linked to the scoped phase run.

        pre: agent_session_ref is the canonical session ref emitted for this phase result.
        post: returns a non-empty list of bounded opaque refs with no transcript/prompt/tool output.
        raises: none.
        """

        run_id = str(envelope.get("runId") or "run").strip() or "run"
        phase_id = str(envelope.get("phaseId") or "phase").strip() or "phase"
        cursor_ref = self._bounded_ref(f"{agent_session_ref}:events:{run_id}:{phase_id}:cursor:1")
        return [cursor_ref]

    def _bounded_ref(self, value: str, *, max_length: int = 128) -> str:
        """Bound opaque refs to a stable non-empty string envelope.

        pre: value is a ref candidate string.
        post: returns a stripped non-empty string capped at max_length bytes/characters.
        raises: none.
        """

        text = str(value or "").strip()
        if not text:
            return "hermes-current-gateway:ref"
        return text[:max_length]

    def _phase_workdir(self, envelope: dict[str, Any]) -> str:
        """Return the only Canon-authorized working directory for this phase.

        pre: self._projection is the private Canon->Hermes scoped projection for this phase.
        post: returns the resolved absolute executionContext.workingDirectory authority exactly when the
              projection supplies one safe existing directory.
        raises: RuntimeError when executionContext or executionContext.workingDirectory is missing,
                blank, relative, nonexistent, or not a directory.
        """

        execution_context = self._projection.get("executionContext")
        if not isinstance(execution_context, dict):
            raise RuntimeError(
                "Canon phase backend projection is missing required executionContext.workingDirectory authority"
            )
        raw = execution_context.get("workingDirectory")
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError(
                "Canon phase backend projection executionContext.workingDirectory must be an absolute existing directory"
            )
        candidate = Path(raw.strip()).expanduser()
        if not candidate.is_absolute() or not candidate.is_dir():
            raise RuntimeError(
                "Canon phase backend projection executionContext.workingDirectory must be an absolute existing directory"
            )
        return str(candidate.resolve())

    def _build_phase_prompt(self, envelope: dict[str, Any], *, workdir: str | None = None) -> str:
        """Build a strict JSON-only prompt from Canon envelope authority.

        pre: envelope carries objective, inputs, outputSchema, runId, phaseId.
        post: prompt asks for exactly one JSON object matching the provided schema and bounded refs.
        raises: RuntimeError when required envelope fields are malformed.
        """

        objective = envelope.get("objective")
        inputs = envelope.get("inputs")
        output_schema = envelope.get("outputSchema", {}).get("schema") if isinstance(envelope.get("outputSchema"), dict) else None
        run_id = str(envelope.get("runId") or "").strip()
        phase_id = str(envelope.get("phaseId") or "").strip()
        if not isinstance(objective, str) or not objective.strip():
            raise RuntimeError("Canon phase envelope objective is required")
        if not isinstance(inputs, dict):
            raise RuntimeError("Canon phase envelope inputs must be an object")
        if not isinstance(output_schema, dict):
            raise RuntimeError("Canon phase envelope output schema must be an object")
        ref_prefix = f"current-gateway/{run_id}"
        solution_modeling_guidance = ""
        if isinstance(output_schema.get("properties"), dict) and "modelPackage" in output_schema.get("properties", {}):
            solution_modeling_guidance = (
                "\nSolution-modeling meaning:\n"
                "- Convert the customer request into a freeze-ready model package, not a casual recommendation.\n"
                "- Make product decisions yourself when the request is under-specified; record assumptions, open questions, and default decisions.\n"
                "- Include scope, non-goals, domain entities, requirements, acceptance criteria, risks, and weighted alternatives.\n"
                "- approvalSummary must be concise but complete enough for a human to approve or revise in chat.\n"
                "- approvalSummary.modelContracts must state the main product contracts being approved.\n"
                "- approvalSummary.decisionRationales must explain why each accepted decision was chosen; keep full scoring in weighedAlternatives.\n"
                "- Approval means the model and its decisions are frozen for downstream spec generation.\n"
            )
        elif phase_id == "finalize_solution":
            solution_modeling_guidance = (
                "\nFinalize meaning:\n"
                "- Preserve the approved draft's specPackageHandoff refs exactly.\n"
                "- Return the refs object and freeze metadata required by the schema.\n"
                "- Mark status as frozen and list every frozen ref; do not invent a new model.\n"
            )
        workdir_guidance = ""
        if workdir:
            workdir_guidance = (
                f"Execution working directory: {workdir}\n"
                "All relative read_file/search_files/write_file/patch/terminal paths for this phase MUST "
                "resolve under that directory unless a phase input explicitly names an absolute path.\n"
            )
        return (
            "You are executing one Canon current-gateway phase.\n"
            "Return ONLY one valid JSON object. No markdown, no prose, no code fences.\n"
            "The JSON object MUST validate against the provided JSON Schema.\n"
            "Before the final JSON answer, use skill_view to load every skill named in inputs.mandatorySkills; apply those skill contracts to the output.\n"
            "Use read_file/search_files/terminal only when the phase inputs name concrete project refs or repo roots that require reconnaissance.\n"
            "Do not use placeholders; fill fields with concise task-specific content.\n"
            "If the schema asks for handoff refs, use these exact refs where applicable:\n"
            f"- modelPackageRef/specPackageRef: {ref_prefix}/model-package\n"
            f"- architectureRef: {ref_prefix}/architecture\n"
            f"- specRef: {ref_prefix}/spec\n"
            f"- workingLogRef: {ref_prefix}/working-log\n"
            f"- chatSummaryRef: {ref_prefix}/chat-summary\n"
            f"{workdir_guidance}"
            f"{solution_modeling_guidance}\n"
            f"Canon identity: runId={run_id}, phaseId={phase_id}.\n\n"
            "Phase objective:\n"
            f"{objective}\n\n"
            "Inputs JSON:\n"
            f"{json.dumps(inputs, ensure_ascii=False, sort_keys=True, indent=2)}\n\n"
            "Output JSON Schema:\n"
            f"{json.dumps(output_schema, ensure_ascii=False, sort_keys=True, indent=2)}\n"
        )

    def _persist_solution_modeling_handoff(self, output: Any, *, envelope: dict[str, Any]) -> list[str]:
        """Persist distinct solution-modeling handoff artifacts for human review and downstream specs.

        pre: output is the parsed model JSON object or a raw non-object model response string.
        pre: only object outputs may contain modelPackage.specPackageHandoff refs.
        post: every declared handoff ref is readable from the configured Canon artifact store; each
              ref contains an artifact-specific human-readable markdown projection instead of a clone
              of the same modelPackage payload.
        raises: RuntimeError when an artifact ref is declared but cannot be persisted.
        """

        model_package = output.get("modelPackage") if isinstance(output, dict) else None
        handoff = model_package.get("specPackageHandoff") if isinstance(model_package, dict) else None
        if not isinstance(model_package, dict) or not isinstance(handoff, dict):
            return []
        refs_by_kind = {
            "model-package": handoff.get("modelPackageRef"),
            "architecture": handoff.get("architectureRef"),
            "spec": handoff.get("specRef"),
            "working-log": handoff.get("workingLogRef"),
            "chat-summary": handoff.get("chatSummaryRef"),
        }
        refs = [str(value) for value in refs_by_kind.values() if isinstance(value, str) and value]
        if not refs:
            return []
        if self._artifacts_dir is None:
            raise RuntimeError("Canon artifact directory is required for solution-modeling handoff refs")
        from canon.durability import LocalArtifactBackend

        backend = LocalArtifactBackend(self._artifacts_dir)
        metadata = {
            "runId": str(envelope.get("runId") or ""),
            "threadId": str(envelope.get("threadId") or ""),
            "workflowRef": str(envelope.get("workflowRef") or ""),
            "phaseId": str(envelope.get("phaseId") or ""),
            "writer": "tools.canon_workflow_command._GatewayHermesScopedPhaseSession",
        }
        written: list[str] = []
        for artifact_kind, ref_value in refs_by_kind.items():
            if not isinstance(ref_value, str) or not ref_value:
                continue
            payload = self._build_solution_modeling_artifact_payload(
                artifact_kind=artifact_kind,
                ref=ref_value,
                model_package=model_package,
            )
            backend.write(ref_value, payload, {**metadata, "artifactKind": artifact_kind})
            written.append(ref_value)
        return sorted(set(written))

    def _build_solution_modeling_artifact_payload(
        self,
        *,
        artifact_kind: str,
        ref: str,
        model_package: dict[str, Any],
    ) -> dict[str, Any]:
        """Build one artifact-specific projection from an approved model package.

        pre: artifact_kind is one declared solution-modeling handoff kind and model_package is the
             schema-shaped package returned by the model_solution phase.
        post: returns a JSON payload with artifactKind/ref/contentMarkdown; only model-package embeds
              the full structured model while other refs expose focused spec/architecture/log/summary
              documents derived from the same approved source of truth.
        raises: RuntimeError when artifact_kind is unknown.
        """

        if artifact_kind == "model-package":
            markdown = self._render_model_package_markdown(model_package)
            return {"artifactKind": artifact_kind, "ref": ref, "modelPackage": model_package, "contentMarkdown": markdown}
        if artifact_kind == "architecture":
            markdown = self._render_architecture_markdown(model_package)
        elif artifact_kind == "spec":
            markdown = self._render_spec_markdown(model_package)
        elif artifact_kind == "working-log":
            markdown = self._render_working_log_markdown(model_package)
        elif artifact_kind == "chat-summary":
            markdown = self._render_chat_summary_markdown(model_package)
        else:
            raise RuntimeError(f"unsupported solution-modeling artifact kind: {artifact_kind}")
        return {"artifactKind": artifact_kind, "ref": ref, "contentMarkdown": markdown}

    def _render_model_package_markdown(self, model_package: dict[str, Any]) -> str:
        """Render the complete frozen-review model package as a human-readable document."""

        summary = self._mapping(model_package.get("approvalSummary"))
        lines = [
            f"# {self._text(summary.get('title'), 'Solution Model Package')}",
            "",
            "## Summary",
            self._text(summary.get("summary"), ""),
            "",
            "## Recommended Approach",
            self._text(summary.get("recommendedApproach"), ""),
            "",
            "## Problem Statement",
            self._text(model_package.get("problemStatement"), ""),
            "",
            "## Scope",
            self._bullets("Goals", model_package.get("goals")),
            self._bullets("Non-goals", model_package.get("nonGoals")),
            self._bullets("Constraints", model_package.get("constraints")),
            "",
            "## Contracts Being Approved",
            self._text(summary.get("modelContracts"), ""),
            "",
            "## Decision Rationale",
            self._text(summary.get("decisionRationales"), ""),
            "",
            "## Domain Model",
            self._domain_model(model_package.get("domainModel")),
            "",
            "## Requirements",
            self._requirements(model_package.get("requirements")),
            "",
            "## Acceptance Criteria",
            self._requirements(model_package.get("acceptanceCriteria")),
            "",
            "## Weighted Alternatives",
            self._weighted_alternatives(model_package.get("weighedAlternatives")),
            "",
            "## Risks",
            self._risks(model_package.get("risks")),
            "",
            "## Open Questions",
            self._open_questions(model_package.get("openQuestions")),
            "",
            "## Freeze",
            json.dumps(model_package.get("freeze", {}), ensure_ascii=False, indent=2, sort_keys=True),
        ]
        return "\n".join(line for line in lines if line is not None).strip() + "\n"

    def _render_architecture_markdown(self, model_package: dict[str, Any]) -> str:
        """Render architecture-relevant decisions and domain boundaries from the model package."""

        summary = self._mapping(model_package.get("approvalSummary"))
        return (
            f"# Architecture — {self._text(summary.get('title'), 'Solution Model')}\n\n"
            f"## Approach\n{self._text(summary.get('recommendedApproach'), '')}\n\n"
            f"## Domain Model\n{self._domain_model(model_package.get('domainModel'))}\n\n"
            f"## Constraints\n{self._bullets('', model_package.get('constraints'))}\n\n"
            f"## Key Decisions\n{self._bullets('', model_package.get('implicitDecisions'))}\n"
        )

    def _render_spec_markdown(self, model_package: dict[str, Any]) -> str:
        """Render implementation-facing requirements and acceptance criteria from the model package."""

        return (
            "# Specification\n\n"
            f"## Problem\n{self._text(model_package.get('problemStatement'), '')}\n\n"
            f"## Users\n{self._users(model_package.get('users'))}\n\n"
            f"## Use Cases\n{self._use_cases(model_package.get('useCases'))}\n\n"
            f"## Requirements\n{self._requirements(model_package.get('requirements'))}\n\n"
            f"## Acceptance Criteria\n{self._requirements(model_package.get('acceptanceCriteria'))}\n"
        )

    def _render_working_log_markdown(self, model_package: dict[str, Any]) -> str:
        """Render decision, risk, and question traceability for the frozen package."""

        return (
            "# Working Log\n\n"
            f"## Explicit Decisions\n{self._bullets('', model_package.get('implicitDecisions'))}\n\n"
            f"## Decision Matrix\n{self._decision_matrix(model_package.get('decisionMatrix'))}\n\n"
            f"## Weighted Alternatives\n{self._weighted_alternatives(model_package.get('weighedAlternatives'))}\n\n"
            f"## Risks\n{self._risks(model_package.get('risks'))}\n\n"
            f"## Open Questions\n{self._open_questions(model_package.get('openQuestions'))}\n"
        )

    def _render_chat_summary_markdown(self, model_package: dict[str, Any]) -> str:
        """Render concise operator-facing summary content from the approved model package."""

        summary = self._mapping(model_package.get("approvalSummary"))
        return (
            f"# Review Summary — {self._text(summary.get('title'), 'Solution Model')}\n\n"
            f"{self._text(summary.get('summary'), '')}\n\n"
            f"## Scope\n{self._text(summary.get('scopeSummary'), '')}\n\n"
            f"## Contracts Being Approved\n{self._text(summary.get('modelContracts'), '')}\n\n"
            f"## Key Decisions\n{self._text(summary.get('keyDecisions'), '')}\n\n"
            f"## Decision Rationale\n{self._text(summary.get('decisionRationales'), '')}\n\n"
            f"## Weighted Alternatives\n{self._text(summary.get('weightedAlternatives'), '')}\n\n"
            f"## Risks\n{self._text(summary.get('keyRisks'), '')}\n\n"
            f"## Freeze Notice\n{self._text(summary.get('freezeNotice'), '')}\n"
        )

    def _mapping(self, value: Any) -> dict[str, Any]:
        """Return a mapping value or an empty mapping for renderer helpers."""

        return value if isinstance(value, dict) else {}

    def _text(self, value: Any, default: str) -> str:
        """Return a stripped string for markdown rendering."""

        return value.strip() if isinstance(value, str) and value.strip() else default

    def _bullets(self, title: str, values: Any) -> str:
        """Render a list of strings as markdown bullets with an optional title."""

        items = values if isinstance(values, list) else []
        body = "\n".join(f"- {self._text(item, '')}" for item in items if self._text(item, ""))
        if title:
            return f"### {title}\n{body or '- Not specified'}"
        return body or "- Not specified"

    def _users(self, values: Any) -> str:
        """Render model users/personas as markdown bullets."""

        items = values if isinstance(values, list) else []
        lines = []
        for item in items:
            data = self._mapping(item)
            lines.append(f"- {self._text(data.get('name'), 'User')}: {self._text(data.get('need'), 'Need not specified')}")
        return "\n".join(lines) or "- Not specified"

    def _use_cases(self, values: Any) -> str:
        """Render use cases with steps and success criteria."""

        items = values if isinstance(values, list) else []
        lines = []
        for item in items:
            data = self._mapping(item)
            lines.append(f"- {self._text(data.get('name'), 'Use case')}: steps={self._join(data.get('steps'))}; success={self._join(data.get('successCriteria'))}")
        return "\n".join(lines) or "- Not specified"

    def _domain_model(self, values: Any) -> str:
        """Render domain entities and fields."""

        items = values if isinstance(values, list) else []
        lines = []
        for item in items:
            data = self._mapping(item)
            lines.append(f"- {self._text(data.get('entity'), 'Entity')}: {self._join(data.get('fields'))}")
        return "\n".join(lines) or "- Not specified"

    def _requirements(self, values: Any) -> str:
        """Render id/text/priority-style arrays used by requirements and acceptance criteria."""

        items = values if isinstance(values, list) else []
        lines = []
        for item in items:
            data = self._mapping(item)
            suffix = f" [{data.get('priority')}]" if data.get("priority") else ""
            lines.append(f"- {self._text(data.get('id'), 'ITEM')}{suffix}: {self._text(data.get('text'), '')}")
        return "\n".join(lines) or "- Not specified"

    def _decision_matrix(self, values: Any) -> str:
        """Render concise decision-matrix rows."""

        items = values if isinstance(values, list) else []
        lines = []
        for item in items:
            data = self._mapping(item)
            lines.append(
                f"- {self._text(data.get('dimension'), 'Decision')}: selected {self._text(data.get('selected'), '')}; "
                f"options={self._join(data.get('options'))}; rationale={self._text(data.get('rationale'), '')}"
            )
        return "\n".join(lines) or "- Not specified"

    def _weighted_alternatives(self, values: Any) -> str:
        """Render weighted alternatives with criteria, scores, and tradeoffs."""

        items = values if isinstance(values, list) else []
        lines = []
        for item in items:
            data = self._mapping(item)
            lines.append(f"- Decision: {self._text(data.get('decision'), '')}; selected: {self._text(data.get('selected'), '')}")
            criteria = [f"{self._text(self._mapping(c).get('name'), '')}={self._mapping(c).get('weight')}" for c in data.get("criteria", []) if isinstance(c, dict)]
            if criteria:
                lines.append(f"  Criteria: {', '.join(criteria)}")
            for option in data.get("options", []) if isinstance(data.get("options"), list) else []:
                opt = self._mapping(option)
                lines.append(f"  - {self._text(opt.get('option'), 'Option')}: total={opt.get('totalScore')}; {self._text(opt.get('tradeoffs'), '')}")
            if self._text(data.get("rationale"), ""):
                lines.append(f"  Rationale: {self._text(data.get('rationale'), '')}")
        return "\n".join(lines) or "- Not specified"

    def _risks(self, values: Any) -> str:
        """Render risks with impact and mitigation."""

        items = values if isinstance(values, list) else []
        lines = []
        for item in items:
            data = self._mapping(item)
            lines.append(f"- {self._text(data.get('risk'), 'Risk')}: impact={self._text(data.get('impact'), '')}; mitigation={self._text(data.get('mitigation'), '')}")
        return "\n".join(lines) or "- Not specified"

    def _open_questions(self, values: Any) -> str:
        """Render open questions with default decisions and impact."""

        items = values if isinstance(values, list) else []
        lines = []
        for item in items:
            data = self._mapping(item)
            lines.append(f"- {self._text(data.get('question'), 'Question')}: default={self._text(data.get('defaultDecision'), '')}; impact={self._text(data.get('impact'), '')}")
        return "\n".join(lines) or "- None"

    def _join(self, values: Any) -> str:
        """Join a string list for one-line markdown renderer output."""

        items = values if isinstance(values, list) else []
        return ", ".join(self._text(item, "") for item in items if self._text(item, "")) or "not specified"

    def _agent_session_ref(self, envelope: dict[str, Any]) -> str:
        """Return a bounded non-secret session evidence reference for this phase call."""

        run_id = str(envelope.get("runId") or "run")
        phase_id = str(envelope.get("phaseId") or "phase")
        attempt = str(envelope.get("phaseAttempt") or "1")
        return f"hermes-current-gateway:{run_id}:{phase_id}:{attempt}"


def _extract_json_object(response_text: str) -> dict[str, Any]:
    """Parse one JSON object from a model response without accepting placeholders.

    pre: response_text is the raw final response from Hermes model execution.
    post: returns the decoded object from raw JSON or a single fenced JSON block.
    raises: RuntimeError when no JSON object can be decoded.
    """

    text = str(response_text or "").strip()
    candidates = [text]
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        candidates.insert(0, fence_match.group(1).strip())
    object_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if object_match:
        candidates.append(object_match.group(0).strip())
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return decoded
    raise RuntimeError("Hermes phase backend model response did not contain a valid JSON object")


def _is_generic_review_text(task_text: str) -> bool:
    """Return True when a `/canon run` task looks like placeholder operator text.

    pre: task_text is any user-provided workflow task string.
    post: rejects blank/default/example placeholder text while preserving concrete operator prompts.
    raises: none.
    """

    normalized = str(task_text or "").strip().lower()
    if not normalized:
        return True
    generic_values = {
        "task",
        "todo",
        "tbd",
        "placeholder",
        "default",
        "example",
        "test",
        "review",
        "review text",
        "operator task",
        "explicit operator task",
    }
    return normalized in generic_values


def _handle_latest(*, source: Any) -> str:
    """Resolve `/canon latest` from Canon durable store with origin scope.

    pre: source identifies the gateway origin through platform + chat_id.
    post: returns one status line with run id and production/non-production classification.
    raises: none (fail-closed text on lookup/import/authority errors).
    """

    try:
        summary = _load_latest_summary(source=source)
    except Exception as exc:
        return f"`/canon latest` failed closed: {exc}"
    return _format_operator_summary(prefix="/canon latest", summary=summary)


def _handle_list(*, source: Any) -> str:
    """Resolve `/canon list` from Canon durable store with origin scope.

    pre: source identifies gateway origin through platform + chat_id.
    post: returns Russian markdown list of visible durable runs with count/status.
    raises: none (fail-closed text on lookup/import/authority errors).
    """

    try:
        listing = _load_list_summary(source=source)
    except Exception as exc:
        return f"`/canon list` failed closed: {exc}"
    return _render_list_surface(listing=listing)


def _handle_observability_subcommand(
    *,
    subcommand: str,
    run_id: str,
    source: Any,
    after_sequence: int | None = None,
    limit: int | None = None,
) -> str:
    """Handle one run-scoped observability subcommand.

    pre: subcommand belongs to list/full/timeline/artifacts/events/report/control and run_id is non-empty.
    post: returns Russian markdown without raw JSON and fail-closes when lookup fails.
    raises: none.
    """

    try:
        if subcommand == "events":
            events_summary = _load_stream_events_summary(
                run_id=run_id,
                source=source,
                after_sequence=after_sequence,
                limit=limit,
            )
            return _render_events_surface(
                run_id=run_id,
                events_summary=events_summary,
                after_sequence=after_sequence,
                limit=limit,
            )
        summary = _load_inspect_summary(run_id=run_id, source=source)
    except Exception as exc:
        return f"`/canon {subcommand} {run_id}` failed closed: {exc}"
    return _render_observability_surface(subcommand=subcommand, run_id=run_id, summary=summary)


def _parse_events_cursor_args(args: list[str]) -> tuple[int | None, int | None]:
    """Parse optional cursor arguments for `/canon events`.

    pre: args are tokens after `<run-id>`.
    post: returns optional non-negative cursor/limit integers.
    raises: ValueError when args are unsupported, duplicated, or malformed.
    """

    after_sequence: int | None = None
    limit: int | None = None
    index = 0
    while index < len(args):
        token = str(args[index]).strip()
        if token == "--after-sequence":
            if after_sequence is not None:
                raise ValueError("duplicate --after-sequence")
            if index + 1 >= len(args):
                raise ValueError("--after-sequence requires a value")
            after_sequence = _parse_non_negative_int(token="--after-sequence", value=args[index + 1])
            index += 2
            continue
        if token == "--limit":
            if limit is not None:
                raise ValueError("duplicate --limit")
            if index + 1 >= len(args):
                raise ValueError("--limit requires a value")
            limit = _parse_non_negative_int(token="--limit", value=args[index + 1])
            index += 2
            continue
        raise ValueError(f"unsupported events argument: {token}")
    return after_sequence, limit


def _parse_non_negative_int(*, token: str, value: str) -> int:
    """Parse one non-negative integer CLI argument value.

    pre: token is the argument name and value is the raw token value.
    post: returns parsed integer value >= 0.
    raises: ValueError when value is not a non-negative integer.
    """

    try:
        parsed = int(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{token} must be a non-negative integer") from exc
    if parsed < 0:
        raise ValueError(f"{token} must be a non-negative integer")
    return parsed


def _load_stream_events_summary(
    *,
    run_id: str,
    source: Any,
    after_sequence: int | None,
    limit: int | None,
) -> dict[str, Any]:
    """Load one cursor-window events payload from Canon streamWorkflowEvents facade.

    pre: run_id/source are valid operator scope identifiers.
    post: returns stream window payload with events/cursor metadata from Canon authority.
    raises: ImportError/ValueError/PermissionError from Canon facade or input projection.
    """

    from integrations.hermes.canon_hermes.workflow_facade import stream_workflow_events

    platform_raw = getattr(source, "platform", "")
    platform = getattr(platform_raw, "value", platform_raw)
    payload: dict[str, Any] = {
        "runId": run_id,
        "origin": _gateway_origin(source),
        "gatewaySource": {
            "platform": str(platform),
            "chat_id": str(getattr(source, "chat_id", "") or ""),
        },
    }
    thread_id = str(getattr(source, "thread_id", "") or "").strip()
    if thread_id:
        payload["gatewaySource"]["thread_id"] = thread_id
    if after_sequence is not None:
        payload["afterSequence"] = after_sequence
    if limit is not None:
        payload["limit"] = limit
    return stream_workflow_events(payload)


def _render_events_surface(
    *,
    run_id: str,
    events_summary: dict[str, Any],
    after_sequence: int | None,
    limit: int | None,
) -> str:
    """Render `/canon events` cursor stream window as bounded Russian markdown.

    pre: events_summary is streamWorkflowEvents payload from Canon facade.
    post: output includes cursor input/output metadata and bounded event window.
    raises: ValueError when stream payload is malformed.
    """

    if not isinstance(events_summary, dict):
        raise ValueError("streamWorkflowEvents payload must be a mapping")
    run_summary = events_summary.get("run")
    if not isinstance(run_summary, dict):
        raise ValueError("streamWorkflowEvents.run must be a mapping")
    events = events_summary.get("events")
    if not isinstance(events, list):
        raise ValueError("streamWorkflowEvents.events must be a list")
    next_cursor = events_summary.get("nextCursor")
    has_more = events_summary.get("hasMore")
    if has_more is not None and not isinstance(has_more, bool):
        raise ValueError("streamWorkflowEvents.hasMore must be a boolean")

    status = _to_redacted_value(run_summary.get("status") or "unknown", key="status").strip() or "unknown"
    label = "non-production" if _is_non_production_status(status) else "production"
    lines: list[str] = [
        "### Поверхность: События",
        f"- Команда: `/canon events {run_id}`",
        f"- Область запуска: `{run_id}`",
        f"- Статус: `{status}` ({label})",
        f"- Курсор запроса: afterSequence={after_sequence if after_sequence is not None else '-'}, limit={limit if limit is not None else '-'}",
        f"- Курсор ответа: nextCursor={_to_redacted_value(next_cursor, key='nextCursor')}, hasMore={_to_redacted_value(has_more, key='hasMore')}",
        "- События:",
    ]
    if not events:
        lines.append("  - <не опубликованно>")
        return "\n".join(lines)

    for event in events[:50]:
        if isinstance(event, Mapping):
            event_kind = _to_redacted_value(event.get("eventKind") or "unknown", key="eventKind")
            sequence = _to_redacted_value(event.get("sequence") if "sequence" in event else "-", key="sequence")
            node_id = _to_redacted_value(event.get("nodeId") if "nodeId" in event else "-", key="nodeId")
            lines.append(f"  - seq={sequence}; kind={event_kind}; node={node_id}")
            continue
        lines.append(f"  - {_to_redacted_value(event, key='events')}")
    return "\n".join(lines)



def _handle_inspect(*, run_id: str, source: Any) -> str:
    """Resolve `/canon inspect <run-id>` from Canon durable store with origin scope.

    pre: run_id is non-empty and source identifies gateway origin.
    post: returns one status line with run id and production/non-production classification.
    raises: none (fail-closed text on lookup/import/authority errors).
    """

    try:
        summary = _load_inspect_summary(run_id=run_id, source=source)
    except Exception as exc:
        return f"`/canon inspect {run_id}` failed closed: {exc}"
    return _format_operator_summary(prefix=f"/canon inspect {run_id}", summary=summary)


def _render_observability_surface(*, subcommand: str, run_id: str, summary: dict[str, Any]) -> str:
    """Render one localized observability surface in markdown without raw JSON payloads.

    pre: summary is a dictionary loaded from Canon durable inspect authority.
    post: returns a one-screen markdown block per surface with Russian labels and fail-safe placeholders.
    raises: ValueError when an unsupported surface is requested.
    """

    status = _to_redacted_value(summary.get("status", "unknown"), key="status")
    if not isinstance(status, str):
        status = str(status)
    status = status.strip() or "unknown"
    label = "non-production" if _is_non_production_status(status) else "production"
    surface_map = {
        "list": "Список",
        "full": "Полный отчет",
        "timeline": "Таймлайн",
        "artifacts": "Артефакты",
        "events": "События",
        "report": "Отчет",
        "control": "Контроль",
    }
    surface = surface_map.get(subcommand)
    if not surface:
        raise ValueError(f"unknown observability surface: {subcommand}")

    lines: list[str] = [
        f"### Поверхность: {surface}",
        f"- Команда: `/canon {subcommand} {run_id}`",
        f"- Область запуска: `{run_id}`",
        f"- Статус: `{status}` ({label})",
        "",
    ]

    if subcommand == "list":
        lines.append("- Доступные поверхности:")
        values = [
            "- list",
            "- full",
            "- timeline",
            "- artifacts",
            "- events",
            "- report",
            "- control",
        ]
        lines.extend(values)
        return "\n".join(lines)

    if subcommand == "timeline":
        lines.append("- Таймлайн:")
        timeline = summary.get("timeline")
        if isinstance(timeline, list) and timeline:
            for item in timeline:
                lines.append(f"  - {_to_redacted_value(item, key='timeline')}")
        else:
            lines.append("  - <не опубликованно>")
        return "\n".join(lines)

    if subcommand == "artifacts":
        lines.append("- Артефакты:")
        artifacts = _extract_observability_artifacts(summary.get("artifacts"))
        if artifacts:
            for artifact in artifacts:
                lines.append(f"  - {_to_redacted_value(artifact, key='artifacts')}")
        else:
            lines.append("  - <не опубликованно>")
        return "\n".join(lines)

    if subcommand == "events":
        lines.append("- События:")
        events = summary.get("events")
        if isinstance(events, list) and events:
            for event_line in events:
                lines.append(f"  - {_to_redacted_value(event_line, key='events')}")
        else:
            lines.append("  - <не опубликованно>")
        return "\n".join(lines)

    if subcommand == "report":
        lines.append("- Отчет:")
        # Canon observe authority uses `reportSummary`; keep `report` only as
        # legacy fallback when reportSummary is absent.
        report_payload = summary.get("reportSummary")
        report_key = "reportSummary"
        if report_payload is None:
            report_payload = summary.get("report")
            report_key = "report"
        if isinstance(report_payload, dict):
            rendered = _render_observability_mapping(report_payload)
            if rendered:
                lines.extend(rendered)
            else:
                lines.append("  - <не опубликованно>")
        elif report_payload is not None:
            lines.append(f"  - {_to_redacted_value(report_payload, key=report_key)}")
        else:
            lines.append("  - <не опубликованно>")
        return "\n".join(lines)

    if subcommand == "control":
        lines.append("- Контроль:")
        control = summary.get("control")
        if isinstance(control, dict):
            rendered = _render_observability_mapping(control)
            if rendered:
                lines.extend(rendered)
            else:
                lines.append("  - <не опубликованно>")
        elif control is not None:
            lines.append(f"  - {_to_redacted_value(control, key='control')}")
        else:
            lines.append("  - <не опубликованно>")
        return "\n".join(lines)

    # full output.
    full_markdown = _observe_to_markdown(summary)
    lines.append("- Полный отчет:")
    if full_markdown:
        lines.extend(f"  {line}" if line else "" for line in full_markdown.splitlines())
    else:
        lines.append("  - <не опубликованно>")
    return "\n".join(lines)


def _extract_observability_artifacts(raw_artifacts: Any) -> list[str]:
    """Build stable artifact name list from list-like artifact summaries."""

    if not isinstance(raw_artifacts, list):
        return []

    names: list[str] = []
    for artifact in raw_artifacts:
        if isinstance(artifact, str):
            if artifact.strip():
                names.append(artifact)
            continue
        if isinstance(artifact, dict):
            name = artifact.get("path") or artifact.get("fileName")
            if isinstance(name, str) and name.strip():
                names.append(name)
    return sorted(set(names))


def _render_observability_compact_mapping(payload: Mapping[str, Any]) -> str:
    """Flatten one mapping into compact operator text without raw brace syntax.

    pre: payload is a small operator-facing mapping from a durable summary payload.
    post: returns bounded `key=value` text suitable for inline markdown list values.
    raises: none.
    """

    pairs: list[str] = []
    for key, value in payload.items():
        key_text = str(key)
        if isinstance(value, Mapping):
            pairs.append(f"{key_text}=({_render_observability_compact_mapping(value)})")
            continue
        if isinstance(value, list):
            rendered_items = ", ".join(_render_observability_inline_value(item, key=key_text) for item in value[:5])
            pairs.append(f"{key_text}={rendered_items or '-'}")
            continue
        pairs.append(f"{key_text}={_render_observability_inline_value(value, key=key_text)}")
    return ", ".join(pairs) if pairs else "-"



def _render_observability_inline_value(value: Any, *, key: str) -> str:
    """Render one inline observability value without falling back to raw dict/list dumps.

    pre: value is any operator-facing scalar/list/mapping and key names the source field.
    post: mappings/lists become bounded brace-free text and null becomes '-'.
    raises: none.
    """

    if value is None:
        return "-"
    if isinstance(value, Mapping):
        return _render_observability_compact_mapping(value)
    if isinstance(value, list):
        rendered = ", ".join(_render_observability_inline_value(item, key=key) for item in value[:5])
        return rendered or "-"
    text = _to_redacted_value(value, key=key).strip()
    return text or "-"



def _render_observability_mapping(payload: Mapping[str, Any]) -> list[str]:
    """Render a durable mapping payload as markdown lines.

    pre: payload is an operator-facing dict object.
    post: list contains short, readable bullet rows.
    raises: none.
    """

    lines: list[str] = []
    for key, value in payload.items():
        key_text = str(key)
        if isinstance(value, dict):
            nested = _render_observability_mapping(value)
            lines.append(f"  - {key_text}:")
            lines.extend(f"    {line}" if line else "" for line in nested)
            continue
        if isinstance(value, list):
            rendered_items = ", ".join(_render_observability_inline_value(item, key=key_text) for item in value[:5])
            lines.append(f"  - {key_text}: {rendered_items}" if rendered_items else f"  - {key_text}: <не опубликованно>")
            continue
        lines.append(f"  - {key_text}: {_render_observability_inline_value(value, key=key_text)}")
    return lines


_SECRET_KEYS = {
    "token",
    "password",
    "api_key",
    "private_key",
    "secret",
    "prompt",
    "raw_prompt",
    "tool_stdout",
    "transcript",
    "raw_transcript",
    "secret",
}
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(token|password|api[_-]?key|private[_-]?key|secret|prompt|raw[_-]?prompt|tool[_-]?stdout|transcript)\s*[:=]\s*([^\n;,}]*)"
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(top[_-]?secret|secret[_-]?(token|value|status)?|password|api[_-]?key|private[_-]?key|bearer\s+[a-z0-9._-]+|sk-[a-z0-9_-]+)"
)


def _to_redacted_value(value: Any, *, key: str | None = None) -> str:
    """Return redacted/normalized value for operator markdown.

    pre: value may be scalar or a mapping/list.
    post: redacts secret-like values and assignments.
    raises: none.
    """

    normalized_key = (key or "").strip().lower()
    if isinstance(value, dict) or isinstance(value, list):
        return str(value)
    rendered = value if isinstance(value, str) else str(value)
    if normalized_key in _SECRET_KEYS or any(marker in normalized_key for marker in _SECRET_KEYS):
        return "***REDACTED***"
    redacted = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}={match.group(1) and '***REDACTED***'}", rendered)
    return _SECRET_VALUE_RE.sub("***REDACTED***", redacted)


def _load_list_summary(*, source: Any) -> dict[str, Any]:
    """Read origin-scoped Canon run list from durable backends.

    pre: Canon integration modules are importable and source carries origin identity.
    post: returns detached durable list payload from Canon operator authority.
    raises: ValueError/ImportError from origin or Canon integration loading.
    """

    list_runs_for_origin, journal, artifacts = _load_operator_backends(listing=True)
    return list_runs_for_origin(origin=_gateway_origin(source), journal=journal, artifacts=artifacts)


def _load_latest_summary(*, source: Any) -> dict[str, Any]:
    """Read the newest origin-scoped Canon run summary from durable backends.

    pre: Canon integration modules are importable and source carries origin identity.
    post: returns detached durable summary payload from Canon workflow index authority.
    raises: ValueError/LookupError/ImportError from origin or Canon integration loading.
    """

    latest_run_for_origin, journal, artifacts = _load_operator_backends()
    return latest_run_for_origin(origin=_gateway_origin(source), journal=journal, artifacts=artifacts)


def _load_inspect_summary(*, run_id: str, source: Any) -> dict[str, Any]:
    """Read one origin-scoped Canon run summary by run id from durable backends.

    pre: run_id is non-empty, Canon integration modules are importable, source has origin identity.
    post: returns detached durable inspect summary payload from Canon workflow index authority.
    raises: ValueError/LookupError/PermissionError/ImportError from Canon integration stack.
    """

    inspect_run_for_operator, journal, artifacts = _load_operator_backends(inspect=True)
    return inspect_run_for_operator(run_id=run_id, origin=_gateway_origin(source), journal=journal, artifacts=artifacts)


def _load_operator_backends(*, inspect: bool = False, listing: bool = False):
    """Load Canon durable operator callables/backends from integration authority.

    pre: Canon repo integration package and durability backends are available in runtime.
    post: returns operator callable plus journal/artifact backend instances rooted by Canon config.
    raises: ImportError when integration modules are unavailable.
    """

    from canon.durability import LocalArtifactBackend
    from canon.journal import SqliteExecutionJournal
    from integrations.hermes.canon_hermes.current_gateway_operator_commands import (
        inspect_run_for_operator,
        latest_run_for_origin,
        list_runs_for_origin,
    )
    from integrations.hermes.canon_hermes.current_gateway_runner import (
        build_current_gateway_runner_config,
    )

    config = build_current_gateway_runner_config()
    journal = SqliteExecutionJournal(config.journal_path)
    artifacts = LocalArtifactBackend(config.artifacts_dir)
    if listing:
        return list_runs_for_origin, journal, artifacts
    return (inspect_run_for_operator if inspect else latest_run_for_origin), journal, artifacts


def _gateway_origin(source: Any) -> str:
    """Build canonical gateway-source origin string used by Canon durable operator scope.

    pre: source carries a non-empty platform and chat_id.
    post: returns '<platform>:<chat_id>:<thread_id>' when thread_id is present, otherwise
          '<platform>:<chat_id>'.
    raises: ValueError when source identity is incomplete.
    """

    platform_raw = getattr(source, "platform", "")
    platform = getattr(platform_raw, "value", platform_raw)
    chat_id = str(getattr(source, "chat_id", "") or "").strip()
    thread_id = str(getattr(source, "thread_id", "") or "").strip()
    if not str(platform or "").strip():
        raise ValueError("source.platform is required for /canon durable lookup")
    if not chat_id:
        raise ValueError("source.chat_id is required for /canon durable lookup")
    return f"{platform}:{chat_id}:{thread_id}" if thread_id else f"{platform}:{chat_id}"


def _render_list_surface(*, listing: dict[str, Any]) -> str:
    """Render `/canon list` origin-scoped durable run listing as Russian markdown.

    pre: listing is detached mapping with origin/count/runs from Canon operator surface.
    post: returns bounded markdown bullets with run id/status/count and redacted values.
    raises: ValueError when listing payload is malformed.
    """

    if not isinstance(listing, dict):
        raise ValueError("durable list payload must be a mapping")
    origin = _to_redacted_value(listing.get("origin") or "unknown", key="origin")
    runs = listing.get("runs")
    if not isinstance(runs, list):
        raise ValueError("durable list payload.runs must be a list")
    count = listing.get("count")
    if not isinstance(count, int) or count < 0:
        raise ValueError("durable list payload.count must be a non-negative integer")

    lines = [
        "### Поверхность: Список запусков",
        "- Команда: `/canon list`",
        f"- Origin: `{origin}`",
        f"- Видимых запусков: `{count}`",
        "- Запуски:",
    ]
    if not runs:
        lines.append("  - <пусто>")
        return "\n".join(lines)

    for item in runs:
        if not isinstance(item, dict):
            continue
        run_id = _to_redacted_value(item.get("runId") or "unknown", key="runId")
        status = _to_redacted_value(item.get("status") or "unknown", key="status")
        lines.append(f"  - `{run_id}` — `{status}`")
    return "\n".join(lines)


def _format_operator_summary(*, prefix: str, summary: dict[str, Any]) -> str:
    """Render one durable Canon run summary for operator chat output.

    pre: summary is a detached mapping from Canon durable inspect/list authority.
    post: returns concise line with runId, status, and production classification label.
    raises: ValueError when summary is malformed.
    """

    if not isinstance(summary, dict):
        raise ValueError("durable summary must be a mapping")
    run_id = str(summary.get("runId") or "").strip() or "unknown"
    status = _to_redacted_value(summary.get("status") or "unknown", key="status").strip() or "unknown"
    label = "non-production" if _is_non_production_status(status) else "production"
    return f"`{prefix}` run `{run_id}` status `{status}` ({label})."


def _is_non_production_status(status: str) -> bool:
    """Classify Canon run status as non-production when it indicates blocked/dry-run semantics.

    pre: status is a status-like string from Canon durable summaries.
    post: returns True for blocked/dry-run/pending/non-terminal statuses and False otherwise.
    raises: none.
    """

    normalized = status.strip().lower()
    non_production_markers = (
        "blocked",
        "dry-run",
        "dry_run",
        "awaiting",
        "paused",
        "failed",
        "error",
        "rejected",
        "cancel",
    )
    return any(marker in normalized for marker in non_production_markers)
