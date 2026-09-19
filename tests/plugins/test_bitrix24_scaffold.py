from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["bitrix24"]}})
    )
    return hermes_home


def test_bitrix24_bundled_plugin_scaffold_is_discoverable_before_downstream_slices():
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load(force=True)

    assert "bitrix24" in manager._plugins, (
        "AC-BITRIX24-SCAFFOLD REQ-BITRIX24-SCAFFOLD: "
        "bundled Bitrix24 plugin scaffold missing from PluginManager.discover_and_load(force=True)"
    )
