"""Agent-callable Canon durable observability tool surface.

This tool exposes bounded origin-scoped observability reads over current-gateway
Canon durable truth for non-slash agent callers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from tools.canon_workflow_observe import to_markdown as _to_markdown
from tools.registry import registry

CANON_WORKFLOW_OBSERVE_SCHEMA = {
    "name": "canon_workflow_observe",
    "description": (
        "Read Canon current-gateway durable observability for an explicit origin. "
        "Supports latest, list, full/inspect, and report surfaces with fail-closed scope checks."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "origin": {
                "type": "string",
                "description": "Required origin scope (for example: telegram:<chat_id>).",
            },
            "surface": {
                "type": "string",
                "enum": ["latest", "list", "full", "inspect", "report"],
                "description": "Observability surface to render. Report renders the run report projection rather than the full inspect view.",
            },
            "run_id": {
                "type": "string",
                "description": "Required for run-scoped surfaces: full/inspect/report.",
            },
        },
        "required": ["origin", "surface"],
        "additionalProperties": False,
    },
}


def _load_operator_backends(*, inspect: bool = False, listing: bool = False):
    """Load Canon operator durable helpers and backends.

    pre: Canon integration packages are importable.
    post: returns operator callable and initialized durable journal/artifact backends.
    raises: ImportError when integration/durability modules are unavailable.
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


def _check_requirements() -> bool:
    """Expose tool when Canon durable configuration imports resolve.

    pre: none.
    post: returns True only if canonical durability/config imports and config resolution work.
    raises: none.
    """

    try:
        from integrations.hermes.canon_hermes.current_gateway_runner import (
            build_current_gateway_runner_config,
        )

        config = build_current_gateway_runner_config()
        return bool(getattr(config, "journal_path", None) and getattr(config, "artifacts_dir", None))
    except Exception:
        return False


def _normalize_origin(value: Any) -> str:
    """Normalize and validate origin string.

    pre: value is user-provided origin candidate.
    post: returns non-empty '<platform>:<scope>' origin string.
    raises: ValueError when origin is missing or malformed.
    """

    origin = str(value or "").strip()
    if not origin:
        raise ValueError("origin is required (expected format: <platform>:<scope>)")
    if ":" not in origin:
        raise ValueError("origin must include ':' separator (example: telegram:<chat_id>)")
    platform, scope = origin.split(":", 1)
    if not platform.strip() or not scope.strip():
        raise ValueError("origin must be non-empty on both sides of ':'")
    return f"{platform.strip()}:{scope.strip()}"


def _normalize_surface(value: Any) -> str:
    surface = str(value or "").strip().lower()
    if surface not in {"latest", "list", "full", "inspect", "report"}:
        raise ValueError("surface must be one of: latest, list, full, inspect, report")
    return surface


def _normalize_run_id(value: Any) -> str:
    run_id = str(value or "").strip()
    if not run_id:
        raise ValueError("run_id is required for run-scoped observe surfaces")
    return run_id


def _render_list_markdown(origin: str, listing: Mapping[str, Any]) -> str:
    runs = listing.get("runs")
    count = listing.get("count")
    lines = [
        "### Canon observe: list",
        f"- origin: `{origin}`",
        f"- count: `{count if isinstance(count, int) else 0}`",
        "- runs:",
    ]
    if not isinstance(runs, list) or not runs:
        lines.append("  - <empty>")
        return "\n".join(lines)
    for row in runs[:10]:
        if not isinstance(row, Mapping):
            continue
        run_id = str(row.get("runId") or "unknown")
        status = str(row.get("status") or "unknown")
        lines.append(f"  - `{run_id}` — `{status}`")
    if len(runs) > 10:
        lines.append(f"  - ... and {len(runs) - 10} more")
    return "\n".join(lines)


def _observe_impl(*, origin: str, surface: str, run_id: str | None) -> dict[str, Any]:
    """Read one observability surface from Canon durable truth.

    pre: origin/surface normalized; run_id provided for run-scoped surfaces.
    post: returns bounded JSON-serializable payload including success/surface/markdown.
    raises: ValueError/LookupError/ImportError for fail-closed invalid or unavailable authority.
    """

    if surface == "list":
        list_runs_for_origin, journal, artifacts = _load_operator_backends(listing=True)
        listing = list_runs_for_origin(origin=origin, journal=journal, artifacts=artifacts)
        markdown = _render_list_markdown(origin, listing if isinstance(listing, Mapping) else {})
        return {"success": True, "surface": surface, "origin": origin, "markdown": markdown}

    if surface == "latest":
        latest_run_for_origin, journal, artifacts = _load_operator_backends()
        summary = latest_run_for_origin(origin=origin, journal=journal, artifacts=artifacts)
        markdown = _to_markdown(summary if isinstance(summary, Mapping) else {})
        return {
            "success": True,
            "surface": surface,
            "origin": origin,
            "run_id": str((summary or {}).get("runId") or "") if isinstance(summary, Mapping) else "",
            "markdown": markdown,
        }

    scoped_run_id = _normalize_run_id(run_id)
    inspect_run_for_operator, journal, artifacts = _load_operator_backends(inspect=True)
    summary = inspect_run_for_operator(
        run_id=scoped_run_id,
        origin=origin,
        journal=journal,
        artifacts=artifacts,
    )
    inspect_summary = summary if isinstance(summary, Mapping) else {}
    markdown = _render_run_scoped_markdown(surface=surface, summary=inspect_summary)
    return {
        "success": True,
        "surface": surface,
        "origin": origin,
        "run_id": scoped_run_id,
        "markdown": markdown,
    }


def _inline_markdown_scalar(value: Any) -> str:
    """Render one compact scalar-ish value for report summary markdown safety.

    pre: value is a scalar or already flattened compact item from durable report summary data.
    post: returns a non-empty display string without raw dict/list syntax for null values.
    raises: none.
    """

    if value is None:
        return "-"
    text = str(value).strip()
    return text or "-"



def _inline_markdown_mapping(payload: Mapping[str, Any]) -> str:
    """Flatten one mapping into compact `key=value` text for observe markdown.

    pre: payload is a small operator-facing mapping from durable report summary data.
    post: returns one bounded string without raw JSON braces.
    raises: none.
    """

    pairs: list[str] = []
    for key, value in payload.items():
        label = str(key)
        if isinstance(value, Mapping):
            pairs.append(f"{label}=({_inline_markdown_mapping(value)})")
            continue
        if isinstance(value, list):
            flattened = ", ".join(_inline_markdown_value(item) for item in value[:5])
            pairs.append(f"{label}={flattened or '-'}")
            continue
        pairs.append(f"{label}={_inline_markdown_scalar(value)}")
    return ", ".join(pairs) if pairs else "-"



def _inline_markdown_value(value: Any) -> str:
    """Flatten one report-summary value into compact markdown-safe text.

    pre: value is any durable report summary value.
    post: mappings/lists become bounded text fragments without raw dict/list syntax.
    raises: none.
    """

    if isinstance(value, Mapping):
        return _inline_markdown_mapping(value)
    if isinstance(value, list):
        flattened = ", ".join(_inline_markdown_value(item) for item in value[:5])
        return flattened or "-"
    return _inline_markdown_scalar(value)



def _normalize_report_summary_for_markdown(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Prepare inspect summary report payload for bounded helper markdown rendering.

    pre: summary is one durable inspect summary mapping.
    post: returns a shallow copy whose report/reportSummary values avoid raw dict/list dumps.
    raises: none.
    """

    normalized = dict(summary)
    for key in ("reportSummary", "report"):
        raw_report = normalized.get(key)
        if not isinstance(raw_report, Mapping):
            continue
        normalized[key] = {
            report_key: _inline_markdown_value(report_value) for report_key, report_value in raw_report.items()
        }
    return normalized



def _render_run_scoped_markdown(*, surface: str, summary: Mapping[str, Any]) -> str:
    """Render one run-scoped tool surface without conflating report and full inspect.

    pre: surface is normalized and summary is the durable inspect projection for one run.
    post: `report` renders only the report projection; full/inspect render the complete summary.
    raises: none.
    """

    normalized_summary = _normalize_report_summary_for_markdown(summary)
    if surface != "report":
        return _to_markdown(normalized_summary)
    report_summary = normalized_summary.get("reportSummary")
    if report_summary is None:
        report_summary = normalized_summary.get("report")
    report_view: dict[str, Any] = {"status": normalized_summary.get("status", "unknown")}
    if report_summary is not None:
        report_view["reportSummary"] = report_summary
    return _to_markdown(report_view)


def _handle_tool(args: dict[str, Any], **_: Any) -> str:
    """Tool handler returning JSON string.

    pre: args follows CANON_WORKFLOW_OBSERVE_SCHEMA.
    post: returns JSON string with success/surface and bounded markdown.
    raises: none; failures are encoded as success=False with message.
    """

    try:
        origin = _normalize_origin(args.get("origin"))
        surface = _normalize_surface(args.get("surface"))
        run_id = args.get("run_id")
        if surface in {"full", "inspect", "report"} and not str(run_id or "").strip():
            raise ValueError(f"run_id is required for surface '{surface}'")
        payload = _observe_impl(origin=origin, surface=surface, run_id=str(run_id or ""))
        return json.dumps(payload, ensure_ascii=False)
    except Exception as exc:
        surface = str(args.get("surface") or "").strip().lower()
        return json.dumps({"success": False, "surface": surface or "unknown", "error": str(exc)}, ensure_ascii=False)


registry.register(
    name="canon_workflow_observe",
    toolset="messaging",
    schema=CANON_WORKFLOW_OBSERVE_SCHEMA,
    handler=_handle_tool,
    check_fn=_check_requirements,
    emoji="🔭",
)
