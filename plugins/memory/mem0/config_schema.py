"""Native Mem0 configuration owned by the bundled Hermes adapter."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

NATIVE_LLM_PROVIDER = "hermes"
NATIVE_LLM_SCHEMA_NAME = "hermes_memory_enriched_v1"


class HermesLlmConfig:
    """Minimal factory config carrying the injected host LLM only."""

    def __init__(self, host_llm: Any = None, **_: Any) -> None:
        self.host_llm = host_llm


def register_native_llm() -> None:
    """Register the adapter with the installed Mem0 factory."""
    from mem0.utils.factory import LlmFactory

    LlmFactory.register_provider(
        NATIVE_LLM_PROVIDER,
        "plugins.memory.mem0._enriched_memory.Mem0HermesLlm",
        HermesLlmConfig,
    )


def build_native_memory_config(oss_config: dict[str, Any], host_llm: Any):
    """Build a validated MemoryConfig without constructing a foreign LLM.

    Mem0 2.0.10 validates LLM provider names before consulting its extensible
    LlmFactory.  Validate the ordinary vector/embedder fields first, then
    replace only the LLM value with the already registered host adapter.  The
    temporary allowed provider is never passed to ``Memory`` and therefore
    never creates a client or reads model credentials.
    """
    from mem0.configs.base import MemoryConfig
    from mem0.llms.configs import LlmConfig

    base = deepcopy(oss_config)
    base["llm"] = {"provider": "openai", "config": {}}
    config = MemoryConfig(**base)
    config.llm = LlmConfig.model_construct(
        provider=NATIVE_LLM_PROVIDER,
        config={"host_llm": host_llm},
    )
    return config
