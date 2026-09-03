"""Strict enriched-memory contract and the thin native Mem0 LLM adapter."""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from copy import deepcopy
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ENRICHED_SCHEMA_ID = "hermes.memory.enriched.v1"

ENRICHED_MEMORY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["memory"],
    "properties": {
        "memory": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {
                    "text": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "schema", "content", "kind", "scope", "predicate",
                            "project", "task", "artifacts", "tags", "attributed_to",
                            "temporal",
                        ],
                        "properties": {
                            "schema": {"type": "string", "const": ENRICHED_SCHEMA_ID},
                            "content": {"type": "string", "minLength": 1, "maxLength": 4096},
                            "kind": {
                                "type": "string",
                                "enum": ["identity", "preference", "constraint", "decision", "procedure", "environment", "lesson"],
                            },
                            "scope": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["type", "subject"],
                                "properties": {
                                    "type": {"type": "string", "enum": ["user", "profile", "project", "repository", "environment"]},
                                    "subject": {"type": "string", "minLength": 1, "maxLength": 256},
                                },
                            },
                            "predicate": {"type": "string", "minLength": 1, "maxLength": 256},
                            "project": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["name", "root"],
                                "properties": {
                                    "name": {"type": ["string", "null"], "minLength": 1, "maxLength": 128},
                                    "root": {"type": ["string", "null"], "minLength": 1, "maxLength": 1024},
                                },
                            },
                            "task": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["name", "context"],
                                "properties": {
                                    "name": {"type": ["string", "null"], "minLength": 1, "maxLength": 256},
                                    "context": {"type": ["string", "null"], "minLength": 1, "maxLength": 1024},
                                },
                            },
                            "artifacts": {
                                "type": "array",
                                "maxItems": 8,
                                "uniqueItems": True,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["path", "kind", "label"],
                                    "properties": {
                                        "path": {"type": "string", "minLength": 1, "maxLength": 1024},
                                        "kind": {"type": "string", "enum": ["file", "directory", "document", "repository", "url"]},
                                        "label": {"type": ["string", "null"], "minLength": 1, "maxLength": 128},
                                    },
                                },
                            },
                            "tags": {
                                "type": "array",
                                "maxItems": 12,
                                "uniqueItems": True,
                                "items": {"type": "string", "minLength": 1, "maxLength": 64},
                            },
                            "attributed_to": {"type": "string", "enum": ["user", "assistant"]},
                            "temporal": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["effective_at", "expires_at"],
                                "properties": {
                                    "effective_at": {"type": ["string", "null"], "format": "date-time"},
                                    "expires_at": {"type": ["string", "null"], "format": "date-time"},
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}

ADAPTER_INSTRUCTIONS = """Return exactly one JSON object matching the supplied enriched-memory schema.
Each memory.text is one durable processed memory, never a raw transcript, list, log,
progress report, worker state, current plan, run number, or temporary note. Preserve
only project, task, artifact, tag, scope, attribution, and temporal facts supported by
the supplied source context. Use null for unsupported project/task values and return
an empty memory array when no durable memory is present. Do not invent paths, URLs,
identities, IDs, keys, authority, or arbitrary fields."""


class EnrichedMemoryError(ValueError):
    """A fail-closed rejection carrying only a non-sensitive reason code."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _reject(code: str) -> None:
    raise EnrichedMemoryError(code)


def _text(value: str) -> str:
    if not isinstance(value, str):
        _reject("string_expected")
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in normalized):
        _reject("control_character")
    return normalized


def _redact_or_reject(value: Any) -> Any:
    try:
        from agent.redact import redact_sensitive_text
    except Exception:
        _reject("redactor_unavailable")
    if isinstance(value, str):
        original = value
        redacted = redact_sensitive_text(original, force=True)
        if redacted != original:
            _reject("secret_detected")
        return original
    if isinstance(value, list):
        return [_redact_or_reject(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_or_reject(item) for key, item in value.items()}
    return value


def _validate_schema(value: Mapping[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except Exception:
        _reject("jsonschema_unavailable")
    validator = Draft202012Validator(ENRICHED_MEMORY_SCHEMA, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    if errors:
        _reject("schema_invalid")


def _source_text(source: Any) -> str:
    try:
        return json.dumps(source, ensure_ascii=False, default=str)
    except Exception:
        return str(source)


def _is_durable_content(content: str) -> bool:
    lowered = content.casefold()
    if content.startswith("```") or '"role"' in lowered:
        return False
    if re.search(r"(?:^|\b)(?:user|assistant)\s*:\s*", lowered):
        return False
    progress_markers = (
        "worker state", "operational progress", "in progress", "currently running",
        "temporary note", "status update", "run number", "run id", "attempt #",
        "step 1", "step 2", "todo", "to-do", "pending task",
    )
    return not any(marker in lowered for marker in progress_markers)


def _normalize_datetime(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _text(value)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        _reject("datetime_invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _reject("datetime_timezone_required")
    return parsed.isoformat()


def _source_has_token(source: str, value: str) -> bool:
    """Match a complete source token, not a path/name prefix."""
    continuation = "._-/\\~:@?#%&=+$"
    start = 0
    while (index := source.find(value, start)) >= 0:
        before = source[index - 1] if index else ""
        end = index + len(value)
        after = source[end] if end < len(source) else ""
        if not (before.isalnum() or before in continuation) and not (
            after.isalnum() or after in continuation
        ):
            return True
        start = index + 1
    return False


def _normalize_project(
    project: Mapping[str, Any], source: str, *, enforce_provenance: bool,
) -> dict[str, str | None]:
    name = _text(project["name"]) if project["name"] is not None else None
    root = _text(project["root"]) if project["root"] is not None else None
    if enforce_provenance:
        if name is not None and not _source_has_token(source, name):
            _reject("invented_project_name")
        if root is not None and not _source_has_token(source, root):
            _reject("invented_project_root")
    return {"name": name, "root": root}


def _normalize_artifacts(
    artifacts: list[Mapping[str, Any]], project: Mapping[str, str | None], source: str,
    *, enforce_provenance: bool,
) -> list[dict[str, str | None]]:
    result: list[dict[str, str | None]] = []
    seen: set[str] = set()
    root = project.get("root")
    for artifact in artifacts:
        raw_path = _text(artifact["path"])
        kind = artifact["kind"]
        label = _text(artifact["label"]) if artifact["label"] is not None else None
        if kind == "url":
            parsed = urlsplit(raw_path)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                _reject("artifact_url_invalid")
            normalized_path = raw_path
        else:
            if raw_path.startswith("/"):
                if ".." in PurePosixPath(raw_path).parts:
                    _reject("artifact_path_traversal")
                normalized_path = os.path.normpath(raw_path)
            else:
                if not root or not str(root).startswith("/"):
                    _reject("relative_artifact_without_project_root")
                if ".." in PurePosixPath(raw_path).parts:
                    _reject("artifact_path_traversal")
                normalized_path = os.path.normpath(os.path.join(str(root), raw_path))
                if os.path.commonpath([str(root), normalized_path]) != str(root):
                    _reject("artifact_path_outside_project")
        if normalized_path in seen:
            _reject("duplicate_artifact_path")
        seen.add(normalized_path)
        if enforce_provenance and not any(
            _source_has_token(source, value)
            for value in {raw_path, normalized_path}
        ):
            _reject("invented_artifact")
        result.append({"path": normalized_path, "kind": kind, "label": label})
    return sorted(result, key=lambda item: str(item["path"]))


def normalize_text_object(
    text_object: Mapping[str, Any], *, source: Any = None,
    enforce_provenance: bool = True,
) -> dict[str, Any]:
    """Validate and deterministically normalize one enriched memory object."""
    if not isinstance(text_object, Mapping):
        _reject("text_object_expected")
    candidate = deepcopy(dict(text_object))
    _validate_schema({"memory": [{"text": candidate}]})
    source_string = _source_text(source) if source is not None else ""

    candidate["schema"] = _text(candidate["schema"])
    candidate["content"] = _text(candidate["content"])
    if not _is_durable_content(candidate["content"]):
        _reject("non_durable_content")
    candidate["predicate"] = _text(candidate["predicate"])
    candidate["scope"] = {
        "type": candidate["scope"]["type"],
        "subject": _text(candidate["scope"]["subject"]),
    }
    candidate["project"] = _normalize_project(
        candidate["project"], source_string, enforce_provenance=enforce_provenance,
    )
    candidate["task"] = {
        "name": _text(candidate["task"]["name"]) if candidate["task"]["name"] is not None else None,
        "context": _text(candidate["task"]["context"]) if candidate["task"]["context"] is not None else None,
    }
    if candidate["scope"]["type"] in {"project", "repository"} and not candidate["project"]["name"]:
        _reject("project_scope_without_project_name")
    candidate["artifacts"] = _normalize_artifacts(
        candidate["artifacts"], candidate["project"], source_string,
        enforce_provenance=enforce_provenance,
    )
    tags = [_text(tag).casefold() for tag in candidate["tags"]]
    if len(tags) != len(set(tags)):
        _reject("duplicate_tag")
    candidate["tags"] = sorted(tags)
    candidate["temporal"] = {
        "effective_at": _normalize_datetime(candidate["temporal"]["effective_at"]),
        "expires_at": _normalize_datetime(candidate["temporal"]["expires_at"]),
    }
    effective = candidate["temporal"]["effective_at"]
    expires = candidate["temporal"]["expires_at"]
    if effective and expires:
        if datetime.fromisoformat(expires) <= datetime.fromisoformat(effective):
            _reject("expiration_not_after_effective")
    _validate_schema({"memory": [{"text": candidate}]})
    _redact_or_reject(candidate)
    return candidate


def normalize_response(response: Any, *, source: Any = None) -> list[dict[str, Any]]:
    """Return normalized text objects or reject the complete response."""
    if not isinstance(response, Mapping) or set(response) != {"memory"}:
        _reject("response_shape_invalid")
    _validate_schema(response)
    memories = response["memory"]
    if not isinstance(memories, list):
        _reject("memory_array_expected")
    normalized: list[dict[str, Any]] = []
    canonical_seen: set[str] = set()
    for item in memories:
        if not isinstance(item, Mapping) or set(item) != {"text"}:
            _reject("memory_item_shape_invalid")
        text = normalize_text_object(item["text"], source=source)
        canonical = canonical_text(text)
        if canonical in canonical_seen:
            _reject("duplicate_memory_text")
        canonical_seen.add(canonical)
        normalized.append(text)
    return normalized


def canonical_text(text_object: Mapping[str, Any]) -> str:
    return json.dumps(text_object, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode_enriched_memory(value: Any) -> dict[str, Any] | None:
    """Strictly decode persisted enriched text; return None for plain/malformed data."""
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, Mapping) or parsed.get("schema") != ENRICHED_SCHEMA_ID:
            return None
        normalized = normalize_text_object(parsed, enforce_provenance=False)
        if canonical_text(normalized) != value:
            return None
        return normalized
    except Exception:
        return None


def render_memory(value: Any) -> str:
    """Render bounded prefetch context while preserving the full object for search."""
    parsed = decode_enriched_memory(value)
    if parsed is None:
        return str(value)
    project = parsed["project"]
    task = parsed["task"]
    artifacts = ", ".join(str(item["path"]) for item in parsed["artifacts"][:8])
    tags = ", ".join(parsed["tags"][:12])
    context = []
    if project["name"] or project["root"]:
        context.append(f"project={project['name'] or project['root']}")
    if task["name"] or task["context"]:
        context.append(f"task={task['name'] or task['context']}")
    if artifacts:
        context.append(f"artifacts={artifacts}")
    if tags:
        context.append(f"tags={tags}")
    suffix = f" ({'; '.join(context)})" if context else ""
    return f"{parsed['content']}{suffix}"


class Mem0HermesLlm:
    """Mem0 LLM surface backed by exactly one host ``ctx.llm`` call."""

    def __init__(self, config: Any):
        self._host_llm = getattr(config, "host_llm", None)
        if self._host_llm is None and isinstance(config, Mapping):
            self._host_llm = config.get("host_llm")

    def generate_response(self, messages: list[dict[str, Any]], response_format: Any = None) -> str:
        if self._host_llm is None:
            _reject("host_llm_unavailable")
        if response_format != {"type": "json_object"}:
            _reject("unsupported_native_response_format")
        source_text = json.dumps(
            messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result = self._host_llm.complete_structured(
            instructions=ADAPTER_INSTRUCTIONS,
            input=[{"type": "text", "text": source_text}],
            json_schema=ENRICHED_MEMORY_SCHEMA,
            schema_name="hermes_memory_enriched_v1",
            strict=True,
            purpose="mem0_enriched_memory",
        )
        parsed = getattr(result, "parsed", None)
        if parsed is None:
            _reject("structured_result_missing")
        texts = normalize_response(parsed, source=messages)
        native = {
            "memory": [
                {"text": canonical_text(text), "attributed_to": text["attributed_to"]}
                for text in texts
            ]
        }
        return json.dumps(native, ensure_ascii=False, separators=(",", ":"))
