"""The Bitrix task session can inspect tracked source, but not arbitrary host files."""

import json
import subprocess
from pathlib import Path

import pytest

from tools.approval_context import reset_current_session_key, set_current_session_key


@pytest.fixture
def source(tmp_path: Path, monkeypatch):
    backend = tmp_path / "backend"
    editor = tmp_path / "editor"
    for root, folder, filename, content in (
        (backend, "application/models", "M_catalog.php", "<?php\nfunction catalog_item() {}\n"),
        (editor, "src/services", "catalog.ts", "export function catalogItem() {}\n"),
    ):
        path = root / folder / filename
        path.parent.mkdir(parents=True)
        path.write_text(content)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "--", str(path.relative_to(root))], check=True)
    (backend / "application/models/extra.php").write_text("<?php secret_untracked\n")
    (backend / "application/models/outside.php").symlink_to(tmp_path / "sensitive.php")
    (tmp_path / "sensitive.php").write_text("<?php secret_outside\n")
    config = backend / "application/config/database.php"
    config.parent.mkdir()
    config.write_text("<?php secret_config\n")
    subprocess.run(["git", "-C", str(backend), "add", "--", "application/config/database.php", "application/models/outside.php"], check=True)
    from plugins.bitrix24 import code
    monkeypatch.setattr(code, "_load_extra", lambda: {"code_roots": {"backend": str(backend), "editor": str(editor)}})
    return backend, editor


@pytest.fixture
def task_session():
    token = set_current_session_key("agent:main:bitrix24:group:task:1180:1180")
    try:
        yield
    finally:
        reset_current_session_key(token)


def test_task_can_read_and_search_both_code_roots(source, task_session):
    from plugins.bitrix24.code import _handle_bitrix24_code_read, _handle_bitrix24_code_search

    read = json.loads(_handle_bitrix24_code_read({"root": "backend", "path": "application/models/M_catalog.php", "start_line": 2}))
    assert read["ok"] is True
    assert read["result"]["lines"] == [{"line": 2, "text": "function catalog_item() {}"}]
    found = json.loads(_handle_bitrix24_code_search({"root": "editor", "query": "catalogItem"}))
    assert found["ok"] is True
    assert found["result"]["matches"] == [{"path": "src/services/catalog.ts", "line": 1, "text": "export function catalogItem() {}"}]


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.php", "application/models/extra.php", "application/models/outside.php", "application/config/database.php", "../../.env"])
def test_read_rejects_paths_outside_tracked_code(source, task_session, path):
    from plugins.bitrix24.code import _handle_bitrix24_code_read

    result = json.loads(_handle_bitrix24_code_read({"root": "backend", "path": path}))
    assert result["ok"] is False
    assert "secret" not in json.dumps(result)


def test_search_ignores_untracked_and_non_source_files(source, task_session):
    from plugins.bitrix24.code import _handle_bitrix24_code_search

    result = json.loads(_handle_bitrix24_code_search({"root": "backend", "query": "secret_"}))
    assert result["ok"] is True
    assert result["result"]["matches"] == []
    config = json.loads(_handle_bitrix24_code_search({"root": "backend", "query": "secret_config"}))
    assert config["ok"] is True
    assert config["result"]["matches"] == []


@pytest.mark.parametrize("key", ["agent:main:bitrix24:group:chat:1180:1180", "agent:main:telegram:dm:1180", "default", "agent:main:bitrix24:group:task:1180:1190"])
def test_other_sessions_cannot_read_code(source, key):
    from plugins.bitrix24.code import _handle_bitrix24_code_read
    token = set_current_session_key(key)
    try:
        result = json.loads(_handle_bitrix24_code_read({"root": "backend", "path": "application/models/M_catalog.php"}))
    finally:
        reset_current_session_key(token)
    assert result["ok"] is False
    assert "catalog_item" not in json.dumps(result)
