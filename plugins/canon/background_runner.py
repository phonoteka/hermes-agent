#!/usr/bin/env python3
"""Run one private Canon manifest with shell-free child execution."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
from typing import Any

_MANIFEST_SCHEMA = "canon-plugin-background-manifest.v1"
_MANIFEST_KEYS = frozenset({"schemaVersion", "operation", "argv", "timeoutSeconds", "cleanupRoot"})
_MAX_TIMEOUT = 600
_MAX_STDOUT_BYTES = 1_048_576
_MAX_COMPLETION_CHARS = 2_000
_RESULT_SCHEMA_VERSION = "canon-run-result.schema.v2"
_RESULT_REQUIRED_KEYS = frozenset({
    "schemaVersion", "status", "runId", "threadId", "workflowRef", "artifactRefs", "result",
})
_RESULT_OPTIONAL_KEYS = frozenset({
    "checkpointId", "nodeId", "eventSequence", "appendSequence", "executionFacts", "pendingHumanGate",
})
_RESULT_STATUSES = frozenset({"running", "paused", "completed", "failed", "cancelled"})
_HUMAN_GATE_PROMPT_KEYS = frozenset({
    "type", "status", "workflowRef", "gateId", "instruction", "actions",
    "resumeHandle", "subjectRevision", "subjectDigest", "decisionAttempt",
    "cardBody", "cardBodyDigest",
})
_HUMAN_GATE_ACTIONS = ["approve", "revise", "abort"]
_HUMAN_GATE_WAKE_INSTRUCTION = (
    "Canon-процесс остановлен на Human Gate.\n"
    "Используй clarify и передай пользователю точное содержимое карточки.\n"
    "Не пересказывай карточку и не принимай решение самостоятельно.\n"
    "После ответа пользователя передай typed decision в текущий checkpoint."
)
_child: subprocess.Popen[bytes] | None = None


def _private_file(path: Path, mode: int) -> bool:
    try:
        info = path.lstat()
        return stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == mode
    except OSError:
        return False


def _private_root(path: Path) -> bool:
    try:
        info = path.lstat()
        return (
            stat.S_ISDIR(info.st_mode)
            and stat.S_IMODE(info.st_mode) == 0o700
            and path.is_absolute()
            and path.resolve(strict=True) == path
        )
    except (OSError, RuntimeError):
        return False


def _owned_manifest_root(path: Path) -> Path | None:
    if not path.is_absolute() or path.name != "manifest.json" or not _private_file(path, 0o600):
        return None
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    root = path.parent
    if (
        canonical != path
        or not root.name.startswith("hermes-canon-")
        or root.parent.name != "canon"
        or root.parent.parent.name != "runtime"
        or not _private_root(root)
    ):
        return None
    return root


def _reject_constant(value: str) -> None:
    raise ValueError(value)


def _bounded_error(message: str) -> None:
    sys.stdout.write(json.dumps({"error": message}, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _read_manifest(path: Path) -> tuple[dict[str, Any], Path] | None:
    if not path.is_absolute() or not _private_file(path, 0o600):
        return None
    try:
        canonical = path.resolve(strict=True)
        if canonical != path:
            return None
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    try:
        manifest = json.loads(raw, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        return None
    if manifest.get("schemaVersion") != _MANIFEST_SCHEMA:
        return None
    if manifest.get("operation") not in {"launch", "restart", "command", "observe"}:
        return None
    argv = manifest.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item for item in argv):
        return None
    timeout = manifest.get("timeoutSeconds")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= _MAX_TIMEOUT:
        return None
    cleanup_raw = manifest.get("cleanupRoot")
    if not isinstance(cleanup_raw, str) or not cleanup_raw.strip():
        return None
    cleanup_root = Path(cleanup_raw)
    if not _private_root(cleanup_root) or cleanup_root != path.parent:
        return None
    return manifest, cleanup_root


def _terminate_child() -> None:
    child = _child
    if child is None or child.poll() is not None:
        return
    try:
        child.terminate()
        child.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            child.kill()
            child.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _signal_handler(_signum: int, _frame: Any) -> None:
    _terminate_child()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _decode_json(stdout: bytes | str) -> Any | None:
    if not stdout or len(stdout.encode("utf-8") if isinstance(stdout, str) else stdout) > _MAX_STDOUT_BYTES:
        return None
    try:
        text = stdout.decode("utf-8") if isinstance(stdout, bytes) else stdout
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return None
    return value


def _decode_object(stdout: bytes | str) -> dict[str, Any] | None:
    value = _decode_json(stdout)
    return value if isinstance(value, dict) else None


def _finite_json(value: Any) -> bool:
    if isinstance(value, float):
        return value == value and value not in {float("inf"), float("-inf")}
    if isinstance(value, dict):
        return all(_finite_json(child) for child in value.values())
    if isinstance(value, list):
        return all(_finite_json(child) for child in value)
    return True


def _valid_failed_result(stdout: bytes | str) -> dict[str, Any] | None:
    """Validate the Canon-owned machine-result boundary for a failed run.

    The background runner and foreground transport call this same seam. It
    accepts only the closed result envelope; arbitrary process output remains
    a generic bounded error.
    """
    value = _decode_object(stdout)
    if value is None or not _finite_json(value):
        return None
    if set(value) - (_RESULT_REQUIRED_KEYS | _RESULT_OPTIONAL_KEYS):
        return None
    if not _RESULT_REQUIRED_KEYS.issubset(value) or value["schemaVersion"] != _RESULT_SCHEMA_VERSION:
        return None
    if value.get("status") != "failed":
        return None
    if any(not isinstance(value.get(field), str) or not value[field].strip() for field in ("runId", "threadId", "workflowRef")):
        return None
    artifact_refs = value.get("artifactRefs")
    if not isinstance(artifact_refs, list) or any(not isinstance(ref, str) or not ref for ref in artifact_refs):
        return None
    if len(set(artifact_refs)) != len(artifact_refs):
        return None
    if "checkpointId" in value or "nodeId" in value or "pendingHumanGate" in value:
        return None
    event_sequence = value.get("eventSequence")
    if event_sequence is not None and (
        not isinstance(event_sequence, list)
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in event_sequence)
    ):
        return None
    append_sequence = value.get("appendSequence")
    if append_sequence is not None and (
        not isinstance(append_sequence, list)
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in append_sequence)
    ):
        return None
    execution_facts = value.get("executionFacts")
    if execution_facts is not None:
        if not isinstance(execution_facts, dict) or set(execution_facts) - {
            "inputPublications", "profileId", "settingsDigest", "modelSelections",
            "backendSessions", "phaseAuthorities", "outputPublications", "humanGateApprovals",
        }:
            return None
        if any(
            key not in execution_facts or not isinstance(execution_facts[key], list)
            for key in ("modelSelections", "phaseAuthorities", "outputPublications", "humanGateApprovals")
        ):
            return None
        profile_id = execution_facts.get("profileId")
        if profile_id is not None and (not isinstance(profile_id, str) or not profile_id):
            return None
        settings_digest = execution_facts.get("settingsDigest")
        if settings_digest is not None and (
            not isinstance(settings_digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", settings_digest) is None
        ):
            return None
        input_publications = execution_facts.get("inputPublications")
        if input_publications is not None:
            if not isinstance(input_publications, list):
                return None
            for publication in input_publications:
                if not isinstance(publication, dict) or set(publication) != {
                    "portId", "logicalArtifactRef", "version", "deliveryMode",
                }:
                    return None
                if any(
                    not isinstance(publication.get(field), str) or not publication[field]
                    for field in ("portId", "logicalArtifactRef")
                ):
                    return None
                version = publication.get("version")
                if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                    return None
                if publication.get("deliveryMode") not in {"inline-candidate", "reference-only"}:
                    return None
    return value


def _valid_result(stdout: bytes | str) -> dict[str, Any] | None:
    """Parse one finite JSON object for the ordinary success path."""
    value = _decode_object(stdout)
    return value if value is not None and _finite_json(value) else None


def _valid_command_result(stdout: bytes | str) -> dict[str, Any] | list[Any] | None:
    """Parse one finite generic JSON object or list for transport commands."""
    value = _decode_json(stdout)
    return value if isinstance(value, (dict, list)) and _finite_json(value) else None


def _valid_human_gate_prompt(prompt: Any, enclosing_result: Any) -> bool:
    """Validate Canon's closed Human Gate wake prompt against its result."""

    if not isinstance(enclosing_result, dict) or not _finite_json(enclosing_result):
        return False
    if enclosing_result.get("status") != "paused":
        return False
    if not isinstance(prompt, dict) or set(prompt) != _HUMAN_GATE_PROMPT_KEYS or not _finite_json(prompt):
        return False

    def non_empty_string(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def positive_integer(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    if prompt.get("type") != "canon_human_gate_prompt" or prompt.get("status") != "paused":
        return False
    if prompt.get("instruction") != _HUMAN_GATE_WAKE_INSTRUCTION:
        return False
    if prompt.get("actions") != _HUMAN_GATE_ACTIONS:
        return False
    if not all(
        non_empty_string(prompt.get(field))
        for field in ("workflowRef", "gateId", "instruction", "subjectDigest", "cardBody", "cardBodyDigest")
    ):
        return False
    resume_handle = prompt.get("resumeHandle")
    if not isinstance(resume_handle, dict) or set(resume_handle) != {
        "runId", "threadId", "checkpointId", "nodeId",
    } or not all(non_empty_string(resume_handle.get(field)) for field in resume_handle):
        return False
    if not positive_integer(prompt.get("subjectRevision")) or not positive_integer(prompt.get("decisionAttempt")):
        return False
    if re.fullmatch(r"sha256:[a-f0-9]{64}", prompt["subjectDigest"]) is None:
        return False
    if re.fullmatch(r"sha256:[a-f0-9]{64}", prompt["cardBodyDigest"]) is None:
        return False
    if prompt["cardBodyDigest"] != "sha256:" + hashlib.sha256(prompt["cardBody"].encode("utf-8")).hexdigest():
        return False

    pending = enclosing_result.get("pendingHumanGate")
    if not isinstance(pending, dict):
        return False
    authority = pending.get("subjectAuthority")
    if not isinstance(authority, dict):
        return False
    if pending.get("actions") != _HUMAN_GATE_ACTIONS:
        return False
    if not non_empty_string(enclosing_result.get("runId")):
        return False
    if not non_empty_string(enclosing_result.get("threadId")):
        return False
    if not non_empty_string(enclosing_result.get("workflowRef")):
        return False
    if not non_empty_string(enclosing_result.get("checkpointId")):
        return False
    if not non_empty_string(enclosing_result.get("nodeId")):
        return False
    if not non_empty_string(pending.get("gateId")) or not positive_integer(pending.get("decisionAttempt")):
        return False
    if not non_empty_string(pending.get("nodeId")) or pending["nodeId"] != enclosing_result["nodeId"]:
        return False
    if not non_empty_string(authority.get("gateId")) or authority.get("gateId") != pending.get("gateId"):
        return False
    if not positive_integer(authority.get("subjectRevision")):
        return False
    if not non_empty_string(authority.get("subjectDigest")) or re.fullmatch(
        r"sha256:[a-f0-9]{64}", authority["subjectDigest"],
    ) is None:
        return False
    if not positive_integer(authority.get("decisionAttempt")):
        return False
    if authority["decisionAttempt"] != pending["decisionAttempt"]:
        return False

    return (
        prompt["workflowRef"] == enclosing_result["workflowRef"]
        and prompt["gateId"] == pending["gateId"]
        and resume_handle == {
            "runId": enclosing_result["runId"],
            "threadId": enclosing_result["threadId"],
            "checkpointId": enclosing_result["checkpointId"],
            "nodeId": enclosing_result["nodeId"],
        }
        and prompt["subjectRevision"] == authority["subjectRevision"]
        and prompt["subjectDigest"] == authority["subjectDigest"]
        and prompt["decisionAttempt"] == pending["decisionAttempt"]
    )


def _run(manifest: dict[str, Any]) -> int:
    global _child
    operation = manifest.get("operation", "launch")
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, _signal_handler)
    try:
        _child = subprocess.Popen(
            manifest["argv"],
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, _stderr = _child.communicate(timeout=manifest["timeoutSeconds"])
        except subprocess.TimeoutExpired:
            _terminate_child()
            stdout, _stderr = _child.communicate()
            _bounded_error("Canon process timed out")
            return 124
        if _child.returncode != 0:
            if operation not in {"command", "observe"}:
                result = _valid_failed_result(stdout)
                if result is not None:
                    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
                    sys.stdout.flush()
                    return int(_child.returncode)
            _bounded_error("Canon process failed")
            return int(_child.returncode)
        if operation in {"command", "observe"}:
            output = _valid_command_result(stdout)
            if output is None:
                _bounded_error("Canon stdout framing is invalid")
                return 125
        else:
            result = _valid_result(stdout)
            if result is None:
                _bounded_error("Canon stdout framing is invalid")
                return 125
            output = result
        if operation != "observe" and isinstance(output, dict) and "pendingHumanGate" in output:
            prompt = output.get("humanGatePrompt")
            if not _valid_human_gate_prompt(prompt, output):
                _bounded_error("Canon Human Gate prompt is invalid")
                return 125
            serialized_prompt = json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))
            if len(serialized_prompt) > _MAX_COMPLETION_CHARS:
                _bounded_error("Canon Human Gate prompt exceeds completion boundary")
                return 125
            output = prompt
        sys.stdout.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return 0
    except (OSError, ValueError):
        _bounded_error("Canon process could not start")
        return 125
    finally:
        _child = None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 125
    manifest_path = Path(argv[1])
    cleanup_root = _owned_manifest_root(manifest_path)
    if cleanup_root is None:
        return 125
    try:
        loaded = _read_manifest(manifest_path)
        if loaded is None:
            return 125
        manifest, declared_root = loaded
        if declared_root != cleanup_root:
            return 125
        return _run(manifest)
    finally:
        shutil.rmtree(cleanup_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
