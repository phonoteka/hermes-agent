"""Gateway-facing `/canon` command parsing and fail-closed operator responses.

This module intentionally keeps `/canon` parsing local to Hermes gateway while
delegating durable latest/inspect reads to Canon integration authority.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable


def handle_gateway_canon_command(event: Any) -> str:
    """Parse `/canon` command text and return a deterministic fail-closed response.

    pre: event has `.text` and optional `.source` with chat/thread/user identity attributes.
    post: `/canon run <workflow> <task...>` without task fails closed with task-required guidance.
    post: `/canon latest` and `/canon inspect <run-id>` query Canon durable stores when available.
    post: blocked/dry-run/non-terminal statuses are labeled non-production in operator output.
    raises: none.
    """

    text = (getattr(event, "text", "") or "").strip()
    args = text[len("/canon") :].strip() if text.startswith("/canon") else text
    tokens = shlex.split(args) if args else []
    if not tokens:
        return "Usage: /canon <run|latest|inspect> ..."

    subcommand = tokens[0].lower()
    source = getattr(event, "source", None)
    if subcommand == "run":
        return _handle_run(tokens=tokens, source=source)
    if subcommand == "latest":
        return _handle_latest(source=source)
    if subcommand == "inspect":
        if len(tokens) < 2 or not tokens[1].strip():
            return "`/canon inspect` requires an explicit run id: /canon inspect <run-id>."
        return _handle_inspect(run_id=tokens[1].strip(), source=source)
    return "Unsupported /canon subcommand. Usage: /canon <run|latest|inspect> ..."


async def handle_gateway_canon_command_live(
    event: Any,
    *,
    send_review_prompt: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> str:
    """Execute `/canon` with live current-gateway wiring for `/canon run`.

    pre: event contains message text and source identity; `send_review_prompt` is provided for run path.
    post: non-run commands keep deterministic sync behavior.
    post: run path creates a real current-gateway awaiting-human-review run or fails closed.
    raises: none.
    """

    text = (getattr(event, "text", "") or "").strip()
    args = text[len("/canon") :].strip() if text.startswith("/canon") else text
    tokens = shlex.split(args) if args else []
    if not tokens:
        return "Usage: /canon <run|latest|inspect> ..."

    if tokens[0].lower() != "run":
        return handle_gateway_canon_command(event)
    if send_review_prompt is None:
        return "`/canon run` failed closed: review-delivery sender is unavailable."

    try:
        return await _handle_live_run(
            tokens=tokens,
            source=getattr(event, "source", None),
            message_id=str(getattr(event, "message_id", "") or ""),
            send_review_prompt=send_review_prompt,
        )
    except Exception as exc:
        return f"`/canon run` failed closed: {exc}"


def _handle_run(*, tokens: list[str], source: Any) -> str:
    """Handle `/canon run` argument validation and topic identity echo.

    pre: tokens[0] == 'run'.
    post: requires explicit workflow and non-empty task text.
    post: response includes source chat/thread identity when available.
    raises: none.
    """

    if len(tokens) < 2 or not tokens[1].strip():
        return "`/canon run` requires workflow name and task text: /canon run <workflow> <task>."
    workflow = tokens[1].strip()
    task_text = " ".join(tokens[2:]).strip()
    if not task_text:
        return "`/canon run` requires explicit task text after workflow name."

    chat_id = str(getattr(source, "chat_id", "") or "")
    thread_id = str(getattr(source, "thread_id", "") or "")
    origin_bits = []
    if chat_id:
        origin_bits.append(f"chat_id={chat_id}")
    if thread_id:
        origin_bits.append(f"message_thread_id={thread_id}")
    origin_suffix = f" ({', '.join(origin_bits)})" if origin_bits else ""

    return (
        "`/canon run` accepted for production gateway dispatch"
        f"{origin_suffix}: workflow={workflow}, task={task_text}"
    )


async def _handle_live_run(
    *,
    tokens: list[str],
    source: Any,
    message_id: str,
    send_review_prompt: Callable[..., Awaitable[dict[str, Any]]],
) -> str:
    """Run live current-gateway flow for one `/canon run <workflow> <task>` command.

    pre: tokens include workflow + explicit task; source/message_id carry gateway origin identity.
    post: routes every workflow through the generic workflow facade — no workflow
          is hardcoded as the sole production path. Creates paused run + review
          card delivery evidence through Canon runner authority.
    raises: ValueError on malformed workflow/task/source identity or delivery/runtime failures.
    """

    if len(tokens) < 2 or not tokens[1].strip():
        raise ValueError("`/canon run` requires workflow name and task text: /canon run <workflow> <task>.")
    workflow = tokens[1].strip()
    # Route ALL workflows through the generic facade; no workflow is hardcoded
    # as the sole production path. The facade resolves workflow names to
    # repo-relative workflowRef paths (via static mapping or dynamic registry).
    from integrations.hermes.canon_hermes.workflow_facade import (
        resolve_workflow_ref_for_command as _facade_resolve_workflow_ref,
    )
    workflow_ref = _facade_resolve_workflow_ref(workflow_name=workflow)
    # post: workflow_ref is non-empty; ValueError is raised for unknown workflows.
    task_text = " ".join(tokens[2:]).strip()
    if not task_text:
        raise ValueError("`/canon run` requires explicit task text after workflow name.")

    import integrations.hermes.canon_hermes.current_gateway as current_gateway

    build_current_gateway_request = current_gateway.build_current_gateway_request
    from integrations.hermes.canon_hermes.current_gateway_runner import (
        _resolve_request_workflow_path,
        _run_current_gateway_runtime_pause,
        build_current_gateway_runner_config,
        pause_current_gateway_for_human_review,
    )

    if _is_generic_review_text(task_text):
        raise ValueError("task text is placeholder/default; provide an explicit operator task")

    platform_raw = getattr(source, "platform", "")
    platform = getattr(platform_raw, "value", platform_raw)
    if str(platform).strip().lower() != "telegram":
        raise ValueError("live current-gateway `/canon run` is only available for Telegram origin")

    chat_id = str(getattr(source, "chat_id", "") or "").strip()
    thread_id = str(getattr(source, "thread_id", "") or "").strip()
    user_id = str(getattr(source, "user_id", "") or "").strip()
    user_name = str(getattr(source, "user_name", "") or "").strip()
    if not chat_id or not thread_id or not user_id:
        raise ValueError("source chat/thread/user identity is required for live `/canon run`")
    if not message_id:
        raise ValueError("event.message_id is required for live `/canon run`")

    run_id = f"sm-{datetime.now(UTC).strftime('%y%m%d%H%M%S')}"
    request = build_current_gateway_request(
        workflow_ref=workflow_ref,
        run_id=run_id,
        inputs={"request": task_text},
        gateway_source={
            "platform": "telegram",
            "chat_id": chat_id,
            "thread_id": thread_id,
            "user_id": user_id,
            "user_name": user_name,
            "message_id": message_id,
        },
    )
    phase_backend_client = create_gateway_phase_backend_client()
    config = build_current_gateway_runner_config(phase_backend_client=phase_backend_client)
    phase_backend_client.bind_artifacts_dir(getattr(config, "artifacts_dir", None))
    workflow_path = _resolve_request_workflow_path(request)
    runtime_pause = await asyncio.to_thread(
        _run_current_gateway_runtime_pause,
        workflow_path=str(workflow_path),
        request=request,
        config=config,
    )
    resume_handle = runtime_pause.get("resumeHandle") if isinstance(runtime_pause, dict) else None
    if not isinstance(resume_handle, dict):
        raise RuntimeError("runtime pause result missing resumeHandle")

    loop = asyncio.get_running_loop()

    def _sender(payload: dict[str, Any]) -> dict[str, str]:
        """Bridge sync Canon sender hook to async Telegram adapter send call."""

        coro = send_review_prompt(
            chat_id=chat_id,
            thread_id=thread_id,
            run_id=run_id,
            text=str(payload.get("message") or ""),
            callbacks=payload.get("callbacks"),
            gate_identity=payload.get("gateIdentity"),
            downloadable_artifacts=payload.get("downloadableArtifacts"),
        )
        result = asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)
        normalized_message_id = str((result or {}).get("message_id") or (result or {}).get("messageId") or "").strip()
        if not normalized_message_id:
            raise RuntimeError("telegram review prompt did not return message id")
        normalized_chat_id = str((result or {}).get("chatId") or (result or {}).get("chat_id") or chat_id).strip()
        normalized_thread_id = str((result or {}).get("threadId") or (result or {}).get("thread_id") or thread_id).strip()
        delivery_result = {
            "messageId": normalized_message_id,
            "chatId": normalized_chat_id,
            "threadId": normalized_thread_id,
        }
        if isinstance(result, dict) and result.get("message_id"):
            delivery_result["message_id"] = normalized_message_id
        return delivery_result

    pause_result = await asyncio.to_thread(
        pause_current_gateway_for_human_review,
        run_id=run_id,
        thread_id=request["threadId"],
        node_id=str(resume_handle.get("nodeId") or ""),
        checkpoint_id=str(resume_handle.get("checkpointId") or ""),
        request=request,
        workflow_path=str(workflow_path),
        config=config,
        sender=_sender,
    )
    status = str((pause_result or {}).get("status") or "unknown")
    if status not in {"awaiting-human-review", "delivery-failed"}:
        raise RuntimeError(f"unexpected live run status: {status}")
    return f"`/canon run` live run `{run_id}` status `{status}` (chat_id={chat_id}, message_thread_id={thread_id})."


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
    """Run one Canon phase as a bounded Hermes model-only session.

    pre: envelope_projection is Canon-minted identity/scope authority for one phase.
    post: run_phase returns a backend payload with schema-shaped JSON output from a real model call;
          it never fabricates default approval content when model output is absent or invalid.
    raises: RuntimeError when model response is missing, non-JSON, or artifact persistence fails.
    """

    def __init__(self, *, envelope_projection: dict[str, Any], artifacts_dir: Path | None) -> None:
        self._projection = dict(envelope_projection)
        self._artifacts_dir = artifacts_dir

    def run_phase(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Execute one live model call and return Canon phase backend output.

        pre: envelope carries objective, inputs, outputSchema, and Canon identity fields.
        post: returns {"status":"succeeded", "output": <json>} only after parsing a JSON object;
              solution-modeling handoff refs are persisted under the current Canon artifact root.
        raises: RuntimeError when the live model cannot provide valid JSON or artifact writes fail.
        """

        if not isinstance(envelope, dict):
            raise RuntimeError("Canon phase envelope must be an object")
        prompt = self._build_phase_prompt(envelope)
        from hermes_cli.oneshot import _run_agent

        model_route = self._projection.get("modelRoute") if isinstance(self._projection.get("modelRoute"), dict) else {}
        model = str(model_route.get("model") or "").strip() or None
        provider = str(model_route.get("provider") or "").strip() or None
        response = _run_agent(
            prompt,
            model=model,
            provider=provider,
            toolsets=["skills", "file", "terminal"],
            use_config_toolsets=False,
        )
        output = _extract_json_object(response)
        artifact_refs = self._persist_solution_modeling_handoff(output, envelope=envelope)
        result: dict[str, Any] = {"status": "succeeded", "output": output}
        if artifact_refs:
            result["artifactRefs"] = artifact_refs
        result["agentSessionRef"] = self._agent_session_ref(envelope)
        return result

    def _build_phase_prompt(self, envelope: dict[str, Any]) -> str:
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
            f"{solution_modeling_guidance}\n"
            f"Canon identity: runId={run_id}, phaseId={phase_id}.\n\n"
            "Phase objective:\n"
            f"{objective}\n\n"
            "Inputs JSON:\n"
            f"{json.dumps(inputs, ensure_ascii=False, sort_keys=True, indent=2)}\n\n"
            "Output JSON Schema:\n"
            f"{json.dumps(output_schema, ensure_ascii=False, sort_keys=True, indent=2)}\n"
        )

    def _persist_solution_modeling_handoff(self, output: dict[str, Any], *, envelope: dict[str, Any]) -> list[str]:
        """Persist distinct solution-modeling handoff artifacts for human review and downstream specs.

        pre: output is the parsed model JSON for a Canon phase and may contain
             modelPackage.specPackageHandoff refs.
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


def _load_operator_backends(*, inspect: bool = False):
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
    )
    from integrations.hermes.canon_hermes.current_gateway_runner import (
        build_current_gateway_runner_config,
    )

    config = build_current_gateway_runner_config()
    journal = SqliteExecutionJournal(config.journal_path)
    artifacts = LocalArtifactBackend(config.artifacts_dir)
    return (inspect_run_for_operator if inspect else latest_run_for_origin), journal, artifacts


def _gateway_origin(source: Any) -> str:
    """Build canonical gateway-source origin string used by Canon durable operator scope.

    pre: source carries a non-empty platform and chat_id.
    post: returns '<platform>:<chat_id>' so operator reads are origin-scoped.
    raises: ValueError when source identity is incomplete.
    """

    platform_raw = getattr(source, "platform", "")
    platform = getattr(platform_raw, "value", platform_raw)
    chat_id = str(getattr(source, "chat_id", "") or "").strip()
    if not str(platform or "").strip():
        raise ValueError("source.platform is required for /canon durable lookup")
    if not chat_id:
        raise ValueError("source.chat_id is required for /canon durable lookup")
    return f"{platform}:{chat_id}"


def _format_operator_summary(*, prefix: str, summary: dict[str, Any]) -> str:
    """Render one durable Canon run summary for operator chat output.

    pre: summary is a detached mapping from Canon durable inspect/list authority.
    post: returns concise line with runId, status, and production classification label.
    raises: ValueError when summary is malformed.
    """

    if not isinstance(summary, dict):
        raise ValueError("durable summary must be a mapping")
    run_id = str(summary.get("runId") or "").strip() or "unknown"
    status = str(summary.get("status") or "unknown").strip() or "unknown"
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
