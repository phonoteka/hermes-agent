"""Canon phase structured output on the Anthropic Messages transport (output_config.format)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.anthropic_adapter import build_anthropic_kwargs
from agent.chat_completion_helpers import _build_anthropic_kwargs
from agent.transports import get_transport

SCHEMA = {
    "type": "object",
    "properties": {"result": {"type": "string", "description": "answer (minLength=3)"}},
    "required": ["result"],
    "additionalProperties": False,
}
MODEL = "claude-opus-4-6"


def _agent(**overrides):
    values = {
        "model": MODEL,
        "max_tokens": 4096,
        "context_compressor": None,
        "_is_anthropic_oauth": False,
        "_anthropic_base_url": None,
        "_oauth_1m_beta_disabled": False,
        "provider": "anthropic",
        "_get_transport": lambda: get_transport("anthropic_messages"),
        "_prepare_anthropic_messages_for_api": lambda messages: messages,
        "_anthropic_preserve_dots": lambda: False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _messages():
    return [{"role": "user", "content": "Return the answer."}]


def test_output_format_builder_returns_detached_json_schema_projection():
    from agent.transports.anthropic import build_canon_anthropic_output_format

    projection = build_canon_anthropic_output_format(SCHEMA)

    assert projection == {"output_config": {"format": {"type": "json_schema", "schema": SCHEMA}}}
    assert projection["output_config"]["format"]["schema"] is not SCHEMA


@pytest.mark.parametrize("schema", [None, [], {"type": ("object",)}, {1: "x"}, {"n": float("nan")}])
def test_output_format_builder_rejects_non_json_schema_values(schema):
    from agent.transports.anthropic import build_canon_anthropic_output_format

    with pytest.raises(ValueError):
        build_canon_anthropic_output_format(schema)


def test_request_override_output_format_reaches_messages_kwargs():
    from agent.transports.anthropic import build_canon_anthropic_output_format

    overrides = build_canon_anthropic_output_format(SCHEMA)

    kwargs = _build_anthropic_kwargs(_agent(), _messages(), [], None, overrides)

    assert kwargs["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}


def test_output_format_is_merged_with_adaptive_effort():
    from agent.transports.anthropic import build_canon_anthropic_output_format

    overrides = build_canon_anthropic_output_format(SCHEMA)

    kwargs = _build_anthropic_kwargs(_agent(), _messages(), [], {"enabled": True, "effort": "high"}, overrides)

    assert kwargs["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert "effort" in kwargs["output_config"]
    assert kwargs["thinking"]["type"] == "adaptive"


def test_without_output_format_kwargs_are_unchanged():
    baseline = build_anthropic_kwargs(
        model=MODEL, messages=_messages(), tools=None, max_tokens=4096, reasoning_config=None
    )

    kwargs = _build_anthropic_kwargs(_agent(), _messages(), [], None, {})

    assert kwargs == baseline
    assert "output_config" not in kwargs


def test_output_format_with_assistant_prefill_is_rejected():
    from agent.transports.anthropic import build_canon_anthropic_output_format

    overrides = build_canon_anthropic_output_format(SCHEMA)
    prefilled = _messages() + [{"role": "assistant", "content": "{"}]

    with pytest.raises(ValueError, match="prefill"):
        _build_anthropic_kwargs(_agent(), prefilled, [], None, overrides)
