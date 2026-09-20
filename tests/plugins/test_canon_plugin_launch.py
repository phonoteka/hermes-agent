"""Bounded executable proof for the ordinary S07 Canon launch plugin."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import inspect
import json
import os
from pathlib import Path
import queue
import signal
import stat
import subprocess
import sys
import textwrap
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

# Register the real terminal/process built-ins for PluginContext dispatch.
import model_tools
from hermes_cli.plugins import PluginManager, PluginContext, PluginManifest
from hermes_cli.plugins_manifest import parse_manifest_file
from plugins.canon import background_runner
from plugins.canon import register
from plugins.canon import transport


def _workflow(tmp_path: Path) -> tuple[Path, Path]:
    schema_path = tmp_path / "task-input.schema.json"
    schema_path.write_text(json.dumps({"type": "string", "pattern": "\\S"}), encoding="utf-8")
    routing_path = tmp_path / "model-routing.json"
    routing_path.write_text(
        json.dumps({
            "routingId": "s07-routing",
            "failClosed": True,
            "profiles": {"default": {"hermesProfileId": "s07-hermes"}},
        }),
        encoding="utf-8",
    )
    workflow = tmp_path / "workflow.json"
    workflow.write_text(
        json.dumps({
            "workflowId": "s07-workflow",
            "workflowInputPorts": [{
                "portId": "task",
                "schemaRef": schema_path.name,
                "logicalArtifactRef": "artifact:workflow-input/task",
                "deliveryMode": "inline-candidate",
            }],
            "policyRefs": {"modelRoutingRef": routing_path.name},
        }),
        encoding="utf-8",
    )
    return workflow, schema_path


def _run_result(
    *,
    result: object = None,
    status: str = "completed",
    pending_human_gate: dict[str, object] | None = None,
    human_gate_prompt: dict[str, object] | None = None,
) -> str:
    payload: dict[str, object] = {
        "schemaVersion": "canon-run-result.schema.v2",
        "status": status,
        "runId": "run-test",
        "threadId": "thread-test",
        "workflowRef": "workflow:s07-workflow",
        "artifactRefs": [],
        "result": result if result is not None else {},
    }
    if status == "paused":
        payload.update({"checkpointId": "checkpoint-test", "nodeId": "gate-node"})
        if pending_human_gate is not None:
            payload["pendingHumanGate"] = pending_human_gate
        if human_gate_prompt is not None:
            payload["humanGatePrompt"] = human_gate_prompt
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _full_human_gate() -> dict[str, object]:
    digest = "sha256:" + "a" * 64
    task_card = {
        "schemaVersion": "task-card.schema.v1",
        "goal": {"text": "Смоделировать проверяемое решение без потери точных источников ✅"},
        "tasks": [
            {"text": "Разобрать входные ограничения, сохранив структуру, порядок и Unicode без пересказа."},
            {"text": "Сопоставить критерии приёмки с каждым обязательством и границей результата."},
            {"text": "Подготовить доказуемую выдачу с повторяемыми ссылками на источники."},
        ],
        "deliverables": [
            {"text": "solution-model.json"},
            {"text": "acceptance-matrix.md"},
            {"text": "evidence-ledger.json"},
            {"text": "точная карточка Human Gate"},
        ],
        "constraints": [
            {"text": "Не менять смысл исходных требований."},
            {"text": "Не скрывать Unicode и порядок задач."},
            {"text": "Не принимать решение за пользователя."},
        ],
        "acceptanceCriteria": [
            {"text": "Каждая задача имеет проверяемый результат."},
            {"text": "Пользователь видит точное содержимое без пересказа."},
            {"text": "Карточка обратимо декодируется с проверкой digest."},
        ],
    }
    return {
        "schemaVersion": "human-gate-request.schema.v1",
        "gateId": "gate-1",
        "nodeId": "gate-node",
        "subjectRecordIds": ["record-1"],
        "decisionAttempt": 1,
        "subjectAuthority": {
            "gateId": "gate-1",
            "currentAcceptedRecordIds": ["record-1"],
            "subjectRevision": 1,
            "subjectDigest": digest,
            "subjectCount": 1,
            "decisionAttempt": 1,
        },
        "subjectTransport": {
            "inline": [{
                "record": {
                    "schemaVersion": "accepted-execution-record.schema.v1",
                    "recordId": "record-1",
                    "phaseContractRef": "workflow:s07-workflow#phaseContracts/review",
                    "phaseId": "review",
                    "itemId": "item-1",
                    "attempt": 1,
                    "inputValueRef": "value:input",
                    "contextValueRefs": ["value:context"],
                    "outputValueRef": "value:output",
                    "validationEvidenceRef": "value:evidence",
                },
                "input": {"prompt": "审批 ✅", "nested": {"count": 2}},
                "context": [{"role": "system", "text": "保持原样"}],
                "output": task_card,
                "validationEvidence": {"checks": [{"name": "完整性", "passed": True}]},
            }]
        },
        "cardBindingRef": "cards/review.json",
        "card": {
            "cardId": "task-card-review",
            "title": "Проверить карточку задачи",
            "actions": ["approve", "revise", "abort"],
            "presentation": {
                "format": "markdown",
                "title": {"path": "title", "style": "h1"},
                "sections": [
                    {"heading": "Цель", "path": "goal", "style": "h2"},
                    {"heading": "Задачи", "path": "tasks", "style": "h2"},
                    {"heading": "Результат", "path": "deliverables", "style": "h2"},
                    {"heading": "Ограничения", "path": "constraints", "style": "h2"},
                    {"heading": "Критерии приёмки", "path": "acceptanceCriteria", "style": "h2"},
                ],
            },
        },
        "actions": ["approve", "revise", "abort"],
        "deliveryPolicyRef": "canon://delivery/operator",
    }


_HUMAN_GATE_WAKE_INSTRUCTION = (
    "Canon-процесс остановлен на Human Gate.\n"
    "Используй clarify и передай пользователю точное содержимое карточки.\n"
    "Не пересказывай карточку и не принимай решение самостоятельно.\n"
    "После ответа пользователя передай typed decision в текущий checkpoint."
)


def _human_gate_wake_prompt(result: dict[str, object]) -> dict[str, object]:
    pending = result["pendingHumanGate"]
    assert isinstance(pending, dict)
    authority = pending["subjectAuthority"]
    assert isinstance(authority, dict)
    actions = pending["actions"]
    assert isinstance(actions, list)
    subject_transport = pending["subjectTransport"]
    assert isinstance(subject_transport, dict)
    subjects = subject_transport["inline"]
    assert isinstance(subjects, list)
    output = subjects[0]["output"]
    if isinstance(output, dict) and isinstance(output.get("draft"), str):
        body = "# Review subject\n\n## Draft\n" + output["draft"]
    else:
        body = (
            "# Проверить карточку задачи\n\n"
            "## Цель\nСформировать карточку задачи\n\n"
            "## Задачи\n- Уточнить задачу\n\n"
            "## Результат\n- Карточка задачи\n\n"
            "## Ограничения\n- Не пересказывать задачу\n\n"
            "## Критерии приёмки\n- Карточка проверяема"
        )
    return {
        "type": "canon_human_gate_prompt",
        "status": "paused",
        "workflowRef": result["workflowRef"],
        "gateId": pending["gateId"],
        "instruction": _HUMAN_GATE_WAKE_INSTRUCTION,
        "actions": actions,
        "resumeHandle": {
            "runId": result["runId"],
            "threadId": result["threadId"],
            "checkpointId": result["checkpointId"],
            "nodeId": result["nodeId"],
        },
        "subjectRevision": authority["subjectRevision"],
        "subjectDigest": authority["subjectDigest"],
        "decisionAttempt": pending["decisionAttempt"],
        "cardBody": body,
        "cardBodyDigest": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def _deterministic_entropy_text(label: str, length: int) -> str:
    chunks: list[str] = []
    index = 0
    while sum(len(chunk) for chunk in chunks) < length:
        chunks.append(hashlib.sha256(f"{label}:{index}".encode("utf-8")).hexdigest())
        index += 1
    return "".join(chunks)[:length]


def _ready_human_gate_prompt(result: dict[str, object]) -> dict[str, object]:
    body = (
        "# Проверить карточку задачи\n\n"
        "## Цель\nСформировать карточку задачи\n\n"
        "## Задачи\n- Уточнить задачу\n\n"
        "## Результат\n- Карточка задачи\n\n"
        "## Ограничения\n- Не пересказывать задачу\n\n"
        "## Критерии приёмки\n- Карточка проверяема"
    )
    pending = result["pendingHumanGate"]
    assert isinstance(pending, dict)
    authority = pending["subjectAuthority"]
    assert isinstance(authority, dict)
    actions = pending["actions"]
    assert isinstance(actions, list)
    return {
        "type": "canon_human_gate_prompt",
        "status": "paused",
        "workflowRef": result["workflowRef"],
        "gateId": pending["gateId"],
        "instruction": (
            "Canon-процесс остановлен на Human Gate.\n"
            "Используй clarify и передай пользователю точное содержимое карточки.\n"
            "Не пересказывай карточку и не принимай решение самостоятельно.\n"
            "После ответа пользователя передай typed decision в текущий checkpoint."
        ),
        "actions": actions,
        "resumeHandle": {
            "runId": result["runId"],
            "threadId": result["threadId"],
            "checkpointId": result["checkpointId"],
            "nodeId": result["nodeId"],
        },
        "subjectRevision": authority["subjectRevision"],
        "subjectDigest": authority["subjectDigest"],
        "decisionAttempt": pending["decisionAttempt"],
        "cardBody": body,
        "cardBodyDigest": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def _fake_canon(tmp_path: Path, *, output: str | None = None, code: int = 0, delay: float = 0) -> Path:
    if output is None:
        output = _run_result()
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "fake-canon"
    script.write_text(
        textwrap.dedent(
            f"""
            #!/usr/bin/env python3
            import sys, time
            time.sleep({delay!r})
            sys.stdout.write({output!r})
            raise SystemExit({code})
            """
        ).lstrip(),
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


def _manifest(root: Path, argv: list[str], timeout: int = 5, operation: str = "launch") -> Path:
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    path = root / "manifest.json"
    path.write_text(json.dumps({
        "schemaVersion": "canon-plugin-background-manifest.v1",
        "operation": operation,
        "argv": argv,
        "timeoutSeconds": timeout,
        "cleanupRoot": str(root),
    }), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_human_gate_prompt_forwards_ready_card_body_and_rejects_card_payload_only(
    tmp_path: Path,
) -> None:
    pending = _full_human_gate()
    canon_result = json.loads(_run_result(status="paused", pending_human_gate=pending))
    ready_prompt = _ready_human_gate_prompt(canon_result)
    serialized_ready = json.dumps(ready_prompt, ensure_ascii=False, separators=(",", ":"))
    assert len(serialized_ready) <= 2_000

    ready_child = _fake_canon(
        tmp_path / "ready-card-body",
        output=_run_result(
            status="paused",
            pending_human_gate=pending,
            human_gate_prompt=ready_prompt,
        ),
    )
    ready_root = tmp_path / "runtime" / "canon" / "hermes-canon-ready-card-body"
    ready_result = subprocess.run(
        [sys.executable, str(_runner()), str(_manifest(ready_root, [str(ready_child)], operation="command"))],
        capture_output=True,
        shell=False,
    )
    assert ready_result.returncode == 0
    assert ready_result.stdout == serialized_ready.encode("utf-8") + b"\n"
    forwarded = json.loads(ready_result.stdout)
    assert forwarded["cardBody"] == ready_prompt["cardBody"]
    assert forwarded["cardBodyDigest"] == ready_prompt["cardBodyDigest"]
    assert forwarded["cardBodyDigest"] == "sha256:" + hashlib.sha256(
        forwarded["cardBody"].encode("utf-8")
    ).hexdigest()
    assert "cardPayload" not in forwarded
    assert ready_result.stderr == b""
    assert not ready_root.exists()

    payload_pending = canon_result["pendingHumanGate"]
    assert isinstance(payload_pending, dict)
    payload_transport = payload_pending["subjectTransport"]
    assert isinstance(payload_transport, dict)
    payload_subjects = payload_transport["inline"]
    assert isinstance(payload_subjects, list)
    payload_subjects[0]["output"] = {}
    payload_pending["card"] = {"cardId": "x", "title": "x", "actions": ["approve", "revise", "abort"]}
    payload_pending["cardBindingRef"] = "x"
    payload_pending["deliveryPolicyRef"] = "x"
    payload_only = _human_gate_wake_prompt(canon_result)
    payload_only.pop("cardBody")
    payload_only.pop("cardBodyDigest")
    payload_only["cardPayload"] = {"encoding": "legacy", "data": "payload-only"}
    assert len(json.dumps(payload_only, ensure_ascii=False, separators=(",", ":"))) <= 2_000
    payload_child = _fake_canon(
        tmp_path / "payload-only",
        output=_run_result(
            status="paused",
            pending_human_gate=pending,
            human_gate_prompt=payload_only,
        ),
    )
    payload_root = tmp_path / "runtime" / "canon" / "hermes-canon-payload-only"
    payload_result = subprocess.run(
        [sys.executable, str(_runner()), str(_manifest(payload_root, [str(payload_child)], operation="command"))],
        capture_output=True,
        shell=False,
    )
    assert payload_result.returncode == 125
    assert "error" in json.loads(payload_result.stdout)
    assert b"cardPayload" not in payload_result.stdout
    assert payload_result.stderr == b""
    assert not payload_root.exists()


def test_current_canon_accepted_task_card_prompt_crosses_hermes_boundary_without_artifact(
    tmp_path: Path,
) -> None:
    pending = _full_human_gate()
    authority = pending["subjectAuthority"]
    assert isinstance(authority, dict)
    digest = authority["subjectDigest"]
    runtime_result = {
        "status": "paused",
        "runId": "run-test",
        "threadId": "thread-test",
        "workflowRef": "workflow:s07-workflow",
        "result": {},
        "resumeHandle": {
            "runId": "run-test",
            "threadId": "thread-test",
            "checkpointId": "checkpoint-test",
            "nodeId": "gate-node",
            "subjectRevision": 1,
            "subjectDigest": digest,
            "decisionAttempt": 1,
        },
        "pendingHumanGate": pending,
    }
    fixture = tmp_path / "accepted-task-card-runtime-result.json"
    fixture.write_text(json.dumps(runtime_result, ensure_ascii=False), encoding="utf-8")
    canon_root = Path(__file__).resolve().parents[4] / "projects" / "canon"
    producer_script = """
import json
import sys

from canon.operator_observability import build_run_result

with open(sys.argv[1], encoding="utf-8") as stream:
    runtime_result = json.load(stream)
print(json.dumps(build_run_result(runtime_result), ensure_ascii=False, separators=(",", ":")))
"""
    producer = subprocess.run(
        [str(canon_root / ".venv" / "bin" / "python"), "-c", producer_script, str(fixture)],
        cwd=canon_root,
        env={**os.environ, "PYTHONPATH": str(canon_root / "src")},
        capture_output=True,
        text=True,
        shell=False,
    )
    assert producer.returncode == 0, producer.stderr
    assert producer.stderr == ""
    canon_result = json.loads(producer.stdout)
    assert canon_result["status"] == "paused"
    assert canon_result["artifactRefs"] == []
    produced_pending = canon_result["pendingHumanGate"]
    assert len(produced_pending["subjectTransport"]["inline"]) == 1
    produced_prompt = canon_result["humanGatePrompt"]
    expected_body = (
        "# Проверить карточку задачи\n\n"
        "## Цель\nСмоделировать проверяемое решение без потери точных источников ✅\n\n"
        "## Задачи\n"
        "- Разобрать входные ограничения, сохранив структуру, порядок и Unicode без пересказа.\n"
        "- Сопоставить критерии приёмки с каждым обязательством и границей результата.\n"
        "- Подготовить доказуемую выдачу с повторяемыми ссылками на источники.\n\n"
        "## Результат\n"
        "- solution-model.json\n"
        "- acceptance-matrix.md\n"
        "- evidence-ledger.json\n"
        "- точная карточка Human Gate\n\n"
        "## Ограничения\n"
        "- Не менять смысл исходных требований.\n"
        "- Не скрывать Unicode и порядок задач.\n"
        "- Не принимать решение за пользователя.\n\n"
        "## Критерии приёмки\n"
        "- Каждая задача имеет проверяемый результат.\n"
        "- Пользователь видит точное содержимое без пересказа.\n"
        "- Карточка обратимо декодируется с проверкой digest."
    )
    assert produced_prompt["cardBody"] == expected_body
    serialized_prompt = json.dumps(produced_prompt, ensure_ascii=False, separators=(",", ":"))
    child = _fake_canon(tmp_path / "current-producer-result", output=producer.stdout)
    root = tmp_path / "runtime" / "canon" / "hermes-canon-current-producer-result"
    result = subprocess.run(
        [sys.executable, str(_runner()), str(_manifest(root, [str(child)], operation="command"))],
        capture_output=True,
        shell=False,
    )
    assert result.returncode == 0
    assert result.stderr == b""
    forwarded = json.loads(result.stdout)
    assert forwarded == produced_prompt
    assert result.stdout == serialized_prompt.encode("utf-8") + b"\n"
    assert forwarded["subjectRevision"] == produced_pending["subjectAuthority"]["subjectRevision"]
    assert forwarded["subjectDigest"] == produced_pending["subjectAuthority"]["subjectDigest"]
    assert forwarded["cardBodyDigest"] == "sha256:" + hashlib.sha256(expected_body.encode("utf-8")).hexdigest()
    assert "cardPayload" not in forwarded
    assert "artifactRefs" not in forwarded
    assert not root.exists()


def test_runner_rejects_changed_or_missing_card_body_digest_and_cleans_root(tmp_path: Path) -> None:
    body = "# Review\n\n## Items\n- bounded"
    valid_prompt = {
        "type": "canon_human_gate_prompt",
        "status": "paused",
        "workflowRef": "workflow:s07-workflow",
        "gateId": "gate-1",
        "instruction": _HUMAN_GATE_WAKE_INSTRUCTION,
        "actions": ["approve", "revise", "abort"],
        "resumeHandle": {
            "runId": "run-test",
            "threadId": "thread-test",
            "checkpointId": "checkpoint-test",
            "nodeId": "gate-node",
        },
        "subjectRevision": 1,
        "subjectDigest": "sha256:" + "a" * 64,
        "decisionAttempt": 1,
        "cardBody": body,
        "cardBodyDigest": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    serialized_valid = json.dumps(valid_prompt, ensure_ascii=False, separators=(",", ":"))
    assert len(serialized_valid) <= 2_000

    pending = _full_human_gate()
    variants = {
        "changed": {**valid_prompt, "cardBodyDigest": "sha256:" + "b" * 64},
        "missing": {key: value for key, value in valid_prompt.items() if key != "cardBodyDigest"},
    }
    for name, prompt in variants.items():
        child = _fake_canon(
            tmp_path / f"digest-{name}",
            output=_run_result(
                status="paused",
                pending_human_gate=pending,
                human_gate_prompt=prompt,
            ),
        )
        root = tmp_path / "runtime" / "canon" / f"hermes-canon-digest-{name}"
        result = subprocess.run(
            [sys.executable, str(_runner()), str(_manifest(root, [str(child)], operation="command"))],
            capture_output=True,
            shell=False,
        )
        assert result.returncode == 125
        assert result.stderr == b""
        assert len(result.stdout) <= 2_000
        bounded = json.loads(result.stdout)
        assert set(bounded) == {"error"}
        assert b"cardBody" not in result.stdout
        assert b"cardPayload" not in result.stdout
        assert not root.exists()


@pytest.mark.parametrize(
    ("variant", "path", "value"),
    [
        ("wrong_type", ("type",), "not_canon_human_gate_prompt"),
        ("wrong_status", ("status",), "completed"),
        ("wrong_instruction", ("instruction",), "Use the card."),
        ("actions_order", ("actions",), ["revise", "approve", "abort"]),
        ("actions_subset", ("actions",), ["approve"]),
        ("wrong_workflow_ref", ("workflowRef",), "workflow:other"),
        ("wrong_gate_id", ("gateId",), "gate-other"),
        ("wrong_resume_run_id", ("resumeHandle", "runId"), "run-other"),
        ("wrong_resume_thread_id", ("resumeHandle", "threadId"), "thread-other"),
        ("wrong_resume_checkpoint_id", ("resumeHandle", "checkpointId"), "checkpoint-other"),
        ("wrong_resume_node_id", ("resumeHandle", "nodeId"), "node-other"),
        ("wrong_subject_revision", ("subjectRevision",), 2),
        ("wrong_subject_digest", ("subjectDigest",), "sha256:" + "b" * 64),
        ("wrong_decision_attempt", ("decisionAttempt",), 2),
    ],
)
def test_runner_rejects_nonexact_closed_prompt_and_cleans_root(
    variant: str,
    path: tuple[str, ...],
    value: object,
    tmp_path: Path,
) -> None:
    pending = _full_human_gate()
    canon_result = json.loads(_run_result(status="paused", pending_human_gate=pending))
    prompt = deepcopy(_ready_human_gate_prompt(canon_result))
    target: Any = prompt
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    raw_result = _run_result(
        status="paused",
        pending_human_gate=pending,
        human_gate_prompt=prompt,
    )
    child = _fake_canon(tmp_path / f"closed-prompt-{variant}", output=raw_result)
    root = tmp_path / "runtime" / "canon" / f"hermes-canon-closed-prompt-{variant}"
    result = subprocess.run(
        [sys.executable, str(_runner()), str(_manifest(root, [str(child)], operation="command"))],
        capture_output=True,
        shell=False,
    )
    assert result.returncode == 125
    assert result.stderr == b""
    assert len(result.stdout) <= 2_000
    assert set(json.loads(result.stdout)) == {"error"}
    assert b"cardBody" not in result.stdout
    assert b"cardPayload" not in result.stdout
    assert not root.exists()


def _prompt_with_serialized_length(result: dict[str, object], target: int) -> dict[str, object]:
    prompt = _ready_human_gate_prompt(result)
    prompt["cardBody"] = ""
    prompt["cardBodyDigest"] = "sha256:" + hashlib.sha256(b"").hexdigest()
    empty_length = len(json.dumps(prompt, ensure_ascii=False, separators=(",", ":")))
    prompt["cardBody"] = _deterministic_entropy_text(f"completion-{target}", target - empty_length)
    prompt["cardBodyDigest"] = "sha256:" + hashlib.sha256(prompt["cardBody"].encode("utf-8")).hexdigest()
    serialized = json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))
    assert len(serialized) == target
    assert prompt["cardBody"]
    return prompt


@pytest.mark.parametrize(
    ("name", "serialized_length", "expected_code"),
    [("below", 1_999, 0), ("exact", 2_000, 0), ("over", 2_001, 125)],
)
def test_completion_line_boundary_is_exact_serialized_prompt_length(
    name: str,
    serialized_length: int,
    expected_code: int,
    tmp_path: Path,
) -> None:
    pending = _full_human_gate()
    canon_result = json.loads(_run_result(status="paused", pending_human_gate=pending))
    prompt = _prompt_with_serialized_length(canon_result, serialized_length)
    serialized_prompt = json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))
    assert len(serialized_prompt) == serialized_length
    raw_result = _run_result(
        status="paused",
        pending_human_gate=pending,
        human_gate_prompt=prompt,
    )
    child = _fake_canon(tmp_path / f"completion-{name}", output=raw_result)
    root = tmp_path / "runtime" / "canon" / f"hermes-canon-completion-{name}"
    result = subprocess.run(
        [sys.executable, str(_runner()), str(_manifest(root, [str(child)], operation="command"))],
        capture_output=True,
        shell=False,
    )
    assert result.returncode == expected_code
    assert result.stderr == b""
    assert not root.exists()
    if expected_code == 0:
        assert result.stdout == serialized_prompt.encode("utf-8") + b"\n"
    else:
        assert json.loads(result.stdout) == {"error": "Canon Human Gate prompt exceeds completion boundary"}
        assert len(result.stdout) <= 2_000


def _runner() -> Path:
    return Path(transport.__file__).with_name("background_runner.py")


def test_background_runner_is_python_source_not_executable_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_path = tmp_path / "transport.py"
    module_path.write_text("", encoding="utf-8")
    runner = tmp_path / "background_runner.py"
    runner.write_text("print('ok')\n", encoding="utf-8")
    runner.chmod(0o600)
    monkeypatch.setattr(transport, "__file__", str(module_path))

    assert transport._runner_path() == runner.resolve()


def test_background_runner_skips_unavailable_posix_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[int] = []

    class Child:
        returncode = 0

        def communicate(self, timeout=None):
            return _run_result().encode("utf-8"), b""

        def poll(self):
            return 0

    monkeypatch.delattr(background_runner.signal, "SIGHUP")
    monkeypatch.setattr(
        background_runner.signal,
        "signal",
        lambda signum, _handler: installed.append(signum),
    )
    monkeypatch.setattr(
        background_runner.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Child(),
    )

    result = background_runner._run({"argv": ["canon"], "timeoutSeconds": 1})

    assert result == 0
    assert installed == [signal.SIGTERM, signal.SIGINT]


def test_registers_exactly_four_async_canon_tools_with_closed_schemas() -> None:
    calls = []

    class Context:
        def register_tool(self, **kwargs):
            calls.append(kwargs)

    register(Context())
    assert len(calls) == 4
    assert [call["name"] for call in calls] == [
        "canon_gateway_launch", "canon_gateway_checkpoint",
        "canon_workflow_observe", "canon_workflow_control",
    ]
    for call in calls:
        assert call["toolset"] == "canon"
        assert call["is_async"] is True
        assert call["schema"]["parameters"]["additionalProperties"] is False
    call = calls[0]
    assert call["name"] == "canon_gateway_launch"
    assert set(call["schema"]["parameters"]["properties"]) == {
        "workflow_name", "workflow_input", "execution_root", "timeout_seconds", "wait_mode"
    }


def test_observe_argv_routes_all_surfaces_to_parser_valid_canon_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: ["canon"])
    root = tmp_path / "durable"
    root.mkdir()
    cases = {
        "latest": ({"surface": "latest"}, "index"),
        "list": ({"surface": "list", "limit": 4, "offset": 2}, "index"),
        "full": ({"surface": "full", "run_id": "run-1"}, "inspect"),
        "inspect": ({"surface": "inspect", "run_id": "run-1"}, "inspect"),
        "report": ({"surface": "report", "run_id": "run-1"}, "inspect"),
        "timeline": ({"surface": "timeline", "run_id": "run-1", "after_sequence": 3, "limit": 2}, "inspect"),
        "artifacts": ({"surface": "artifacts", "run_id": "run-1"}, "inspect"),
        "events": ({"surface": "events", "run_id": "run-1", "after_sequence": 3, "limit": 2}, "inspect"),
    }
    for surface, (args, command) in cases.items():
        argv = transport._observe_argv(args, root)
        assert argv[:5] == ["canon", command, "--mode", "production", "--artifact-backend"]
        if command == "inspect":
            assert argv[argv.index("--surface") + 1] == surface
            assert argv[argv.index("--run-id") + 1] == "run-1"
            assert argv[0] == "canon"
        else:
            assert "--surface" not in argv
        assert "--mode" in argv and "--artifact-backend" in argv and "--journal-backend" in argv


def test_observe_argv_rejects_invalid_conditional_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: ["canon"])
    root = tmp_path / "durable"
    root.mkdir()
    invalid = [
        {"surface": "latest", "run_id": "run-1"},
        {"surface": "latest", "limit": 2},
        {"surface": "list", "after_sequence": 1},
        {"surface": "report", "run_id": "run-1", "limit": 1},
        {"surface": "timeline", "run_id": "run-1", "offset": 1},
        {"surface": "events"},
    ]
    for args in invalid:
        with pytest.raises(transport.CanonTransportError):
            transport._observe_argv(args, root)


@pytest.mark.parametrize("surface", ["latest", "list", "full", "inspect", "report", "timeline", "artifacts", "events"])
def test_handle_observe_dispatches_every_declared_surface(
    surface: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "durable"
    root.mkdir()
    monkeypatch.setattr(transport, "_profile_durable_root", lambda: root)
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: ["canon"])
    captured: list[tuple[list[str], str]] = []

    def run_cli(_ctx, argv, *, root, timeout, task_id=None, operation="command"):
        captured.append((argv, operation))
        return {"result": []}

    monkeypatch.setattr(transport, "_run_cli", run_cli)
    args = {"surface": surface}
    if surface not in {"latest", "list"}:
        args["run_id"] = "run-1"
    result = json.loads(asyncio.run(transport.handle_observe(args, ctx=object())))
    assert result == {"result": []}
    assert len(captured) == 1
    assert captured[0][0][1] == ("index" if surface in {"latest", "list"} else "inspect")
    assert captured[0][1] == "observe"


def test_checkpoint_schema_and_transport_reject_delivery_targeting_and_observe_combinations(tmp_path: Path) -> None:
    schema = transport.CANON_GATEWAY_CHECKPOINT_SCHEMA["parameters"]
    assert {"chat_id", "target_thread_id", "thread_id_target"}.isdisjoint(schema["properties"])
    with pytest.raises(transport.CanonTransportError):
        transport._checkpoint_argv(
            {"workflow_name": "workflow.json", "mode": "history", "run_id": "run-1", "thread_id": "thread-1", "chat_id": "chat"},
            Path("/tmp"),
        )
    observe = transport.CANON_WORKFLOW_OBSERVE_SCHEMA["parameters"]
    assert observe["additionalProperties"] is False
    assert len(observe["allOf"]) == 4
    observe_validator = Draft202012Validator(observe)
    for valid in (
        {"surface": "latest"},
        {"surface": "latest", "limit": 1},
        {"surface": "list", "limit": 4, "offset": 2},
        {"surface": "full", "run_id": "run-1"},
        {"surface": "inspect", "run_id": "run-1"},
        {"surface": "report", "run_id": "run-1"},
        {"surface": "artifacts", "run_id": "run-1"},
        {"surface": "timeline", "run_id": "run-1", "after_sequence": 0, "limit": 1},
        {"surface": "events", "run_id": "run-1", "after_sequence": 0, "limit": 1},
    ):
        observe_validator.validate(valid)
    for invalid in (
        {"surface": "latest", "limit": 2},
        {"surface": "latest", "run_id": "run-1"},
        {"surface": "list", "after_sequence": 1},
        {"surface": "full", "run_id": "run-1", "limit": 1},
        {"surface": "timeline", "run_id": "run-1", "offset": 1},
        {"surface": "events"},
        {"surface": "report", "run_id": ""},
    ):
        with pytest.raises(JsonSchemaValidationError):
            observe_validator.validate(invalid)
    checkpoint_validator = Draft202012Validator(schema)
    for field in ("chat_id", "target_thread_id", "thread_id_target"):
        with pytest.raises(JsonSchemaValidationError):
            checkpoint_validator.validate({
                "workflow_name": "workflow.json",
                "mode": "history",
                "run_id": "run-1",
                "thread_id": "thread-1",
                field: "forbidden",
            })
    manager = PluginManager(scope_key=str(tmp_path))
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "canon"
    manifest = parse_manifest_file(plugin_dir / "plugin.yaml", plugin_dir, "bundled", "")
    assert manifest is not None
    try:
        manager._load_plugin(manifest)
        assert manager._plugins["canon"].enabled is True
        expected = [
            "canon_gateway_launch", "canon_gateway_checkpoint",
            "canon_workflow_observe", "canon_workflow_control",
        ]
        assert manager._plugins["canon"].tools_registered == expected
        assert manager._plugin_tool_names == set(expected)
    finally:
        manager.unload()


def test_closed_arguments_reject_unknown_keys_without_context(tmp_path: Path) -> None:
    workflow, _ = _workflow(tmp_path)
    result = json.loads(asyncio.run(transport.handle_tool({
        "workflow_name": str(workflow),
        "workflow_input": {"task": "x"},
        "execution_root": str(tmp_path),
        "session_key": "forbidden",
    })))
    assert "unsupported tool arguments" in result["error"]
    assert "session_key" in result["error"]


def test_transport_does_not_mint_or_forward_run_or_thread_identity(tmp_path: Path) -> None:
    workflow, _ = _workflow(tmp_path)
    request = transport.build_run_request(
        workflow_path=workflow,
        workflow_input={"task": "x"},
        execution_root=tmp_path,
    )
    assert "runId" not in request
    assert "threadId" not in request


def test_carriers_are_private_outside_execution_root_and_cleanup_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow, _ = _workflow(tmp_path)
    execution = tmp_path / "execution"
    execution.mkdir()
    profile = tmp_path / "profile-home"
    profile.mkdir()
    monkeypatch.setattr(transport, "get_hermes_home", lambda: profile)
    monkeypatch.setattr(transport, "get_default_hermes_root", lambda: profile)
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: ["/bin/true"])

    calls = []

    class Context:
        def dispatch_tool(self, name, args, **kwargs):
            calls.append((name, args, kwargs))
            return json.dumps({
                "session_id": "native-bad",
                "exit_code": 0,
                "notify_on_complete": False,
            }) if name == "terminal" else json.dumps({"status": "killed"})

    result = json.loads(asyncio.run(transport.handle_tool({
        "workflow_name": str(workflow), "workflow_input": {"task": "x"},
        "execution_root": str(execution), "wait_mode": "async-no-wait",
    }, ctx=Context())))
    assert "error" in result
    assert [item[0] for item in calls] == ["terminal", "process_manage"]
    private = profile / "runtime" / "canon"
    assert private.is_dir()
    assert not any(private.glob("hermes-canon-*/manifest.json"))


def test_private_root_does_not_change_existing_profile_or_runtime_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "profile-home"
    runtime = profile / "runtime"
    runtime.mkdir(parents=True)
    profile.chmod(0o750)
    runtime.chmod(0o751)
    monkeypatch.setattr(transport, "get_hermes_home", lambda: profile)
    root = transport._profile_runtime_parent()
    assert stat.S_IMODE(profile.stat().st_mode) == 0o750
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o751
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_runner_executes_exact_argv_shell_free_and_cleans_root(tmp_path: Path) -> None:
    marker = tmp_path / "shell-marker"
    argument = f"safe;touch {marker}"
    probe = tmp_path / "argv-probe"
    probe.write_text(
        "#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps({"
        "'schemaVersion':'canon-run-result.schema.v2','status':'completed',"
        "'runId':'run-shell','threadId':'thread-shell','workflowRef':'workflow:shell',"
        "'artifactRefs':[],'result':{'arg':sys.argv[1]}}))\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    root = tmp_path / "runtime" / "canon" / "hermes-canon-shell"
    manifest = _manifest(root, [str(probe), argument])
    result = subprocess.run([sys.executable, str(_runner()), str(manifest)], capture_output=True, shell=False)
    assert result.returncode == 0
    assert json.loads(result.stdout)["result"]["arg"] == argument
    assert not marker.exists()
    assert not root.exists()


@pytest.mark.parametrize("kind", ["object", "list"])
def test_command_manifest_executes_exact_argv_and_preserves_generic_json(
    kind: str, tmp_path: Path,
) -> None:
    marker = tmp_path / f"command-shell-marker-{kind}"
    argument = f"safe;touch {marker}"
    probe = tmp_path / f"command-probe-{kind}"
    probe.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        f"value = {{'kind': {kind!r}, 'arg': sys.argv[1]}} if {kind!r} == 'object' else [{kind!r}, sys.argv[1]]\n"
        "sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(',', ':')))\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    root = tmp_path / "runtime" / "canon" / f"hermes-canon-command-{kind}"
    manifest = _manifest(root, [str(probe), argument], operation="command")
    result = subprocess.run(
        [sys.executable, str(_runner()), str(manifest)], capture_output=True, shell=False,
    )

    expected = (
        {"kind": kind, "arg": argument}
        if kind == "object" else [kind, argument]
    )
    assert result.returncode == 0
    assert result.stdout == (
        json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    )
    assert result.stderr == b""
    assert not marker.exists()
    assert not root.exists()


def test_observe_manifest_preserves_pending_human_gate_report(
    tmp_path: Path,
) -> None:
    pending = _full_human_gate()
    raw_result = _run_result(status="paused", pending_human_gate=pending)
    expected_report = json.loads(raw_result)
    assert "pendingHumanGate" in expected_report
    assert "humanGatePrompt" not in expected_report

    child = _fake_canon(tmp_path / "observe-report", output=raw_result)
    root = tmp_path / "runtime" / "canon" / "hermes-canon-observe-report"
    result = subprocess.run(
        [sys.executable, str(_runner()), str(_manifest(root, [str(child)], operation="observe"))],
        capture_output=True,
        shell=False,
    )

    assert result.returncode == 0
    assert result.stdout == json.dumps(
        expected_report, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    assert json.loads(result.stdout) == expected_report
    assert result.stderr == b""
    assert not root.exists()


def test_command_repaused_human_gate_emits_bounded_prompt_and_cleans_root(tmp_path: Path) -> None:
    pending = _full_human_gate()
    subject_transport = pending["subjectTransport"]
    assert isinstance(subject_transport, dict)
    subjects = subject_transport["inline"]
    assert isinstance(subjects, list)
    subjects[0]["output"] = {"value": "small"}
    canon_result = json.loads(_run_result(status="paused", pending_human_gate=pending))
    expected_prompt = _human_gate_wake_prompt(canon_result)
    raw_result = _run_result(
        status="paused",
        pending_human_gate=pending,
        human_gate_prompt=expected_prompt,
    )
    expected_output = json.dumps(expected_prompt, ensure_ascii=False, separators=(",", ":"))
    assert len(expected_output) <= 2_000
    assert len(raw_result) > len(expected_output)

    child = _fake_canon(tmp_path / "command-repaused", output=raw_result)
    root = tmp_path / "runtime" / "canon" / "hermes-canon-command-repaused"
    result = subprocess.run(
        [sys.executable, str(_runner()), str(_manifest(root, [str(child)], operation="command"))],
        capture_output=True,
        shell=False,
    )

    assert result.returncode == 0
    assert result.stdout == expected_output.encode("utf-8") + b"\n"
    assert json.loads(result.stdout) == expected_prompt
    assert b"pendingHumanGate" not in result.stdout
    assert result.stderr == b""
    assert not root.exists()


def test_command_oversize_human_gate_fails_closed_and_cleans_root(tmp_path: Path) -> None:
    pending = _full_human_gate()
    subject_transport = pending["subjectTransport"]
    assert isinstance(subject_transport, dict)
    subjects = subject_transport["inline"]
    assert isinstance(subjects, list)
    subjects[0]["output"] = {
        "draft": _deterministic_entropy_text("command-oversized-human-gate", 5_000),
    }
    canon_result = json.loads(_run_result(status="paused", pending_human_gate=pending))
    expected_prompt = _human_gate_wake_prompt(canon_result)
    raw_result = _run_result(
        status="paused",
        pending_human_gate=pending,
        human_gate_prompt=expected_prompt,
    )
    expected_output = json.dumps(
        {"error": "Canon Human Gate prompt exceeds completion boundary"},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert len(json.dumps(expected_prompt, ensure_ascii=False, separators=(",", ":"))) > 2_000
    assert len(raw_result) > 2_000

    child = _fake_canon(tmp_path / "command-oversized", output=raw_result)
    root = tmp_path / "runtime" / "canon" / "hermes-canon-command-oversized"
    result = subprocess.run(
        [sys.executable, str(_runner()), str(_manifest(root, [str(child)], operation="command"))],
        capture_output=True,
        shell=False,
    )

    assert result.returncode == 125
    assert result.stdout == expected_output.encode("utf-8") + b"\n"
    assert json.loads(result.stdout) == {
        "error": "Canon Human Gate prompt exceeds completion boundary",
    }
    assert b"pendingHumanGate" not in result.stdout
    assert b"cardPayload" not in result.stdout
    assert result.stderr == b""
    assert not root.exists()


def test_command_nonzero_ignores_launch_failure_schema_and_cleans_root(tmp_path: Path) -> None:
    typed_failed = _typed_failed_result()
    typed_failed["schemaVersion"] = "canon-run-result.schema.v1"
    child = _fake_canon(
        tmp_path / "command-nonzero",
        output=json.dumps(typed_failed, ensure_ascii=False, separators=(",", ":")),
        code=7,
    )
    root = tmp_path / "runtime" / "canon" / "hermes-canon-command-nonzero"
    result = subprocess.run(
        [sys.executable, str(_runner()), str(_manifest(root, [str(child)], operation="command"))],
        capture_output=True,
        shell=False,
    )

    assert result.returncode == 7
    assert result.stdout == b'{"error":"Canon process failed"}\n'
    assert json.loads(result.stdout) == {"error": "Canon process failed"}
    assert b"canon-run-result.schema.v2" not in result.stdout
    assert result.stderr == b""
    assert not root.exists()


def test_preserves_current_v2_failed_result_on_nonzero_child_exit(tmp_path: Path) -> None:
    typed_failed = _typed_failed_result()
    serialized = json.dumps(typed_failed, ensure_ascii=False, separators=(",", ":"))
    child = _fake_canon(
        tmp_path / "v2-failed",
        output=serialized,
        code=7,
    )
    root = tmp_path / "runtime" / "canon" / "hermes-canon-v2-failed"
    result = subprocess.run(
        [sys.executable, str(_runner()), str(_manifest(root, [str(child)]))],
        capture_output=True,
        shell=False,
    )
    assert result.returncode == 7
    assert result.stdout == serialized.encode("utf-8") + b"\n"
    assert result.stderr == b""
    assert not root.exists()

    parsed = transport._parse_runner_result({
        "status": "exited",
        "exit_code": 7,
        "output": serialized,
    })
    assert parsed == typed_failed


def test_v1_failed_result_remains_rejected_as_stale_authority(tmp_path: Path) -> None:
    stale = _typed_failed_result()
    stale["schemaVersion"] = "canon-run-result.schema.v1"
    serialized = json.dumps(stale, ensure_ascii=False, separators=(",", ":"))
    child = _fake_canon(tmp_path / "v1-failed", output=serialized, code=7)
    root = tmp_path / "runtime" / "canon" / "hermes-canon-v1-failed"
    result = subprocess.run(
        [
            sys.executable,
            str(_runner()),
            str(_manifest(root, [str(child)], operation="command")),
        ],
        capture_output=True,
        shell=False,
    )
    assert result.returncode == 7
    assert result.stdout == b'{"error":"Canon process failed"}\n'
    assert b"canon-run-result.schema.v1" not in result.stdout
    assert result.stderr == b""
    assert not root.exists()


@pytest.mark.parametrize("status,code", [("completed", 0), ("failed", 0)])
def test_runner_preserves_typed_status_across_outer_exit_codes(
    status: str, code: int, tmp_path: Path,
) -> None:
    child = _fake_canon(
        tmp_path / f"typed-{status}-{code}",
        output=_run_result(status=status),
        code=code,
    )
    root = tmp_path / "runtime" / "canon" / f"hermes-canon-typed-{status}-{code}"
    result = subprocess.run(
        [sys.executable, str(_runner()), str(_manifest(root, [str(child)]))],
        capture_output=True,
        shell=False,
    )

    assert result.returncode == code
    assert json.loads(result.stdout)["status"] == status
    assert result.stderr == b""
    assert not root.exists()



def test_runner_rejects_empty_malformed_nonfinite_and_multiple_stdout(tmp_path: Path) -> None:
    cases = ["", "{", "[]", '{"value":NaN}', '{"ok":true}\n{"second":true}']
    for index, stdout in enumerate(cases):
        child = _fake_canon(tmp_path / f"child-{index}", output=stdout)
        root = tmp_path / "runtime" / "canon" / f"hermes-canon-bad-{index}"
        manifest = _manifest(root, [str(child)])
        result = subprocess.run([sys.executable, str(_runner()), str(manifest)], capture_output=True, shell=False)
        assert result.returncode == 125
        assert json.loads(result.stdout)["error"] == "Canon stdout framing is invalid"
        assert result.stderr == b""
        assert not root.exists()


def test_runner_hides_stderr_and_bounds_nonzero_diagnostic(tmp_path: Path) -> None:
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    child = _fake_canon(child_dir, output="{}", code=7)
    root = tmp_path / "runtime" / "canon" / "hermes-canon-nonzero"
    manifest = _manifest(root, [str(child)])
    env = os.environ.copy()
    env["SECRET_NOT_IN_OUTPUT"] = "secret"
    result = subprocess.run([sys.executable, str(_runner()), str(manifest)], capture_output=True, shell=False, env=env)
    assert result.returncode == 7
    assert json.loads(result.stdout)["error"] == "Canon process failed"
    assert result.stderr == b""
    assert b"secret" not in result.stdout
    assert not root.exists()


def test_sync_launch_uses_terminal_then_process_wait_and_returns_framed_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow, _ = _workflow(tmp_path)
    execution = tmp_path / "execution"
    execution.mkdir()
    profile = tmp_path / "profile-home"
    profile.mkdir()
    fake = _fake_canon(tmp_path / "fake", output=_run_result(result={"value": 7}))
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: [str(fake)])
    monkeypatch.setattr(transport, "get_hermes_home", lambda: profile)
    monkeypatch.setattr(transport, "get_default_hermes_root", lambda: profile)
    calls = []

    class Context:
        def dispatch_tool(self, name, args, **kwargs):
            calls.append((name, args))
            if name == "terminal":
                assert kwargs == {"task_id": None}
                return json.dumps({"session_id": "native-1", "exit_code": 0, "notify_on_complete": True})
            assert kwargs == {}
            assert name == "process_manage"
            if args["action"] == "wait":
                return json.dumps({"status": "exited", "exit_code": 0, "output": 'truncated-tail'})
            assert args == {"action": "log", "session_id": "native-1", "offset": 0, "limit": 2}
            return json.dumps({
                "status": "exited",
                "total_lines": 1,
                "output": _run_result(result={"value": 7}),
            })

    result = json.loads(asyncio.run(transport.handle_tool({
        "workflow_name": str(workflow), "workflow_input": {"task": "x"},
        "execution_root": str(execution), "wait_mode": "sync-short", "timeout_seconds": 5,
    }, ctx=Context())))
    assert result == json.loads(_run_result(result={"value": 7}))
    assert [item[0] for item in calls] == ["terminal", "process_manage", "process_manage"]
    assert calls[0][1]["background"] is True
    assert calls[0][1]["notify_on_complete"] is False
    assert calls[1][1]["action"] == "wait"
    assert calls[2][1]["action"] == "log"
    assert not any((profile / "runtime" / "canon").glob("hermes-canon-*"))


def test_sync_launch_uses_real_terminal_and_process_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow, _ = _workflow(tmp_path)
    execution = tmp_path / "execution"
    execution.mkdir()
    profile = tmp_path / "profile-home"
    profile.mkdir()
    fake = _fake_canon(tmp_path / "fake", output=_run_result(result={"proof": "real-path"}))
    manager = PluginManager(scope_key=str(profile))
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "canon"
    manifest = parse_manifest_file(plugin_dir / "plugin.yaml", plugin_dir, "bundled", "")
    assert manifest is not None
    try:
        manager._load_plugin(manifest)
        from tools.registry import registry
        entry = registry.get_entry("canon_gateway_launch", scope=manager.scope_key)
        assert entry is not None
        assert entry.handler.__module__.startswith("hermes_plugins.canon")
        dynamic_handle = inspect.getclosurevars(entry.handler).globals["handle_tool"]
        monkeypatch.setitem(dynamic_handle.__globals__, "_canon_command_prefix", lambda: [str(fake)])
        monkeypatch.setitem(dynamic_handle.__globals__, "get_hermes_home", lambda: profile)
        monkeypatch.setitem(dynamic_handle.__globals__, "get_default_hermes_root", lambda: profile)
        raw = registry.dispatch("canon_gateway_launch", {
            "workflow_name": str(workflow),
            "workflow_input": {"task": "real"},
            "execution_root": str(execution),
            "timeout_seconds": 10,
        }, scope=manager.scope_key)
        assert json.loads(raw) == json.loads(_run_result(result={"proof": "real-path"}))
        assert not any((profile / "runtime" / "canon").glob("hermes-canon-*"))
    finally:
        manager.unload()


def test_async_native_acceptance_and_completion_queue_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow, _ = _workflow(tmp_path)
    execution = tmp_path / "execution"
    execution.mkdir()
    profile = tmp_path / "profile-home"
    profile.mkdir()
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: ["/bin/true"])
    monkeypatch.setattr(transport, "get_hermes_home", lambda: profile)
    monkeypatch.setattr(transport, "get_default_hermes_root", lambda: profile)
    calls = []

    class Context:
        def dispatch_tool(self, name, args, **kwargs):
            calls.append((name, args))
            assert kwargs == {"task_id": None}
            if name == "terminal":
                return json.dumps({"session_id": "native-async", "exit_code": 0, "notify_on_complete": True})
            return json.dumps({"status": "exited", "exit_code": 0, "output": _run_result()})

    result = json.loads(asyncio.run(transport.handle_tool({
        "workflow_name": str(workflow), "workflow_input": {"task": "x"},
        "execution_root": str(execution), "wait_mode": "async-no-wait",
    }, ctx=Context())))
    assert result == {
        "accepted": True,
        "processSessionId": "native-async",
        "waitMode": "async-no-wait",
    }
    assert [name for name, _ in calls] == ["terminal"]
    private = profile / "runtime" / "canon"
    assert any(private.glob("hermes-canon-*/manifest.json"))
    for root in private.glob("hermes-canon-*"):
        for path in root.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        import shutil
        shutil.rmtree(root)


def test_real_async_launch_queues_native_completion_for_origin_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _ = _workflow(tmp_path)
    execution = tmp_path / "execution"
    execution.mkdir()
    profile = tmp_path / "profile-home"
    profile.mkdir()
    fake = _fake_canon(
        tmp_path / "fake",
        output=_run_result(result={"proof": "async-real-path"}),
        delay=0.1,
    )
    manager = PluginManager(scope_key=str(profile))
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "canon"
    manifest = parse_manifest_file(plugin_dir / "plugin.yaml", plugin_dir, "bundled", "")
    assert manifest is not None
    from tools.process_registry import process_registry
    while True:
        try:
            process_registry.completion_queue.get_nowait()
        except queue.Empty:
            break
    try:
        manager._load_plugin(manifest)
        from tools.registry import registry
        entry = registry.get_entry("canon_gateway_launch", scope=manager.scope_key)
        assert entry is not None
        dynamic_handle = inspect.getclosurevars(entry.handler).globals["handle_tool"]
        monkeypatch.setitem(dynamic_handle.__globals__, "_canon_command_prefix", lambda: [str(fake)])
        monkeypatch.setitem(dynamic_handle.__globals__, "get_hermes_home", lambda: profile)
        monkeypatch.setitem(dynamic_handle.__globals__, "get_default_hermes_root", lambda: profile)
        raw = registry.dispatch(
            "canon_gateway_launch",
            {
                "workflow_name": str(workflow),
                "workflow_input": {"task": "async-real"},
                "execution_root": str(execution),
                "wait_mode": "async-no-wait",
                "timeout_seconds": 10,
            },
            scope=manager.scope_key,
            task_id="origin-task-s07",
        )
        accepted = json.loads(raw)
        event = process_registry.completion_queue.get(timeout=10)
        assert event["type"] == "completion"
        assert event["session_id"] == accepted["processSessionId"]
        assert event["session_key"] == "origin-task-s07"
        assert event["exit_code"] == 0
        assert json.loads(event["output"])["result"]["proof"] == "async-real-path"
        assert not any((profile / "runtime" / "canon").glob("hermes-canon-*"))
    finally:
        manager.unload()


def test_human_gate_wake_prompt_is_compact_reversible_and_integrity_checked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = _full_human_gate()
    pending["gateId"] = "g"
    pending["subjectAuthority"]["gateId"] = "g"  # type: ignore[index]
    canon_result = json.loads(_run_result(status="paused", pending_human_gate=pending))
    expected_wake = _human_gate_wake_prompt(canon_result)
    raw_result = _run_result(
        status="paused",
        pending_human_gate=pending,
        human_gate_prompt=expected_wake,
    )
    expected_output = json.dumps(expected_wake, ensure_ascii=False, separators=(",", ":"))
    assert len(raw_result) > 2_000
    assert len(expected_output) <= 2_000

    workflow, _ = _workflow(tmp_path)
    execution = tmp_path / "execution"
    execution.mkdir()
    profile = tmp_path / "profile-home"
    profile.mkdir()
    fake = _fake_canon(tmp_path / "paused-fake", output=raw_result, delay=0.1)
    manager = PluginManager(scope_key=str(profile))
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "canon"
    manifest = parse_manifest_file(plugin_dir / "plugin.yaml", plugin_dir, "bundled", "")
    assert manifest is not None
    from tools.process_registry import process_registry
    while True:
        try:
            process_registry.completion_queue.get_nowait()
        except queue.Empty:
            break
    try:
        manager._load_plugin(manifest)
        from tools.registry import registry
        entry = registry.get_entry("canon_gateway_launch", scope=manager.scope_key)
        assert entry is not None
        dynamic_handle = inspect.getclosurevars(entry.handler).globals["handle_tool"]
        monkeypatch.setitem(dynamic_handle.__globals__, "_canon_command_prefix", lambda: [str(fake)])
        monkeypatch.setitem(dynamic_handle.__globals__, "get_hermes_home", lambda: profile)
        monkeypatch.setitem(dynamic_handle.__globals__, "get_default_hermes_root", lambda: profile)
        accepted = json.loads(registry.dispatch(
            "canon_gateway_launch",
            {
                "workflow_name": str(workflow),
                "workflow_input": {"task": "human-gate-wake"},
                "execution_root": str(execution),
                "wait_mode": "async-no-wait",
                "timeout_seconds": 10,
            },
            scope=manager.scope_key,
            task_id="origin-human-gate-wake",
        ))
        event = process_registry.completion_queue.get(timeout=10)
        assert event["type"] == "completion"
        assert event["session_id"] == accepted["processSessionId"]
        assert event["session_key"] == "origin-human-gate-wake"
        assert event["exit_code"] == 0
        wake_output = event["output"]
        assert isinstance(wake_output, str)
        assert wake_output.rstrip("\n") == expected_output

        wake = json.loads(wake_output)
        assert set(wake) == {
            "type", "status", "workflowRef", "gateId", "instruction", "actions", "resumeHandle",
            "subjectRevision", "subjectDigest", "decisionAttempt", "cardBody", "cardBodyDigest",
        }
        assert wake["type"] == "canon_human_gate_prompt"
        assert wake["status"] == "paused"
        assert wake["instruction"] == _HUMAN_GATE_WAKE_INSTRUCTION
        assert wake["actions"] == pending["actions"]
        assert set(wake["resumeHandle"]) == {"runId", "threadId", "checkpointId", "nodeId"}
        assert wake["resumeHandle"] == {
            "runId": canon_result["runId"],
            "threadId": canon_result["threadId"],
            "checkpointId": canon_result["checkpointId"],
            "nodeId": canon_result["nodeId"],
        }
        authority = pending["subjectAuthority"]
        assert wake["subjectRevision"] == authority["subjectRevision"]
        assert wake["subjectDigest"] == authority["subjectDigest"]
        assert wake["decisionAttempt"] == pending["decisionAttempt"]

        assert wake["cardBody"] == expected_wake["cardBody"]
        assert wake["cardBodyDigest"] == "sha256:" + hashlib.sha256(
            wake["cardBody"].encode("utf-8")
        ).hexdigest()
        assert wake["cardBodyDigest"] == expected_wake["cardBodyDigest"]
        assert "cardPayload" not in wake
        assert all(
            forbidden not in wake["cardBody"]
            for forbidden in (
                "subjectTransport", "input", "context", "validationEvidence",
                "agentSessionRefs", "sessionRefs", "subject", "workflowInput",
            )
        )
    finally:
        manager.unload()


def test_human_gate_wake_prompt_fails_closed_when_card_cannot_fit_completion_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = _full_human_gate()
    pending["gateId"] = "g"
    pending["subjectAuthority"]["gateId"] = "g"  # type: ignore[index]
    subject_transport = pending["subjectTransport"]
    assert isinstance(subject_transport, dict)
    subjects = subject_transport["inline"]
    assert isinstance(subjects, list)
    subjects[0]["output"] = {
        "draft": _deterministic_entropy_text("oversized-human-gate", 5_000),
    }
    canon_result = json.loads(_run_result(status="paused", pending_human_gate=pending))
    expected_wake = _human_gate_wake_prompt(canon_result)
    raw_result = _run_result(
        status="paused",
        pending_human_gate=pending,
        human_gate_prompt=expected_wake,
    )
    expected_wake_output = json.dumps(expected_wake, ensure_ascii=False, separators=(",", ":"))
    fail_closed = {"error": "Canon Human Gate prompt exceeds completion boundary"}
    expected_output = json.dumps(fail_closed, ensure_ascii=False, separators=(",", ":"))
    assert len(raw_result) > 2_000
    assert len(expected_wake_output) > 2_000
    assert len(expected_output) <= 2_000

    workflow, _ = _workflow(tmp_path)
    execution = tmp_path / "execution"
    execution.mkdir()
    profile = tmp_path / "profile-home"
    profile.mkdir()
    fake = _fake_canon(tmp_path / "oversized-paused-fake", output=raw_result, delay=0.1)
    manager = PluginManager(scope_key=str(profile))
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "canon"
    manifest = parse_manifest_file(plugin_dir / "plugin.yaml", plugin_dir, "bundled", "")
    assert manifest is not None
    from tools.process_registry import process_registry
    while True:
        try:
            process_registry.completion_queue.get_nowait()
        except queue.Empty:
            break
    try:
        manager._load_plugin(manifest)
        from tools.registry import registry
        entry = registry.get_entry("canon_gateway_launch", scope=manager.scope_key)
        assert entry is not None
        dynamic_handle = inspect.getclosurevars(entry.handler).globals["handle_tool"]
        monkeypatch.setitem(dynamic_handle.__globals__, "_canon_command_prefix", lambda: [str(fake)])
        monkeypatch.setitem(dynamic_handle.__globals__, "get_hermes_home", lambda: profile)
        monkeypatch.setitem(dynamic_handle.__globals__, "get_default_hermes_root", lambda: profile)
        accepted = json.loads(registry.dispatch(
            "canon_gateway_launch",
            {
                "workflow_name": str(workflow),
                "workflow_input": {"task": "oversized-human-gate-wake"},
                "execution_root": str(execution),
                "wait_mode": "async-no-wait",
                "timeout_seconds": 10,
            },
            scope=manager.scope_key,
            task_id="origin-oversized-human-gate-wake",
        ))
        event = process_registry.completion_queue.get(timeout=10)
        assert event["type"] == "completion"
        assert event["session_id"] == accepted["processSessionId"]
        assert event["session_key"] == "origin-oversized-human-gate-wake"
        assert event["exit_code"] == 125
        wake_output = event["output"]
        assert isinstance(wake_output, str)
        assert wake_output.rstrip("\n") == expected_output
        bounded = json.loads(wake_output)
        assert set(bounded) == {"error"}
        assert "cardPayload" not in bounded
        assert len(wake_output) <= 2_000
    finally:
        manager.unload()


def test_unexpected_exception_diagnostic_does_not_leak_exception_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        transport,
        "_prepare",
        lambda _args: (_ for _ in ()).throw(RuntimeError("secret-value")),
    )
    result = json.loads(asyncio.run(transport.handle_tool({})))
    assert result == {"error": "Canon launch transport failed"}


def test_timeout_and_cancellation_cleanup_native_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow, _ = _workflow(tmp_path)
    execution = tmp_path / "execution"
    execution.mkdir()
    profile = tmp_path / "profile-home"
    profile.mkdir()
    monkeypatch.setattr(transport, "get_hermes_home", lambda: profile)
    monkeypatch.setattr(transport, "get_default_hermes_root", lambda: profile)
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: ["/bin/true"])
    calls = []

    class Context:
        def dispatch_tool(self, name, args, **kwargs):
            calls.append((name, args))
            if name == "terminal":
                return json.dumps({"session_id": "native-timeout", "exit_code": 0, "notify_on_complete": True})
            if args["action"] == "wait":
                return json.dumps({"status": "timeout", "process_running": True})
            return json.dumps({"status": "killed"})

    result = json.loads(asyncio.run(transport.handle_tool({
        "workflow_name": str(workflow), "workflow_input": {"task": "x"},
        "execution_root": str(execution), "timeout_seconds": 1,
    }, ctx=Context())))
    assert "error" in result
    assert [name for name, _ in calls] == ["terminal", "process_manage", "process_manage"]
    assert calls[-1][1]["action"] == "kill"
    assert not any((profile / "runtime" / "canon").glob("hermes-canon-*"))


def test_redact_cli_text_masks_secret_shaped_markers_across_multiline_and_oversized_input() -> None:
    markers = {
        "BEARER-MARKER",
        "TOKEN-MARKER",
        "SECRET-MARKER",
        "PASSWORD-MARKER",
        "APIKEY-MARKER",
    }
    diagnostic = chr(10).join([
        "Bearer BEARER-MARKER",
        "token=TOKEN-MARKER",
        "secret: SECRET-MARKER",
        "password = PASSWORD-MARKER",
        "api-key: APIKEY-MARKER",
    ]) + chr(10) + "x" * 2_000

    redacted = transport._redact_cli_text(diagnostic)

    assert all(marker not in redacted for marker in markers)
    assert redacted.count("[REDACTED]") == len(markers)
    assert len(redacted) <= transport._MAX_DIAGNOSTIC_CHARS


def test_run_cli_redacts_nonzero_process_diagnostic_before_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "CLI-DIAGNOSTIC-MARKER"
    profile = tmp_path / "profile"
    profile.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(transport, "get_default_hermes_root", lambda: profile)

    class Context:
        def dispatch_tool(self, name, args, **kwargs):
            if name == "terminal":
                return json.dumps({"session_id": "native-redaction", "exit_code": 0})
            assert name == "process_manage"
            if args["action"] == "wait":
                return json.dumps({"status": "exited", "exit_code": 7})
            assert args["action"] == "log"
            return json.dumps({
                "status": "exited",
                "output": f"provider failed\nAuthorization: Bearer {marker}\napi_key={marker}",
            })

    with pytest.raises(transport.CanonTransportError) as error:
        transport._run_cli(Context(), ["canon", "inspect"], root=workdir, timeout=5)

    diagnostic = str(error.value)
    assert marker not in diagnostic
    assert diagnostic.count("[REDACTED]") == 2
    assert len(diagnostic) <= transport._MAX_DIAGNOSTIC_CHARS


def test_run_cli_rejects_unexited_or_multiline_log_framing(tmp_path: Path) -> None:
    root = tmp_path / "workdir"
    root.mkdir()

    class Context:
        def __init__(self, log_result: dict[str, object]) -> None:
            self.log_result = log_result

        def dispatch_tool(self, name, args, **kwargs):
            if name == "terminal":
                return json.dumps({"session_id": "native-framing", "exit_code": 0})
            if args["action"] == "wait":
                return json.dumps({"status": "exited", "exit_code": 0})
            return json.dumps(self.log_result)

    for log_result in (
        {"status": "running", "total_lines": 1, "output": "{}"},
        {"status": "exited", "total_lines": 2, "output": "{}\n{}"},
    ):
        with pytest.raises(transport.CanonTransportError, match="log framing"):
            transport._run_cli(
                Context(log_result), ["canon", "inspect"], root=root, timeout=5,
            )


def test_run_cli_redacts_json_secret_values_before_bounded_error() -> None:
    diagnostic = transport._redact_cli_text(
        '{"token":"JSON-TOKEN-MARKER","api_key":"JSON-API-MARKER"}'
    )

    assert "JSON-TOKEN-MARKER" not in diagnostic
    assert "JSON-API-MARKER" not in diagnostic
    assert diagnostic.count("[REDACTED]") == 2


def _typed_failed_result() -> dict[str, object]:
    return {
        "schemaVersion": "canon-run-result.schema.v2",
        "status": "failed",
        "runId": "run-failed",
        "threadId": "thread-failed",
        "workflowRef": "workflow:s07-workflow",
        "artifactRefs": [],
        "result": {"failure": {"code": "domain_failure"}},
    }


def test_command_prefix_isolated_and_not_ambient(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker = tmp_path / "marker-canon"
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin")
    argv = transport._build_argv(
        transport._canon_command_prefix(),
        Path("/workflow.json"),
        Path("/request.json"),
        Path("/config.json"),
    )

    assert argv[:4] == [sys.executable, "-I", "-m", "canon.cli"]
    assert str(marker) not in argv
    assert "canon" not in argv[:3]


def test_plugin_has_no_core_registration_or_alternate_runner() -> None:
    assert not Path("tools/canon_gateway_launch.py").exists()
    assert not Path("tools/canon_gateway_checkpoint.py").exists()
    assert not Path("tools/canon_workflow_control_tool.py").exists()
    assert not Path("tools/canon_workflow_observe_tool.py").exists()
    assert not Path("tools/canon_gateway_review.py").exists()


def _checkpoint_continue_request(workflow: Path, *, run_id: str = "run-checkpoint", thread_id: str = "thread-checkpoint") -> dict[str, object]:
    workflow_declaration = json.loads(workflow.read_text(encoding="utf-8"))
    return {
        "schemaVersion": "checkpoint-control-request.schema.v1",
        "action": "checkpointControl",
        "mode": "continue_paused",
        "workflowRef": f"workflow:{workflow_declaration['workflowId']}",
        "runId": run_id,
        "threadId": thread_id,
        "checkpointId": "checkpoint-1",
        "nodeId": "await-user",
        "gateId": "gate-1",
        "subjectRevision": 1,
        "subjectDigest": "sha256:" + "a" * 64,
        "decisionAttempt": 1,
        "gateDecision": {"action": "revise", "revisionInstructions": "  preserve exact instructions  "},
        "workflowInput": {"task": "review"},
        "inputPublications": [{"publicationId": "input-1"}],
        "runConfiguration": {"durability": {"mode": "durable-production"}},
        "requestId": "request-1",
    }


def _probe_canon_checkpoint_request(
    argv: list[str],
    workflow: Path,
    *,
    validate_semantics: bool,
) -> subprocess.CompletedProcess[str]:
    """Parse one checkpoint carrier in the current Canon source and its own venv."""
    canon_root = Path(__file__).resolve().parents[4] / "projects" / "canon"
    script = """
import json
import sys
from pathlib import Path

from canon.cli import _checkpoint_request_from_args, build_parser

argv = json.loads(sys.argv[1])
workflow = Path(sys.argv[2])
parsed = _checkpoint_request_from_args(
    build_parser().parse_args(argv),
    workflow_path=workflow,
    mode="continue_paused",
)
if sys.argv[3] == "validate":
    from canon.checkpoint_control import CheckpointControlRequest
    CheckpointControlRequest.from_mapping(parsed)
print(json.dumps(parsed, ensure_ascii=False, sort_keys=True))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(canon_root / "src")
    return subprocess.run(
        [
            str(canon_root / ".venv" / "bin" / "python"),
            "-c",
            script,
            json.dumps(argv[1:]),
            str(workflow),
            "validate" if validate_semantics else "parse",
        ],
        cwd=canon_root,
        env=env,
        capture_output=True,
        text=True,
        shell=False,
    )


def test_checkpoint_continue_builds_complete_private_request_and_cli_valid_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _ = _workflow(tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    durable = profile / "runtime" / "canon" / "durable"
    monkeypatch.setattr(transport, "get_default_hermes_root", lambda: profile)
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: ["canon"])
    root = profile / "runtime" / "canon" / "operation"
    root.mkdir(parents=True, mode=0o700)
    request = _checkpoint_continue_request(workflow)
    assert workflow.name == "workflow.json"
    assert request["workflowRef"] == "workflow:s07-workflow"
    args = {
        "workflow_name": str(workflow), "mode": "continue_paused",
        "run_id": request["runId"], "thread_id": request["threadId"], "payload": request,
    }

    argv = transport._checkpoint_argv(args, root, durable_root=durable)
    assert "--checkpoint-id" not in argv
    assert "--node-id" not in argv
    assert "--payload-json" not in argv
    request_path = Path(argv[argv.index("--request-json") + 1])
    config_path = Path(argv[argv.index("--host-config") + 1])
    assert json.loads(request_path.read_text(encoding="utf-8")) == request
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["hostAdapter"] == "canon-hermes-profile"
    assert config["hostConfiguration"] == {"profilesRoot": str(profile.resolve())}
    assert config["checkpoint"]["db"] == str(durable / "checkpoint.sqlite")
    assert config["artifact"]["root"] == str(durable / "artifacts")
    assert config["journal"]["db"] == str(durable / "journal.sqlite")

    probe = _probe_canon_checkpoint_request(argv, workflow, validate_semantics=False)
    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout) == request


def test_checkpoint_continue_leaves_gate_semantics_to_canon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _ = _workflow(tmp_path)
    root = tmp_path / "operation"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: ["canon"])
    request = _checkpoint_continue_request(workflow)
    request["gateDecision"] = {"action": "not-a-canon-decision"}

    argv = transport._checkpoint_argv({
        "workflow_name": str(workflow), "mode": "continue_paused",
        "run_id": request["runId"], "thread_id": request["threadId"], "payload": request,
    }, root, durable_root=tmp_path / "durable")

    parsed = _probe_canon_checkpoint_request(argv, workflow, validate_semantics=False)
    assert parsed.returncode == 0, parsed.stderr
    assert json.loads(parsed.stdout) == request
    rejected = _probe_canon_checkpoint_request(argv, workflow, validate_semantics=True)
    assert rejected.returncode != 0


def test_checkpoint_replay_preserves_selector_lineage_and_safe_mode_in_private_carrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _ = _workflow(tmp_path)
    root = tmp_path / "operation"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: ["canon"])
    argv = transport._checkpoint_argv(
        {
            "workflow_name": str(workflow), "mode": "replay_from_node",
            "run_id": "run-replay", "thread_id": "thread-replay", "node_id": "await-user",
            "checkpoint_selector": {"matchIndex": 2, "checkpointId": "source-2"},
            "lineage_mode": "same_run_replay", "safeMode": "allow_replay_side_effects",
            "reason": "bounded replay proof",
        },
        root,
        durable_root=tmp_path / "durable",
    )
    assert "--request-json" in argv
    carrier = json.loads(Path(argv[argv.index("--request-json") + 1]).read_text(encoding="utf-8"))
    assert carrier == {
        "checkpointSelector": {"matchIndex": 2, "checkpointId": "source-2"},
        "lineageMode": "same_run_replay",
        "safeMode": "allow_replay_side_effects",
    }
    assert argv[argv.index("--node-id") + 1] == "await-user"
    assert "--checkpoint-id" not in argv


@pytest.mark.parametrize("workflow_ref", ["workflow.json", "workflow:other"])
def test_checkpoint_workflow_ref_mismatch_fails_before_native_process_and_carrier_acceptance(
    workflow_ref: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _ = _workflow(tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(transport, "get_hermes_home", lambda: profile)
    monkeypatch.setattr(transport, "get_default_hermes_root", lambda: profile)
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: ["canon"])
    request = _checkpoint_continue_request(workflow)
    request["workflowRef"] = workflow_ref
    calls: list[str] = []

    class Context:
        def dispatch_tool(self, name, args, **kwargs):
            calls.append(name)
            raise AssertionError("native process must not start")

    result = json.loads(asyncio.run(transport.handle_checkpoint({
        "workflow_name": str(workflow), "mode": "continue_paused",
        "run_id": request["runId"], "thread_id": request["threadId"], "payload": request,
    }, ctx=Context())))
    assert "identity does not match" in result["error"]
    assert calls == []
    private = profile / "runtime" / "canon"
    assert not any(private.glob("hermes-canon-*/checkpoint-request.json"))


def test_checkpoint_identity_mismatch_fails_before_native_process_and_cleans_operation_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _ = _workflow(tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(transport, "get_hermes_home", lambda: profile)
    monkeypatch.setattr(transport, "get_default_hermes_root", lambda: profile)
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: ["canon"])
    request = _checkpoint_continue_request(workflow, run_id="request-run")
    calls: list[str] = []

    class Context:
        def dispatch_tool(self, name, args, **kwargs):
            calls.append(name)
            raise AssertionError("native process must not start")

    result = json.loads(asyncio.run(transport.handle_checkpoint({
        "workflow_name": str(workflow), "mode": "continue_paused",
        "run_id": "public-run", "thread_id": request["threadId"], "payload": request,
    }, ctx=Context())))
    assert "identity does not match" in result["error"]
    assert calls == []
    private = profile / "runtime" / "canon"
    assert not any(private.glob("hermes-canon-*"))
    assert (private / "durable").is_dir()


def test_checkpoint_success_and_failure_remove_only_temporary_carriers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _ = _workflow(tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(transport, "get_hermes_home", lambda: profile)
    monkeypatch.setattr(transport, "get_default_hermes_root", lambda: profile)
    monkeypatch.setattr(transport, "_canon_command_prefix", lambda: ["canon"])
    durable = profile / "runtime" / "canon" / "durable"
    observed: list[list[str]] = []

    class Context:
        def __init__(self, *, timeout: bool = False):
            self.timeout = timeout

        def dispatch_tool(self, name, args, **kwargs):
            if name == "terminal":
                observed.append(args["command"].split())
                return json.dumps({"session_id": "checkpoint-native", "exit_code": 0})
            if args["action"] == "wait":
                return json.dumps({"status": "timeout"}) if self.timeout else json.dumps({"status": "exited", "exit_code": 0})
            if args["action"] == "kill":
                return json.dumps({"status": "killed"})
            return json.dumps({"status": "exited", "total_lines": 1, "output": "{}"})

    base = {
        "workflow_name": str(workflow), "mode": "history",
        "run_id": "run-history", "thread_id": "thread-history",
    }
    assert json.loads(asyncio.run(transport.handle_checkpoint(base, ctx=Context()))) == {"result": {}}
    timeout_result = json.loads(asyncio.run(transport.handle_checkpoint(base, ctx=Context(timeout=True))))
    assert "error" in timeout_result
    assert (durable / "checkpoint.sqlite").parent.is_dir()
    assert not any((profile / "runtime" / "canon").glob("hermes-canon-*"))
    assert observed
