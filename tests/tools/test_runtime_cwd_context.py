"""Regression tests for turn-scoped runtime cwd propagation."""

from runtime_context import scoped_runtime_cwd
from tools import file_tools, terminal_tool


def test_file_tools_resolve_relative_paths_from_runtime_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.setattr(file_tools, "_get_live_tracking_cwd", lambda task_id: None)

    with scoped_runtime_cwd(str(tmp_path)):
        resolved = file_tools._resolve_path_for_task("generated/hello-world.txt", task_id="canon-phase")

    assert resolved == (tmp_path / "generated" / "hello-world.txt").resolve()


def test_terminal_env_config_prefers_runtime_cwd_over_process_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(terminal_tool.os, "getcwd", lambda: "/wrong/process/cwd")

    with scoped_runtime_cwd(str(tmp_path)):
        config = terminal_tool._get_env_config()

    assert config["cwd"] == str(tmp_path)