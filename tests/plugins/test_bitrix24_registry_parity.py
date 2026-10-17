from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.registry import ToolRegistry, invalidate_check_fn_cache


class _CapturingContext:
    def __init__(self) -> None:
        self.tools: list[dict] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)

    def register_platform(self, **kwargs) -> None:
        self.platforms = [*getattr(self, "platforms", []), kwargs]


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_MANIFEST = REPO_ROOT / "plugins" / "bitrix24" / "plugin.yaml"
ALL_BITRIX24_TOOLS = {
    "bitrix24_code_read",
    "bitrix24_code_search",
    "bitrix24_status",
    "bitrix24_webhook_ingress",
    "bitrix24_task_create",
    "bitrix24_task_get",
    "bitrix24_task_list",
    "bitrix24_task_update",
    "bitrix24_task_complete",
    "bitrix24_task_comments",
    "bitrix24_task_comment_add",
    "bitrix24_task_comment_update",
    "bitrix24_chat_get",
    "bitrix24_chat_send",
    "bitrix24_my_plan_stages_get",
    "bitrix24_my_plan_task_move",
}
TASK_SURFACE_WITHOUT_TOKEN = {
    "bitrix24_code_read",
    "bitrix24_code_search",
    "bitrix24_status",
    "bitrix24_task_create",
    "bitrix24_task_get",
    "bitrix24_task_list",
    "bitrix24_task_update",
    "bitrix24_task_complete",
    "bitrix24_task_comments",
    "bitrix24_task_comment_add",
    "bitrix24_task_comment_update",
    "bitrix24_chat_get",
    "bitrix24_chat_send",
    "bitrix24_my_plan_stages_get",
    "bitrix24_my_plan_task_move",
}


def _registered_tools() -> list[dict]:
    from plugins.bitrix24 import register

    ctx = _CapturingContext()
    register(ctx)
    return ctx.tools


def _registry_for_registered_tools(tools: list[dict]) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(
            name=tool["name"],
            toolset=tool["toolset"],
            schema=tool["schema"],
            handler=tool["handler"],
            check_fn=tool["check_fn"],
            requires_env=tool["requires_env"],
        )
    return registry


def _visible_tool_names(registry: ToolRegistry, tool_names: set[str]) -> set[str]:
    invalidate_check_fn_cache()
    return {
        definition["function"]["name"]
        for definition in registry.get_definitions(tool_names, quiet=True)
    }


def test_bitrix24_manifest_provides_tools_matches_registered_surface_exactly():
    manifest = yaml.safe_load(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    registered_names = {tool["name"] for tool in _registered_tools()}

    assert registered_names == ALL_BITRIX24_TOOLS
    assert set(manifest["provides_tools"]) == registered_names


def test_bitrix24_manifest_requires_env_reflects_plugin_minimum_not_ingress_token_gate():
    manifest = yaml.safe_load(PLUGIN_MANIFEST.read_text(encoding="utf-8"))

    assert manifest["requires_env"] == ["BITRIX24_WEBHOOK_URL"]


def test_bitrix24_registered_requires_env_keeps_ingress_token_specific():
    tools = {tool["name"]: tool for tool in _registered_tools()}

    assert tools["bitrix24_code_read"]["requires_env"] == ["BITRIX24_WEBHOOK_URL"]
    assert tools["bitrix24_code_search"]["requires_env"] == ["BITRIX24_WEBHOOK_URL"]
    assert tools["bitrix24_webhook_ingress"]["requires_env"] == [
        "BITRIX24_WEBHOOK_URL",
        "BITRIX24_APPLICATION_TOKEN",
    ]
    for tool_name in TASK_SURFACE_WITHOUT_TOKEN:
        assert tools[tool_name]["requires_env"] == ["BITRIX24_WEBHOOK_URL"]


def test_bitrix24_registry_visibility_exposes_all_tools_when_url_and_token_are_set(monkeypatch):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    registry = _registry_for_registered_tools(_registered_tools())

    assert _visible_tool_names(registry, ALL_BITRIX24_TOOLS) == ALL_BITRIX24_TOOLS


def test_bitrix24_registry_visibility_hides_all_tools_without_valid_webhook_url(monkeypatch):
    monkeypatch.delenv("BITRIX24_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("BITRIX24_APPLICATION_TOKEN", raising=False)

    registry = _registry_for_registered_tools(_registered_tools())

    assert _visible_tool_names(registry, ALL_BITRIX24_TOOLS) == set()


def test_bitrix24_registry_visibility_keeps_task_tools_available_without_token(monkeypatch):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://example.com/rest/1/test")
    monkeypatch.delenv("BITRIX24_APPLICATION_TOKEN", raising=False)

    registry = _registry_for_registered_tools(_registered_tools())
    visible = _visible_tool_names(registry, ALL_BITRIX24_TOOLS)

    assert visible == TASK_SURFACE_WITHOUT_TOKEN


@pytest.mark.parametrize(
    "raw_url",
    [
        "https://example.com/not-rest-path",
        "https://example.com/rest/api/1/token",
        "https://example.com/rest/1/test/tasks.task.get",
    ],
)
def test_bitrix24_registry_visibility_hides_all_tools_for_invalid_old_rest_base(monkeypatch, raw_url):
    monkeypatch.setenv("BITRIX24_WEBHOOK_URL", raw_url)
    monkeypatch.setenv("BITRIX24_APPLICATION_TOKEN", "expected-app-token")

    registry = _registry_for_registered_tools(_registered_tools())

    assert _visible_tool_names(registry, ALL_BITRIX24_TOOLS) == set()
