from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tools.registry import ToolRegistry

from plugins.bitrix24 import schemas as bitrix24_schemas


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_MANIFEST = REPO_ROOT / "plugins" / "bitrix24" / "plugin.yaml"
WEBHOOK_DOC = REPO_ROOT / "docs" / "integrations" / "bitrix24-webhooks.md"


class _CapturingContext:
    def __init__(self) -> None:
        self.tools: list[dict] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)


def _register_bitrix24_status_tool() -> dict:
    from plugins.bitrix24 import register

    ctx = _CapturingContext()
    register(ctx)

    tool = next(tool for tool in ctx.tools if tool["name"] == "bitrix24_status")
    assert tool["name"] == "bitrix24_status"
    return tool


def _register_bitrix24_tool(name: str) -> dict:
    from plugins.bitrix24 import register

    ctx = _CapturingContext()
    register(ctx)

    tool = next(tool for tool in ctx.tools if tool["name"] == name)
    assert tool["name"] == name
    return tool


def _fresh_registry_with(tool: dict) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        name=tool["name"],
        toolset=tool["toolset"],
        schema=tool["schema"],
        handler=tool["handler"],
        check_fn=tool["check_fn"],
        requires_env=tool["requires_env"],
    )
    return registry


def test_bitrix24_status_tool_is_hidden_without_webhook_credentials(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["bitrix24"]}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("BITRIX24_WEBHOOK_URL", raising=False)

    tool = _register_bitrix24_status_tool()
    registry = _fresh_registry_with(tool)

    assert tool["requires_env"] == ["BITRIX24_WEBHOOK_URL"]
    assert tool["check_fn"]() is False
    assert registry.get_definitions({"bitrix24_status"}, quiet=True) == []


def test_bitrix24_status_tool_becomes_visible_with_webhook_credentials(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["bitrix24"]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")

    tool = _register_bitrix24_status_tool()
    registry = _fresh_registry_with(tool)
    visible = [
        definition["function"]["name"]
        for definition in registry.get_definitions({"bitrix24_status"}, quiet=True)
    ]

    assert json.loads(tool["handler"]({}, task_id="red-prep"))["webhook_configured"] is True
    assert visible == ["bitrix24_status"], (
        "AC-1 REQ-6 REQ-8: explicit Bitrix24 credential config still does not open "
        "the gated tool surface; expected ToolRegistry.get_definitions(...) to expose "
        "bitrix24_status after BITRIX24_WEBHOOK_URL is set"
    )


def test_bitrix24_webhook_ingress_requires_application_token_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["bitrix24"]}}),
        encoding="utf-8",
    )

    ingress_tool = _register_bitrix24_tool("bitrix24_webhook_ingress")

    assert ingress_tool["requires_env"] == ["BITRIX24_WEBHOOK_URL", "BITRIX24_APPLICATION_TOKEN"]


def test_bitrix24_manifest_documents_old_rest_only_and_secret_redaction_contract():
    manifest = yaml.safe_load(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    description = manifest["description"]

    assert manifest["requires_env"] == ["BITRIX24_WEBHOOK_URL"]
    assert "https://{portal}/rest/{user}/{secret}" in description
    assert "/rest/api/{user}/{token}" in description
    assert "unsupported/fail-closed" in description
    assert "BITRIX24_WEBHOOK_URL" in description
    assert "POST /bitrix24/webhook" in description
    assert "platforms.webhook.extra.bitrix24.enabled=true" in description
    assert "auth.application_token" in description
    assert "auth.domain" in description
    assert "generic HMAC headers" in description
    assert "redacted" in description
    assert "application tokens" in description


@pytest.mark.parametrize(
    ("schema_name", "expected_required", "expected_additional_properties"),
    [
        ("BITRIX24_STATUS_SCHEMA", [], False),
        ("BITRIX24_WEBHOOK_INGRESS_SCHEMA", ["auth", "data", "event", "event_id"], True),
        ("BITRIX24_TASK_CREATE_SCHEMA", ["responsible_id", "title"], False),
        ("BITRIX24_TASK_GET_SCHEMA", ["task_id"], False),
        ("BITRIX24_TASK_UPDATE_SCHEMA", ["fields", "task_id"], False),
        ("BITRIX24_TASK_COMPLETE_SCHEMA", ["task_id"], False),
        ("BITRIX24_TASK_COMMENTS_SCHEMA", ["task_id"], False),
        ("BITRIX24_CHAT_GET_SCHEMA", ["chat_id"], False),
    ],
)
def test_bitrix24_schema_contracts_keep_required_fields_and_additional_properties(
    schema_name,
    expected_required,
    expected_additional_properties,
):
    schema = getattr(bitrix24_schemas, schema_name)
    parameters = schema["parameters"]

    assert schema["name"].startswith("bitrix24_")
    assert parameters["type"] == "object"
    assert sorted(parameters.get("required", [])) == expected_required
    assert parameters["additionalProperties"] is expected_additional_properties


def test_bitrix24_webhook_ingress_schema_documents_application_token_requirement_and_redaction_policy():
    parameters = bitrix24_schemas.BITRIX24_WEBHOOK_INGRESS_SCHEMA["parameters"]
    auth = parameters["properties"]["auth"]
    description = bitrix24_schemas.BITRIX24_WEBHOOK_INGRESS_SCHEMA["description"]

    assert "event_id" in parameters["properties"]
    assert auth["properties"]["application_token"] == {"type": "string"}
    assert auth["required"] == ["application_token"]
    assert "auth.application_token" in description
    assert "auth.domain" in description
    assert "https://{portal}/rest/{user}/{secret}" in description
    assert "/rest/api/{user}/{token} is unsupported" in description
    assert "POST /bitrix24/webhook" in description
    assert "platforms.webhook.extra.bitrix24.enabled=true" in description
    assert "generic HMAC headers" in description
    assert "application/x-www-form-urlencoded" in description
    assert "event/event_handler_id/material" in description
    assert "BITRIX24_WEBHOOK_URL" in description
    assert "BITRIX24_APPLICATION_TOKEN" in description
    assert "DOWNLOAD_URL" in description


def test_bitrix24_webhook_contract_doc_covers_dedicated_route_auth_and_redaction_surface():
    text = WEBHOOK_DOC.read_text(encoding="utf-8")

    assert "BITRIX24_WEBHOOK_URL" in text
    assert "https://{portal}/rest/{user}/{secret}" in text
    assert "/rest/api/{user}/{token}" in text
    assert "unsupported" in text
    assert "POST /bitrix24/webhook" in text
    assert "platforms.webhook.extra.bitrix24.enabled: true" in text
    assert "auth.application_token" in text
    assert "auth.domain" in text
    assert "application/x-www-form-urlencoded" in text
    assert "event/event_handler_id/material" in text
    assert "HMAC" in text
    assert "Svix" in text
    assert "GitLab-token" in text
    assert "/webhooks/<name>" in text
    assert "relay" in text
    assert "DOWNLOAD_URL" in text
