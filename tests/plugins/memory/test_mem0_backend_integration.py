"""Integration coverage for Hermes' pinned Mem0 OSS boundary."""

import copy
import os
from types import SimpleNamespace

import pytest


pytest.importorskip("mem0", reason="requires the existing mem0 extra")


def test_oss_backend_uses_injected_host_llm_without_openai(monkeypatch, tmp_path):
    mem0_dir = tmp_path / "mem0"
    monkeypatch.setenv("MEM0_DIR", str(mem0_dir))
    monkeypatch.setenv("OPENAI_API_KEY", "environment-openai-sentinel")
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-sentinel")

    import openai
    from mem0.memory import main as memory_main
    from mem0.utils.factory import LlmFactory

    from plugins.memory.mem0._backend import OSSBackend
    from plugins.memory.mem0._enriched_memory import Mem0HermesLlm

    class RaisingOpenAI:
        def __init__(self, *args, **kwargs):
            raise AssertionError("the Mem0 host-LLM path must not construct OpenAI")

    class FakeHostLlm:
        def __init__(self):
            self.requests = []

        def complete_structured(self, **request):
            self.requests.append(request)
            return SimpleNamespace(parsed={"memory": []})

    class DummyVectorStore:
        pass

    class DummyDB:
        def __init__(self, _path):
            pass

    monkeypatch.setattr(
        LlmFactory,
        "provider_to_class",
        dict(LlmFactory.provider_to_class),
    )
    monkeypatch.setattr(openai, "OpenAI", RaisingOpenAI)
    monkeypatch.setattr(
        memory_main.EmbedderFactory,
        "create",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        memory_main.VectorStoreFactory,
        "create",
        lambda *_args, **_kwargs: DummyVectorStore(),
    )
    monkeypatch.setattr(memory_main, "SQLiteManager", DummyDB)
    monkeypatch.setattr(memory_main, "MEM0_TELEMETRY", False)
    monkeypatch.setattr(memory_main, "capture_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        OSSBackend,
        "_recreate_collection_if_dims_changed",
        staticmethod(lambda *_args, **_kwargs: None),
    )

    config = {
        "llm": {
            "provider": "openai",
            "config": {
                "model": "gpt-5-mini",
                "api_key": "configured-openai-sentinel",
                "openai_base_url": "https://openai.example/v1",
                "models": ["router-model"],
                "route": "lowest-latency",
            },
        },
        "embedder": {
            "provider": "ollama",
            "config": {
                "model": "nomic-embed-text",
                "ollama_base_url": "http://ollama.example:11434",
                "embedding_dims": 768,
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "mem0",
                "path": str(tmp_path / "qdrant"),
            },
        },
    }
    original_config = copy.deepcopy(config)
    environment = dict(os.environ)
    host_llm = FakeHostLlm()
    messages = [{"role": "user", "content": "remember tea"}]

    backend = OSSBackend(config, llm=host_llm)
    result = backend._memory.llm.generate_response(
        messages,
        response_format={"type": "json_object"},
    )

    assert isinstance(backend._memory.llm, Mem0HermesLlm)
    assert len(host_llm.requests) == 1
    request = host_llm.requests[0]
    assert request["strict"] is True
    assert request["schema_name"] == "hermes_memory_enriched_v1"
    assert request["purpose"] == "mem0_enriched_memory"
    assert request["json_schema"]["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert request["input"] == [{
        "type": "text",
        "text": '[{"content":"remember tea","role":"user"}]',
    }]
    assert result == '{"memory":[]}'
    assert config == original_config
    assert dict(os.environ) == environment
