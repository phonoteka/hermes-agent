from __future__ import annotations

import json
from pathlib import Path

import pytest

@pytest.fixture(autouse=True)
def fake_event_source(monkeypatch):
    from plugins.bitrix24 import ingress
    from tests.plugins.test_bitrix24_event_enrichment import FakeEventSource

    source = FakeEventSource('@hermes take a look')
    monkeypatch.setattr(ingress, "RestBitrix24EventSource", lambda client: source)
    return source

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WEBHOOK_DOC = REPO_ROOT / "docs" / "integrations" / "bitrix24-webhooks.md"


class _CapturingContext:
    def __init__(self) -> None:
        self.tools: list[dict] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("BITRIX24_BOT_MENTION", "@hermes")
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["bitrix24"]}}),
        encoding="utf-8",
    )
    return hermes_home


@pytest.fixture
def registered_bitrix24_tools(hermes_home):
    from plugins.bitrix24 import register

    ctx = _CapturingContext()
    register(ctx)
    return ctx.tools


def _find_tool(tools: list[dict], name: str) -> dict | None:
    return next((tool for tool in tools if tool["name"] == name), None)


def _supported_payload(domain: str = "example.com") -> dict:
    return {
        "event_id": "surface-task-42",
        "event": "ONTASKCOMMENTADD",
        "auth": {"domain": domain, "application_token": "expected-app-token"},
        "data": {"FIELDS_AFTER": {"ID": "0", "TASK_ID": "42", "MESSAGE_ID": "84"}},
    }


def test_bitrix24_webhook_ingress_surface_is_registered_before_downstream_behavior(
    registered_bitrix24_tools,
):
    registered_names = [tool["name"] for tool in registered_bitrix24_tools]

    assert "bitrix24_webhook_ingress" in registered_names, (
        "AC-2A REQ-1: plugins.bitrix24.register(ctx) must expose the public "
        "webhook ingress surface before normalization/dedup/filter slices run; "
        f"observed registered tools={registered_names!r}"
    )


def test_bitrix24_webhook_contract_doc_surfaces_dedicated_route_and_generic_route_boundary():
    text = WEBHOOK_DOC.read_text(encoding="utf-8")

    assert "Inbound Bitrix24 -> Hermes" in text
    assert "POST /bitrix24/webhook" in text
    assert "payload auth.application_token + auth.domain" in text
    assert "does not use Hermes generic HMAC headers" in text
    assert "Generic /webhooks/<name> remains HMAC/Svix/GitLab-token gated" in text
    assert "unless an external relay signs Hermes HMAC" in text


def test_bitrix24_webhook_ingress_accepts_supported_payloads_as_json(
    registered_bitrix24_tools,
    monkeypatch,
):
    ingress_tool = _find_tool(registered_bitrix24_tools, "bitrix24_webhook_ingress")
    if ingress_tool is None:
        pytest.skip("registration contour already proven by the dedicated ingress-tool test")

    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")
    result = json.loads(ingress_tool["handler"](_supported_payload(), task_id="red-prep"))

    assert ingress_tool["requires_env"] == ["BITRIX24_WEBHOOK_URL", "BITRIX24_APPLICATION_TOKEN"]
    assert callable(ingress_tool["check_fn"])
    assert result["accepted"] is True


def test_bitrix24_webhook_ingress_rejects_when_webhook_is_unconfigured(
    registered_bitrix24_tools,
    monkeypatch,
):
    ingress_tool = _find_tool(registered_bitrix24_tools, "bitrix24_webhook_ingress")
    if ingress_tool is None:
        pytest.skip("registration contour already proven by the dedicated ingress-tool test")

    monkeypatch.delenv("BITRIX24_WEBHOOK_URL", raising=False)
    result = json.loads(ingress_tool["handler"](_supported_payload(), task_id="red-prep"))

    assert result["accepted"] is False
    assert result["reason"] == "invalid_webhook_url"


def test_bitrix24_webhook_ingress_rejects_when_application_token_is_unconfigured(
    registered_bitrix24_tools,
    monkeypatch,
):
    ingress_tool = _find_tool(registered_bitrix24_tools, "bitrix24_webhook_ingress")
    if ingress_tool is None:
        pytest.skip("registration contour already proven by the dedicated ingress-tool test")

    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.delenv("BITRIX24_APPLICATION_TOKEN", raising=False)
    result = json.loads(ingress_tool["handler"](_supported_payload(), task_id="red-prep"))

    assert result == {
        "accepted": False,
        "reason": "webhook_auth_not_configured",
        "event": "ONTASKCOMMENTADD",
    }


def test_bitrix24_webhook_ingress_rejects_foreign_source_before_downstream_handling(
    registered_bitrix24_tools,
    monkeypatch,
):
    ingress_tool = _find_tool(registered_bitrix24_tools, "bitrix24_webhook_ingress")
    if ingress_tool is None:
        pytest.skip("registration contour already proven by the dedicated ingress-tool test")

    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")
    result = json.loads(
        ingress_tool["handler"](_supported_payload(domain="foreign.example"), task_id="red-prep")
    )

    assert result["accepted"] is False
    assert result["reason"] == "unauthorized_source"
