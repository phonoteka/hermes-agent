from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "bitrix24"
PLUGIN_INIT = PLUGIN_DIR / "__init__.py"
EXPECTED_MODULES = (
    "schemas.py",
    "client.py",
    "tools.py",
    "ingress.py",
    "events.py",
    "attachments.py",
    "handoff.py",
    "dedup.py",
)
FORBIDDEN_INIT_MARKERS = (
    "class Bitrix24Client",
    "def _normalize_",
    "def _handle_",
    "BITRIX24_STATUS_SCHEMA =",
    "BITRIX24_WEBHOOK_INGRESS_SCHEMA =",
    "BITRIX24_TASK_CREATE_SCHEMA =",
    "BITRIX24_TASK_GET_SCHEMA =",
    "BITRIX24_TASK_UPDATE_SCHEMA =",
    "BITRIX24_TASK_COMPLETE_SCHEMA =",
    "BITRIX24_TASK_COMMENTS_SCHEMA =",
    "BITRIX24_CHAT_GET_SCHEMA =",
    "from urllib.request import Request, urlopen",
    "def _attachment_",
    "def _materialize_attachments",
)
FORBIDDEN_LEAF_PATTERNS = (
    'sys.modules.get("plugins.bitrix24")',
    "sys.modules.get('plugins.bitrix24')",
)
MAX_INIT_LINES = 200


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["bitrix24"]}}),
        encoding="utf-8",
    )
    return hermes_home


def test_bitrix24_refactor_target_module_files_exist():
    missing = [name for name in EXPECTED_MODULES if not (PLUGIN_DIR / name).is_file()]

    assert not missing, (
        "AC-BITRIX24-ARCH REQ-R0: expected Bitrix24 refactor module files are missing; "
        f"missing={missing}"
    )


def test_bitrix24_init_is_thin_register_and_reexport_only():
    init_text = PLUGIN_INIT.read_text(encoding="utf-8")
    line_count = len(init_text.splitlines())
    offenders = [marker for marker in FORBIDDEN_INIT_MARKERS if marker in init_text]

    assert line_count <= MAX_INIT_LINES, (
        "AC-BITRIX24-ARCH REQ-R0: plugins/bitrix24/__init__.py must stay thin after the "
        f"refactor; line_count={line_count} max_allowed={MAX_INIT_LINES}"
    )
    assert not offenders, (
        "AC-BITRIX24-ARCH REQ-R0: plugins/bitrix24/__init__.py still contains implementation "
        f"markers that belong in extracted modules; offenders={offenders}"
    )


def test_bitrix24_public_register_import_still_works_without_live_credentials(monkeypatch):
    monkeypatch.delenv("BITRIX24_WEBHOOK_URL", raising=False)

    module = importlib.import_module("plugins.bitrix24")

    assert hasattr(module, "register"), (
        "AC-BITRIX24-ARCH REQ-R0: importing plugins.bitrix24 must continue to expose a "
        "register attribute"
    )
    assert callable(module.register)


def test_bitrix24_public_register_symbol_import_still_works(monkeypatch):
    monkeypatch.delenv("BITRIX24_WEBHOOK_URL", raising=False)

    register = getattr(importlib.import_module("plugins.bitrix24"), "register")

    assert callable(register), (
        "AC-BITRIX24-ARCH REQ-R0: from plugins.bitrix24 import register must remain "
        "shape-compatible during the refactor"
    )


def test_bitrix24_leaf_modules_do_not_reverse_lookup_root_package_namespace():
    offenders: dict[str, list[str]] = {}

    for module_name in ("client.py", "attachments.py"):
        module_text = (PLUGIN_DIR / module_name).read_text(encoding="utf-8")
        matches = [pattern for pattern in FORBIDDEN_LEAF_PATTERNS if pattern in module_text]
        if matches:
            offenders[module_name] = matches

    assert not offenders, (
        "AC-BITRIX24-ARCH REQ-R0: extracted Bitrix24 leaf modules must not reverse-couple "
        f"through the root package namespace; offenders={offenders}"
    )
