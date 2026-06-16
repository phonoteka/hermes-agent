from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.oneshot import _temporary_terminal_cwd_env
from runtime_context import scoped_runtime_cwd
from tools import terminal_tool
from tools.file_operations import ShellFileOperations
from tools.file_tools import (
    _effective_file_tool_cwd,
    _file_ops_cache,
    _file_ops_lock,
    _read_tracker,
    patch_tool,
    read_file_tool,
    search_tool,
    write_file_tool,
)


class _FakeEnv:
    """Minimal terminal env for ShellFileOperations integration tests."""

    def __init__(self, start_cwd: str):
        self.cwd = start_cwd
        self.calls: list[dict] = []

    def execute(self, command: str, cwd: str = None, **kwargs) -> dict:
        import subprocess

        self.calls.append({"command": command, "cwd": cwd})
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=cwd or self.cwd,
            input=kwargs.get("stdin_data"),
            capture_output=True,
            text=True,
        )
        return {
            "output": proc.stdout + proc.stderr,
            "returncode": proc.returncode,
        }


@pytest.fixture(autouse=True)
def _isolate_default_task(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    with _file_ops_lock:
        previous = _file_ops_cache.pop("default", None)
    with terminal_tool._env_lock:
        previous_env = terminal_tool._active_environments.pop("default", None)
    _read_tracker.clear()
    try:
        yield
    finally:
        with _file_ops_lock:
            _file_ops_cache.pop("default", None)
            if previous is not None:
                _file_ops_cache["default"] = previous
        with terminal_tool._env_lock:
            terminal_tool._active_environments.pop("default", None)
            if previous_env is not None:
                terminal_tool._active_environments["default"] = previous_env
        _read_tracker.clear()


def _seed_workspace(tmp_path: Path) -> tuple[Path, Path]:
    source_root = tmp_path / "source_root"
    target_repo = tmp_path / "target_repo"
    (source_root / "generated").mkdir(parents=True)
    (target_repo / "generated").mkdir(parents=True)
    (source_root / "generated" / "hello-world.txt").write_text("slice-1\n")
    (target_repo / "generated" / "hello-world.txt").write_text(
        "foundation: ready\nhello: assembled\n"
    )
    return source_root, target_repo


def _prewarm_default_cache(source_root: Path) -> ShellFileOperations:
    fake_env = _FakeEnv(start_cwd=str(source_root))
    ops = ShellFileOperations(fake_env, cwd=str(source_root))
    with terminal_tool._env_lock:
        terminal_tool._active_environments["default"] = fake_env
    with _file_ops_lock:
        _file_ops_cache["default"] = ops
    return ops


def test_read_file_tool_prefers_scoped_runtime_cwd_over_cached_live_cwd(tmp_path):
    source_root, target_repo = _seed_workspace(tmp_path)
    _prewarm_default_cache(source_root)

    with scoped_runtime_cwd(str(target_repo)):
        result = json.loads(read_file_tool("generated/hello-world.txt", task_id="default"))

    assert "error" not in result
    assert "foundation: ready" in result["content"]
    assert "hello: assembled" in result["content"]
    assert "slice-1" not in result["content"]


def test_search_tool_prefers_scoped_runtime_cwd_over_cached_live_cwd(tmp_path):
    source_root, target_repo = _seed_workspace(tmp_path)
    _prewarm_default_cache(source_root)

    with scoped_runtime_cwd(str(target_repo)):
        result = json.loads(
            search_tool(
                pattern="hello-world.txt",
                target="files",
                path=".",
                task_id="default",
            )
        )

    files = result.get("files", [])
    assert files == [str(target_repo / "generated" / "hello-world.txt")]
    assert str(source_root / "generated" / "hello-world.txt") not in files


def test_oneshot_temporary_terminal_cwd_env_projects_into_public_file_tools(tmp_path):
    source_root, target_repo = _seed_workspace(tmp_path)
    _prewarm_default_cache(source_root)

    with _temporary_terminal_cwd_env(str(target_repo)):
        read_result = json.loads(read_file_tool("generated/hello-world.txt", task_id="default"))
        search_result = json.loads(
            search_tool(
                pattern="hello-world.txt",
                target="files",
                path=".",
                task_id="default",
            )
        )

    assert "error" not in read_result
    assert "foundation: ready" in read_result["content"]
    assert "hello: assembled" in read_result["content"]
    assert "slice-1" not in read_result["content"]

    files = search_result.get("files", [])
    assert files == [str(target_repo / "generated" / "hello-world.txt")]
    assert str(source_root / "generated" / "hello-world.txt") not in files


def test_effective_file_tool_cwd_logs_scoped_cached_mismatch_and_keeps_scoped(tmp_path, caplog):
    source_root, target_repo = _seed_workspace(tmp_path)
    _prewarm_default_cache(source_root)

    with _temporary_terminal_cwd_env(str(target_repo)), caplog.at_level("INFO"):
        effective = _effective_file_tool_cwd("default")

    assert effective == target_repo.resolve()
    records = [record for record in caplog.records if record.msg == "file_tool.cwd_authority_mismatch"]
    assert records, "expected cwd mismatch observability log"
    record = records[-1]
    assert getattr(record, "file_tool.cwd_authority") == "scoped_runtime_cwd"
    assert getattr(record, "file_tool.cached_env_cwd") == str(source_root.resolve())
    assert getattr(record, "file_tool.effective_cwd") == str(target_repo.resolve())
    assert getattr(record, "file_tool.task_id") == "default"


def test_effective_file_tool_cwd_prefers_live_cwd_over_ambient_terminal_cwd(tmp_path, monkeypatch):
    source_root, target_repo = _seed_workspace(tmp_path)
    _prewarm_default_cache(source_root)
    monkeypatch.setenv("TERMINAL_CWD", str(target_repo))

    effective = _effective_file_tool_cwd("default")

    assert effective == source_root.resolve()


def test_write_file_tool_mutates_scoped_target_repo_only(tmp_path):
    source_root, target_repo = _seed_workspace(tmp_path)
    _prewarm_default_cache(source_root)

    source_path = source_root / "generated" / "out.txt"
    target_path = target_repo / "generated" / "out.txt"
    source_path.write_text("source-root baseline\n")

    with scoped_runtime_cwd(str(target_repo)):
        result = json.loads(
            write_file_tool("generated/out.txt", "target-repo only\n", task_id="default")
        )

    assert "error" not in result
    assert target_path.read_text() == "target-repo only\n"
    assert source_path.read_text() == "source-root baseline\n"


def test_patch_tool_replace_mode_mutates_scoped_target_repo_only(tmp_path):
    source_root, target_repo = _seed_workspace(tmp_path)
    _prewarm_default_cache(source_root)

    source_path = source_root / "generated" / "patch.txt"
    target_path = target_repo / "generated" / "patch.txt"
    source_path.write_text("alpha\nsource-root\nomega\n")
    target_path.write_text("alpha\ntarget-repo\nomega\n")

    with scoped_runtime_cwd(str(target_repo)):
        result = json.loads(
            patch_tool(
                mode="replace",
                path="generated/patch.txt",
                old_string="target-repo",
                new_string="patched target",
                task_id="default",
            )
        )

    assert result.get("success") is True
    assert target_path.read_text() == "alpha\npatched target\nomega\n"
    assert source_path.read_text() == "alpha\nsource-root\nomega\n"


def test_patch_tool_v4a_mode_mutates_scoped_target_repo_only(tmp_path):
    source_root, target_repo = _seed_workspace(tmp_path)
    _prewarm_default_cache(source_root)

    source_path = source_root / "generated" / "v4a.txt"
    target_path = target_repo / "generated" / "v4a.txt"
    source_path.write_text("header\nsource-root\nfooter\n")
    target_path.write_text("header\ntarget-repo\nfooter\n")

    patch = """*** Begin Patch
*** Update File: generated/v4a.txt
@@
 header
-target-repo
+target-repo patched
 footer
*** End Patch
"""

    with scoped_runtime_cwd(str(target_repo)):
        result = json.loads(patch_tool(mode="patch", patch=patch, task_id="default"))

    assert result.get("success") is True
    assert target_path.read_text() == "header\ntarget-repo patched\nfooter\n"
    assert source_path.read_text() == "header\nsource-root\nfooter\n"
