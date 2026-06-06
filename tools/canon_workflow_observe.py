"""Helpers for operator-facing Canon workflow observability output formatting."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_REDACTED_VALUE = "***REDACTED***"
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
}
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(token|password|api[_-]?key|private[_-]?key|secret|prompt|raw[_-]?prompt|tool[_-]?stdout|transcript)\s*[:=]\s*([^\n;,}]*)"
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(top[_-]?secret|secret[_-]?(token|value|status)?|password|api[_-]?key|private[_-]?key|bearer\s+[a-z0-9._-]+|sk-[a-z0-9_-]+)"
)


def to_markdown(summary: Mapping[str, Any] | dict[str, Any]) -> str:
    """Render one run summary as operator-facing markdown.

    pre: summary is a dictionary-like object produced by Canon durable store.
    post: returns markdown without raw JSON braces and includes localized status text.
    post: includes artifact names when artifact metadata is available.
    raises: ValueError when summary is not mapping-like.
    """

    if not isinstance(summary, Mapping):
        raise ValueError("summary must be a mapping")

    status = _text(summary.get("status") or "unknown", "unknown", key="status")
    workflow_ref = _text(summary.get("workflowRef"), "")
    artifacts = _extract_artifact_names(summary.get("artifacts"))

    lines: list[str] = [
        "# Обзор состояния Canon",
        "",
        f"- **статус:** `{status}`",
    ]

    if workflow_ref:
        lines.append(f"- workflow: `{workflow_ref}`")

    lines.append("- **артефакты:**")
    if artifacts:
        lines.extend(f"  - {artifact}" for artifact in artifacts)
    else:
        lines.append("  - не опубликованы")

    phase_attempt_lines = _render_phase_attempts(summary.get("phaseAttempts"))
    if phase_attempt_lines:
        lines.append("")
        lines.extend(phase_attempt_lines)

    report_summary_lines = _render_report_summary(summary.get("reportSummary"))
    if report_summary_lines:
        lines.append("")
        lines.extend(report_summary_lines)

    return "\n".join(lines)


def _render_report_summary(raw_report_summary: Any) -> list[str]:
    """Render compact Russian markdown for Canon observe `reportSummary`.

    pre: raw_report_summary may be mapping/list/scalar from durable observe summary.
    post: returns bounded redacted markdown lines or empty list when payload absent.
    raises: none.
    """

    if raw_report_summary is None:
        return []

    lines: list[str] = ["## Сводка отчета"]
    if isinstance(raw_report_summary, Mapping):
        for key, value in raw_report_summary.items():
            key_text = str(key)
            if isinstance(value, list):
                rendered_values = ", ".join(_scalar(item, default="-", key=key_text) for item in value)
                lines.append(f"- {key_text}: {rendered_values if rendered_values else 'нет'}")
                continue
            if isinstance(value, Mapping):
                compact = _render_key_value_pairs(value, allowed_keys=tuple(str(k) for k in value.keys()), default="нет")
                lines.append(f"- {key_text}: {compact}")
                continue
            lines.append(f"- {key_text}: {_scalar(value, default='-', key=key_text)}")
        return lines

    if isinstance(raw_report_summary, list):
        rendered_values = ", ".join(_scalar(item, default="-", key="reportSummary") for item in raw_report_summary)
        lines.append(f"- Значение: {rendered_values if rendered_values else 'нет'}")
        return lines

    lines.append(f"- Значение: {_scalar(raw_report_summary, default='-', key='reportSummary')}")
    return lines


def _render_phase_attempts(raw_phase_attempts: Any) -> list[str]:
    """Render bounded Russian markdown for phase-attempt observability details.

    pre: raw_phase_attempts may be list-like attempt records from Canon inspect summary.
    post: returns readable bullet lines with bounded size and redacted secret-like values.
    raises: none.
    """

    if not isinstance(raw_phase_attempts, list) or not raw_phase_attempts:
        return []

    lines: list[str] = ["## Попытки фаз (G9)"]
    for index, attempt in enumerate(raw_phase_attempts[:5], start=1):
        if not isinstance(attempt, Mapping):
            continue
        node_id = _text(attempt.get("nodeId"), "unknown")
        attempt_count = _scalar(attempt.get("attemptCount"), default="?")
        last_event_kind = _text(attempt.get("lastEventKind"), "unknown")
        last_status = _text(attempt.get("lastStatus"), "unknown", key="status")
        lines.append(
            f"- Попытка {index}: узел=`{node_id}`, count=`{attempt_count}`, событие=`{last_event_kind}`, статус=`{last_status}`"
        )

        feedback_line = _render_key_value_pairs(
            attempt.get("validationFeedback"),
            allowed_keys=("code", "reason", "message", "token", "api_key", "status"),
            default="нет",
        )
        lines.append(f"  - validationFeedback: {feedback_line}")

        attestations_line = _render_attestation_summaries(attempt.get("backendAttestations"))
        lines.append(f"  - backendAttestations: {attestations_line}")

        refs_line = _render_backend_refs(attempt.get("backendRefs"))
        lines.append(f"  - backendRefs: {refs_line}")

    if len(raw_phase_attempts) > 5:
        lines.append(f"- …и ещё {len(raw_phase_attempts) - 5} попыток")
    return lines


def _render_key_value_pairs(payload: Any, *, allowed_keys: tuple[str, ...], default: str) -> str:
    """Render compact key=value pairs from selected keys without JSON dump syntax."""

    if isinstance(payload, Mapping):
        items_to_render: list[Mapping[str, Any]] = [payload]
    elif isinstance(payload, list):
        items_to_render = [item for item in payload if isinstance(item, Mapping)][:3]
    else:
        return default

    rendered_items: list[str] = []
    for item in items_to_render:
        pairs: list[str] = []
        for key in allowed_keys:
            if key not in item:
                continue
            pairs.append(f"{key}={_scalar(item.get(key), default='-', key=key)}")
        if pairs:
            rendered_items.append(", ".join(pairs))

    if not rendered_items:
        return default
    return "; ".join(rendered_items)


def _render_attestation_summaries(raw_attestations: Any) -> str:
    """Summarize attestation rows in a bounded semicolon-delimited string."""

    if not isinstance(raw_attestations, list) or not raw_attestations:
        return "нет"

    chunks: list[str] = []
    for raw_item in raw_attestations[:3]:
        if not isinstance(raw_item, Mapping):
            continue
        status = _scalar(raw_item.get("status"), default="unknown")
        digest = _scalar(raw_item.get("digest"), default="-")
        reason = _scalar(raw_item.get("reason"), default="-")
        chunks.append(f"status={status}, digest={digest}, reason={reason}")
    return "; ".join(chunks) if chunks else "нет"


def _render_backend_refs(raw_backend_refs: Any) -> str:
    """Summarize backend refs including session/checkpoint/cursor references.

    pre: raw_backend_refs may be Canon backend refs mapping.
    post: returns compact text with bounded cursor refs and redacted values.
    raises: none.
    """

    if isinstance(raw_backend_refs, Mapping):
        session_ref = _scalar(raw_backend_refs.get("agentSessionRef"), default="-")
        checkpoint_ref = _scalar(raw_backend_refs.get("agentCheckpointRef"), default="-")
        backend_ref = _scalar(raw_backend_refs.get("backendRef"), default="-")
        cursor_refs = raw_backend_refs.get("eventCursorRefs")
        if isinstance(cursor_refs, list) and cursor_refs:
            rendered_cursor_refs = ", ".join(_scalar(item, default="-") for item in cursor_refs[:3])
        else:
            rendered_cursor_refs = "-"
        return (
            f"agentSessionRef={session_ref}, agentCheckpointRef={checkpoint_ref}, "
            f"eventCursorRefs={rendered_cursor_refs}, backendRef={backend_ref}"
        )

    if not isinstance(raw_backend_refs, list) or not raw_backend_refs:
        return "нет"

    rendered: list[str] = []
    for entry in raw_backend_refs[:5]:
        if isinstance(entry, str):
            rendered.append(f"ref={_scalar(entry, default='-')}")
            continue
        if not isinstance(entry, Mapping):
            continue
        kind = _text(entry.get("kind"), "")
        if kind in {"agentSessionRef", "agentCheckpointRef", "backendRef"}:
            rendered.append(f"{kind}={_scalar(entry.get('ref'), default='-')}")
            continue
        if kind == "eventCursorRefs":
            refs = entry.get("refs")
            if isinstance(refs, list) and refs:
                refs_rendered = ",".join(_scalar(item, default="-") for item in refs[:3])
            else:
                refs_rendered = "-"
            rendered.append(f"eventCursorRefs={refs_rendered}")
            continue
        backend_kind = _text(entry.get("backendKind"), "")
        if backend_kind:
            attempt = _scalar(entry.get("attempt"), default="-")
            ref = _scalar(entry.get("ref"), default="-")
            rendered.append(f"{backend_kind}[{attempt}]={ref}")
            continue
        if "ref" in entry:
            rendered.append(f"ref={_scalar(entry.get('ref'), default='-')}")

    return "; ".join(rendered) if rendered else "нет"


def _scalar(value: Any, *, default: str, key: str | None = None) -> str:
    """Render scalar-ish values as short redacted text for markdown lists.

    pre: value may be scalar-ish runtime data and key may identify its source field.
    post: returns a stripped, redacted string or default when value is empty.
    raises: none.
    """

    if value is None:
        return default
    if isinstance(value, str):
        normalized = _redact_secret_value(value, key=key).strip()
        return normalized if normalized else default
    if isinstance(value, (bool, int, float)):
        return str(value)
    return _redact_secret_value(str(value), key=key)


def _extract_artifact_names(raw_artifacts: Any) -> list[str]:
    """Extract stable artifact names from summary artifact records.

    pre: raw_artifacts may be list-like artifact dictionaries.
    post: returns a list of names sorted for stable operator output.
    raises: none.
    """

    if not isinstance(raw_artifacts, list):
        return []

    names: list[str] = []
    for artifact in raw_artifacts:
        if not isinstance(artifact, Mapping):
            continue
        if isinstance(artifact.get("fileName"), str) and artifact["fileName"].strip():
            names.append(_redact_secret_value(artifact["fileName"], key="fileName").strip())
        elif isinstance(artifact.get("path"), str) and artifact["path"].strip():
            names.append(_redact_secret_value(artifact["path"], key="path").strip().split("/")[-1])

    return sorted(set(names))


def _text(value: Any, default: str, *, key: str | None = None) -> str:
    """Normalize optional text values in markdown output.

    pre: value may be any object.
    post: returns stripped string or default when empty.
    raises: none.
    """

    if not isinstance(value, str):
        return default
    normalized = _redact_secret_value(value.strip(), key=key)
    return normalized if normalized else default


def _redact_secret_value(value: Any, *, key: str | None) -> str:
    """Return a redacted string for secret-bearing values.

    pre: value is any object and optionally accompanied by the source field key.
    post: string with secret-like keys/values replaced by redaction marker.
    raises: none.
    """

    if not isinstance(value, str):
        return str(value)
    redacted = value
    lowered_key = (key or "").strip().lower()
    if lowered_key in _SECRET_KEYS or any(marker in lowered_key for marker in _SECRET_KEYS):
        return _REDACTED_VALUE
    redacted = _SECRET_ASSIGNMENT_RE.sub(_SECRET_SUBSTITUTION, redacted)
    redacted = _SECRET_VALUE_RE.sub(_REDACTED_VALUE, redacted)
    return redacted


def _SECRET_SUBSTITUTION(match: re.Match[str]) -> str:
    """Keep secret field names and replace attached values with marker."""

    return f"{match.group(1)}={_REDACTED_VALUE}"
