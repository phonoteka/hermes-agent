"""Behavioral tests for the strict enriched Mem0/Hermes adapter."""

import json
from types import SimpleNamespace

import pytest

from plugins.memory.mem0._enriched_memory import (
    ENRICHED_SCHEMA_ID,
    EnrichedMemoryError,
    Mem0HermesLlm,
    canonical_text,
    decode_enriched_memory,
    normalize_response,
)


ROOT = "/tmp/hermes-enriched-project"
ARTIFACT = f"{ROOT}/PLAN.md"


def _text_object(**overrides):
    value = {
        "schema": ENRICHED_SCHEMA_ID,
        "content": "The native Mem0 embedding must contain the complete durable memory object.",
        "kind": "constraint",
        "scope": {"type": "project", "subject": "Mem0 adapter"},
        "predicate": "requires complete canonical text",
        "project": {"name": "Hermes project", "root": ROOT},
        "task": {"name": "native memory integration", "context": "Preserve the extraction pipeline."},
        "artifacts": [{"path": ARTIFACT, "kind": "document", "label": "plan"}],
        "tags": ["Mem0", "Hermes", "embedding"],
        "attributed_to": "user",
        "temporal": {"effective_at": None, "expires_at": None},
    }
    value.update(overrides)
    return value


def _messages():
    return [{
        "role": "user",
        "content": f"Project Hermes project at {ROOT}; task native memory integration; artifact {ARTIFACT}; tags Hermes Mem0 embedding.",
    }]


def test_adapter_passes_full_object_as_native_text_once():
    payload = {"memory": [{"text": _text_object()}]}

    class HostLlm:
        def __init__(self):
            self.calls = []

        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(parsed=payload)

    host = HostLlm()
    result = Mem0HermesLlm(SimpleNamespace(host_llm=host)).generate_response(
        _messages(), response_format={"type": "json_object"}
    )
    native = json.loads(result)
    assert len(host.calls) == 1
    assert host.calls[0]["strict"] is True
    assert host.calls[0]["json_schema"]["properties"]["memory"]["maxItems"] == 8
    assert host.calls[0]["input"][0]["type"] == "text"
    assert json.loads(host.calls[0]["input"][0]["text"]) == _messages()
    stored_text = native["memory"][0]["text"]
    assert json.loads(stored_text) == json.loads(canonical_text({
        **_text_object(),
        "tags": ["embedding", "hermes", "mem0"],
    }))
    for field in ("content", "project", "task", "artifacts", "tags", "scope", "predicate", "attributed_to", "temporal"):
        assert field in stored_text


def test_adapter_rejects_non_extraction_call_and_missing_parsed_result():
    class HostLlm:
        def complete_structured(self, **kwargs):
            return SimpleNamespace(parsed=None, text='{"memory":[]}')

    adapter = Mem0HermesLlm(SimpleNamespace(host_llm=HostLlm()))
    with pytest.raises(EnrichedMemoryError, match="unsupported_native_response_format"):
        adapter.generate_response(_messages())
    with pytest.raises(EnrichedMemoryError, match="structured_result_missing"):
        adapter.generate_response(_messages(), response_format={"type": "json_object"})


def test_response_max_items_is_checked_before_normalization():
    memories = [
        {"text": _text_object(content=f"Durable memory {index}")}
        for index in range(9)
    ]
    with pytest.raises(EnrichedMemoryError, match="schema_invalid"):
        normalize_response({"memory": memories}, source=_messages())


def test_invented_project_is_rejected_not_silently_removed():
    invented = _text_object(project={"name": "Invented project", "root": ROOT})
    with pytest.raises(EnrichedMemoryError, match="invented_project_name"):
        normalize_response({"memory": [{"text": invented}]}, source=_messages())


def test_native_config_bypasses_mem0_allowlist_without_foreign_client():
    pytest.importorskip("mem0")
    from mem0.utils.factory import LlmFactory

    from plugins.memory.mem0.config_schema import (
        build_native_memory_config,
        register_native_llm,
    )

    host = object()
    register_native_llm()
    config = build_native_memory_config({}, host)

    assert config.llm.provider == "hermes"
    assert config.llm.config == {"host_llm": host}
    adapter = LlmFactory.create(config.llm.provider, config.llm.config)
    assert adapter._host_llm is host


def test_semantic_enrichment_is_not_limited_to_verbatim_source_tokens():
    candidate = _text_object(
        content="The user prefers deterministic enriched-memory behavior.",
        kind="preference",
        scope={"type": "user", "subject": "the user"},
        predicate="prefers deterministic enriched-memory behavior",
        project={"name": None, "root": None},
        task={"name": "memory reliability", "context": "Keep durable recall deterministic."},
        artifacts=[],
        tags=["determinism", "memory-reliability"],
        attributed_to="user",
    )
    source = [{"role": "user", "content": "Please make my saved memories stable and repeatable."}]
    normalized = normalize_response({"memory": [{"text": candidate}]}, source=source)[0]
    assert normalized["predicate"] == candidate["predicate"]
    assert normalized["task"] == candidate["task"]
    assert normalized["tags"] == ["determinism", "memory-reliability"]


def test_project_and_task_change_canonical_text():
    first = normalize_response({"memory": [{"text": _text_object()}]}, source=_messages())[0]
    changed = _text_object(task={"name": "different task", "context": "Preserve the extraction pipeline."})
    second = normalize_response({"memory": [{"text": changed}]}, source=_messages())[0]
    assert canonical_text(first) != canonical_text(second)


def test_invalid_nested_field_rejects_whole_batch():
    invalid = _text_object(artifacts=[{"path": ARTIFACT, "kind": "document", "label": "plan", "invented": True}])
    with pytest.raises(EnrichedMemoryError) as exc:
        normalize_response({"memory": [{"text": invalid}]}, source=_messages())
    assert exc.value.reason_code == "schema_invalid"


def test_fenced_progress_and_secret_outputs_fail_closed():
    for content, code in (
        ("```json {\\\"memory\\\": []} ```", "non_durable_content"),
        ("Worker state: step 1 is in progress", "non_durable_content"),
        ("Use sk-12345678901234567890123456789012", "secret_detected"),
    ):
        with pytest.raises(EnrichedMemoryError) as exc:
            normalize_response({"memory": [{"text": _text_object(content=content)}]}, source=_messages())
        assert exc.value.reason_code == code


def test_invented_artifact_rejects_entire_response():
    invented = _text_object(artifacts=[{"path": f"{ROOT}/missing.txt", "kind": "file", "label": "missing"}])
    with pytest.raises(EnrichedMemoryError) as exc:
        normalize_response({"memory": [{"text": invented}]}, source=_messages())
    assert exc.value.reason_code == "invented_artifact"


@pytest.mark.parametrize(
    ("source_content", "project", "artifacts", "reason"),
    [
        (
            "/repo/PLAN.md.bak",
            {"name": None, "root": None},
            [{"path": "/repo/PLAN.md", "kind": "file", "label": None}],
            "invented_artifact",
        ),
        (
            "Project Hermes project-old",
            {"name": "Hermes project", "root": None},
            [],
            "invented_project_name",
        ),
        (
            "Project root /repo-old",
            {"name": None, "root": "/repo"},
            [],
            "invented_project_root",
        ),
    ],
)
def test_provenance_rejects_prefix_matches(source_content, project, artifacts, reason):
    candidate = _text_object(
        scope={"type": "user", "subject": "the user"},
        project=project,
        artifacts=artifacts,
    )
    with pytest.raises(EnrichedMemoryError) as exc:
        normalize_response(
            {"memory": [{"text": candidate}]},
            source=[{"role": "user", "content": source_content}],
        )
    assert exc.value.reason_code == reason


def test_provenance_accepts_complete_tokens_before_punctuation():
    candidate = _text_object(
        scope={"type": "project", "subject": "Mem0 adapter"},
        project={"name": "Hermes project", "root": ROOT},
        artifacts=[{"path": ARTIFACT, "kind": "document", "label": "plan"}],
    )
    source = [{
        "role": "user",
        "content": f"Project (Hermes project), root `{ROOT}`; artifact [{ARTIFACT}].",
    }]
    normalized = normalize_response({"memory": [{"text": candidate}]}, source=source)
    assert normalized[0]["artifacts"][0]["path"] == ARTIFACT


def test_readback_returns_structured_only_for_canonical_enriched_text():
    normalized = normalize_response({"memory": [{"text": _text_object()}]}, source=_messages())[0]
    stored = canonical_text(normalized)
    assert decode_enriched_memory(stored) == normalized
    assert decode_enriched_memory("old plain memory") is None
    assert decode_enriched_memory(stored.replace("constraint", "not-a-kind")) is None
    assert decode_enriched_memory(stored + " ") is None


def test_native_pipeline_embeds_hashes_persists_and_deduplicates_full_object(monkeypatch):
    from hashlib import md5

    native = pytest.importorskip("mem0.memory.main")
    from plugins.memory.mem0._backend import OSSBackend

    payload = {"memory": [{"text": {
        **_text_object(
            content="The user prefers deterministic enriched memory embeddings.",
            kind="preference",
            scope={"type": "user", "subject": "the user"},
            predicate="prefers deterministic enriched memory embeddings",
            project={"name": None, "root": None},
            task={"name": None, "context": None},
            artifacts=[],
        ),
    }}]}

    class HostLlm:
        def __init__(self):
            self.calls = []

        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(parsed=payload)

    class Embedder:
        def __init__(self):
            self.batch_calls = []

        def embed(self, text, action):
            return [0.1, 0.2]

        def embed_batch(self, texts, action):
            self.batch_calls.append(list(texts))
            return [[0.1, 0.2] for _ in texts]

    class VectorStore:
        def __init__(self):
            self.rows = []

        def search(self, **kwargs):
            return [
                SimpleNamespace(id=row["id"], payload=row["payload"], score=1.0)
                for row in self.rows
            ]

        def insert(self, *, vectors, ids, payloads):
            self.rows.extend(
                {"id": memory_id, "payload": stored_payload}
                for memory_id, stored_payload in zip(ids, payloads)
            )

    class HistoryDb:
        def __init__(self, *args, **kwargs):
            pass

        def get_last_messages(self, *args, **kwargs):
            return []

        def save_messages(self, *args, **kwargs):
            pass

        def batch_add_history(self, *args, **kwargs):
            pass

        def add_history(self, *args, **kwargs):
            pass

    host = HostLlm()
    embedder = Embedder()
    vector_store = VectorStore()
    monkeypatch.setattr(native.EmbedderFactory, "create", lambda *args, **kwargs: embedder)
    monkeypatch.setattr(native.VectorStoreFactory, "create", lambda *args, **kwargs: vector_store)
    monkeypatch.setattr(native, "SQLiteManager", HistoryDb)
    monkeypatch.setattr(native, "MEM0_TELEMETRY", False)
    monkeypatch.setattr(native, "capture_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(native, "display_first_run_notice", lambda *args, **kwargs: None)
    monkeypatch.setattr(native, "extract_entities_batch", lambda texts: [[] for _ in texts])
    monkeypatch.setattr(native, "lemmatize_for_bm25", lambda text: text)

    backend = OSSBackend(
        {
            "llm": {"provider": "openai", "config": {"model": "must-not-be-created"}},
            "embedder": {"provider": "ollama", "config": {"model": "probe-no-known-dims"}},
            "vector_store": {"provider": "qdrant", "config": {"collection_name": "probe"}},
        },
        llm=host,
    )
    messages = [{"role": "user", "content": "I prefer deterministic enriched memory embeddings."}]
    first = backend.add(messages, user_id="probe-user", agent_id="hermes", infer=True)
    second = backend.add(messages, user_id="probe-user", agent_id="hermes", infer=True)

    assert len(host.calls) == 2
    assert len(vector_store.rows) == 1
    stored = vector_store.rows[0]["payload"]["data"]
    assert embedder.batch_calls[0] == [stored]
    assert vector_store.rows[0]["payload"]["hash"] == md5(stored.encode()).hexdigest()
    assert first["results"][0]["memory"] == stored
    assert second["results"] == []
    assert set(json.loads(stored)) == {
        "artifacts", "attributed_to", "content", "kind", "predicate", "project",
        "schema", "scope", "tags", "task", "temporal",
    }
