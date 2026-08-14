"""Tests for hermes -z --usage-file (per-run JSON usage report)."""

import json
import sys
import types

from hermes_cli import oneshot
from hermes_cli.oneshot import _write_usage_file


def _result(**overrides):
    base = {
        "estimated_cost_usd": 0.1234,
        "cost_status": "estimated",
        "cost_source": "pricing-table",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 800,
        "cache_write_tokens": 0,
        "reasoning_tokens": 50,
        "total_tokens": 1250,
        "api_calls": 3,
        "model": "openai/gpt-5.5",
        "provider": "openrouter",
        "session_id": "abc123",
        "completed": True,
        "failed": False,
    }
    base.update(overrides)
    return base


class TestWriteUsageFile:
    def test_writes_report_with_cost_and_tokens(self, tmp_path):
        path = tmp_path / "usage.json"
        _write_usage_file(str(path), _result())
        report = json.loads(path.read_text())
        assert report["estimated_cost_usd"] == 0.1234
        assert report["input_tokens"] == 1000
        assert report["output_tokens"] == 200
        assert report["model"] == "openai/gpt-5.5"
        assert report["api_calls"] == 3
        assert report["failed"] is False
        assert "failure" not in report

    def test_none_path_is_noop(self, tmp_path):
        # Must not raise and must not create a report file.
        _write_usage_file(None, _result())
        assert not (tmp_path / "usage.json").exists()

    def test_failure_marks_failed_and_records_message(self, tmp_path):
        path = tmp_path / "usage.json"
        _write_usage_file(str(path), {}, failure="boom")
        report = json.loads(path.read_text())
        assert report["failed"] is True
        assert report["failure"] == "boom"
        # Missing result fields serialize as null, not KeyError.
        assert report["estimated_cost_usd"] is None


def test_oneshot_forwards_generic_output_validator(monkeypatch):
    captured = {}
    validator = object()

    class FakeAgent:
        def __init__(self, **_kwargs):
            self.suppress_status_output = False
            self.stream_delta_callback = object()
            self.tool_gen_callback = object()

        def run_conversation(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["run_kwargs"] = kwargs
            return {"failed": False, "final_response": "accepted"}

        def shutdown_memory_provider(self, *_args):
            return None

        def close(self):
            return None

    def module(name, **attrs):
        result = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(result, key, value)
        return result

    monkeypatch.setattr(oneshot, "_create_session_db_for_oneshot", lambda: None)
    monkeypatch.setitem(sys.modules, "run_agent", module("run_agent", AIAgent=FakeAgent))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module(
        "hermes_cli.config", load_config=lambda: {"model": {"default": "model"}}
    ))
    monkeypatch.setitem(sys.modules, "hermes_cli.models", module(
        "hermes_cli.models", detect_provider_for_model=lambda *_args, **_kwargs: None
    ))
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", module(
        "hermes_cli.runtime_provider",
        resolve_runtime_provider=lambda **_kwargs: {
            "api_key": "[REDACTED]",
            "base_url": "https://example.invalid/v1",
            "provider": "test-provider",
            "api_mode": "chat_completions",
            "credential_pool": None,
        },
    ))
    monkeypatch.setitem(sys.modules, "hermes_cli.tools_config", module(
        "hermes_cli.tools_config", _get_platform_tools=lambda *_args, **_kwargs: set()
    ))
    monkeypatch.setitem(sys.modules, "hermes_cli.mcp_startup", module(
        "hermes_cli.mcp_startup",
        ensure_mcp_discovery_before_agent_build=lambda **_kwargs: None,
    ))

    result = oneshot._run_agent(
        "hello",
        final_response_validator=validator,
        max_final_response_validation_retries=4,
    )

    assert result[0] == "accepted"
    assert captured["prompt"] == "hello"
    assert captured["run_kwargs"]["final_response_validator"] is validator
    assert captured["run_kwargs"]["max_final_response_validation_retries"] == 4
