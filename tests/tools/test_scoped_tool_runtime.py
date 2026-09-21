"""RED coverage for the current Hermes tool-execution authority boundary."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import sys
from types import SimpleNamespace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations

import tools.approval as approval
import tools.code_execution_tool as code_execution_tool
import tools.file_tools as file_tools
import tools.terminal_tool as terminal_tool
from tools.terminal_scope import get_terminal_scope, reset_terminal_scope, set_terminal_scope


_SCOPE_IMPORT_ERROR: Exception | None = None
_SCOPE_MODULE: Any = None
try:
    import tools.tool_execution_scope as _SCOPE_MODULE
    from tools.tool_execution_scope import (
        ToolExecutionScope,
        bind_tool_execution_scope,
        get_tool_execution_scope,
    )
except (ImportError, ModuleNotFoundError) as exc:
    ToolExecutionScope = None  # type: ignore[assignment,misc]
    bind_tool_execution_scope = None  # type: ignore[assignment]
    get_tool_execution_scope = None  # type: ignore[assignment]
    _SCOPE_IMPORT_ERROR = exc


def _require_scope_api() -> tuple[Any, Any, Any]:
    if _SCOPE_IMPORT_ERROR is not None:
        pytest.fail(f"current ToolExecutionScope owner is unavailable: {_SCOPE_IMPORT_ERROR}")
    assert ToolExecutionScope is not None
    assert bind_tool_execution_scope is not None
    assert get_tool_execution_scope is not None
    return ToolExecutionScope, bind_tool_execution_scope, get_tool_execution_scope


def test_scope_owner_is_loaded_from_current_checkout() -> None:
    """Editable-install fallback must not masquerade as current source ownership."""
    expected = Path(__file__).resolve().parents[2] / "tools" / "tool_execution_scope.py"
    actual = Path(_SCOPE_MODULE.__file__).resolve() if _SCOPE_MODULE is not None else None
    assert expected.is_file(), f"current ToolExecutionScope owner is absent: {expected}"
    assert actual == expected, f"ToolExecutionScope resolved from foreign checkout: {actual}"


def _scope(
    root: Path,
    *,
    working_directory: Path | None = None,
    read_roots: tuple[Path, ...] | None = None,
    write_roots: tuple[Path, ...] | None = None,
    read_allowlist: tuple[str, ...] = ("**",),
    write_allowlist: tuple[str, ...] = ("**",),
    mode: str = "read-write",
) -> Any:
    scope_type, _, _ = _require_scope_api()
    return scope_type(
        run_id="run-red-1",
        phase_id="proof_loop_build_fix",
        attempt_ref="2",
        root_directory=str(root),
        working_directory=str(working_directory or root),
        read_roots=tuple(str(path) for path in (read_roots if read_roots is not None else (root,))),
        write_roots=tuple(str(path) for path in (write_roots if write_roots is not None else (root,))),
        sandbox_profile_id="workspace-write",
        mode=mode,
        read_allowlist=read_allowlist,
        write_allowlist=write_allowlist,
    )


@pytest.fixture
def scoped_dirs(tmp_path: Path) -> tuple[Path, Path, Any]:
    outside_root = tmp_path / "outside-root"
    target_root = tmp_path / "target-root"
    (outside_root / "generated").mkdir(parents=True)
    target_root.mkdir(parents=True)
    (outside_root / "generated" / "hello-world.txt").write_text("scope-contaminant\n", encoding="utf-8")
    return outside_root, target_root, _scope(target_root)


def _real_file_ops(cwd: Path) -> ShellFileOperations:
    return ShellFileOperations(LocalEnvironment(cwd=str(cwd), timeout=15), cwd=str(cwd))


def _configure_terminal_runtime(
    monkeypatch: pytest.MonkeyPatch, *, task_id: str, env: Any, cwd: Path
) -> None:
    monkeypatch.setattr(terminal_tool, "_active_environments", {task_id: env})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "local", "cwd": str(cwd), "timeout": 15, "lifetime_seconds": 3600},
    )
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_resolve_container_task_id", lambda value: value or "default")
    monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda *args, **kwargs: {"approved": True})


def test_scoped_relative_file_read_uses_scope_working_directory_not_contaminant(
    monkeypatch: pytest.MonkeyPatch, scoped_dirs
) -> None:
    outside_root, _target_root, scope = scoped_dirs
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": _real_file_ops(outside_root))

    with bind_tool_execution_scope(scope):
        result = json.loads(file_tools.read_file_tool("generated/hello-world.txt", task_id="scoped-relative-read"))

    assert "error" in result
    assert "scope-contaminant" not in json.dumps(result)


def test_scoped_search_tree_hides_outside_contaminant(
    monkeypatch: pytest.MonkeyPatch, scoped_dirs
) -> None:
    outside_root, _target_root, scope = scoped_dirs
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": _real_file_ops(outside_root))

    with bind_tool_execution_scope(scope):
        raw = file_tools.search_tool(
            pattern="hello-world.txt",
            target="files",
            path=".",
            task_id="scoped-search-tree",
        )

    payload = json.loads(raw)
    assert payload.get("total_count") == 0
    assert payload.get("files", []) == []
    assert str(outside_root) not in raw


def test_scoped_v4a_rewrites_relative_headers_to_scope_root(
    monkeypatch: pytest.MonkeyPatch, scoped_dirs
) -> None:
    _outside_root, target_root, scope = scoped_dirs
    (target_root / "note.txt").write_text("hello\n", encoding="utf-8")
    mock_ops = MagicMock()
    result_obj = MagicMock()
    result_obj.to_dict.return_value = {"success": True}
    mock_ops.patch_v4a.return_value = result_obj
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": mock_ops)

    patch_body = "*** Begin Patch\n*** Update File: note.txt\n@@\n-hello\n+hola\n*** End Patch"
    with bind_tool_execution_scope(scope):
        result = json.loads(file_tools.patch_tool(mode="patch", patch=patch_body, task_id="scoped-v4a-relative"))

    assert result["success"] is True
    forwarded_patch = mock_ops.patch_v4a.call_args.args[0]
    assert f"*** Update File: {target_root / 'note.txt'}" in forwarded_patch


def test_scoped_v4a_authorizes_every_parser_path_before_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    allowed = root / "allowed"
    forbidden = root / "forbidden"
    allowed.mkdir(parents=True)
    forbidden.mkdir()
    source = allowed / "source.txt"
    destination = forbidden / "moved.txt"
    secret = forbidden / "secret.txt"
    source_bytes = b"source bytes\n"
    secret_bytes = b"secret bytes\n"
    source.write_bytes(source_bytes)
    secret.write_bytes(secret_bytes)
    scope = _scope(root, read_allowlist=("allowed/**",), write_allowlist=("allowed/**",))
    monkeypatch.setattr(file_tools, "_resolve_path_for_task", lambda path, task_id: (root / path).resolve())
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": _real_file_ops(root))

    with bind_tool_execution_scope(scope):
        move_result = json.loads(
            file_tools.patch_tool(
                mode="patch",
                patch="*** Begin Patch\n*** Move File: allowed/source.txt -> forbidden/moved.txt\n*** End Patch",
                task_id="scoped-v4a-parser",
            )
        )
        compact_update_result = json.loads(
            file_tools.patch_tool(
                mode="patch",
                patch=(
                    "*** Begin Patch\n"
                    "***Update File: forbidden/secret.txt\n"
                    "@@\n"
                    "-secret bytes\n"
                    "+changed secret bytes\n"
                    "*** End Patch"
                ),
                task_id="scoped-v4a-parser",
            )
        )

    assert "outside active effect write allowlist" in move_result.get("error", "")
    assert source.exists() and source.read_bytes() == source_bytes
    assert not destination.exists()
    assert "outside active effect write allowlist" in compact_update_result.get("error", "")
    assert secret.read_bytes() == secret_bytes


def test_scope_is_immutable_and_validates_identity_and_directories(tmp_path: Path) -> None:
    scope_type, _, _ = _require_scope_api()
    root = tmp_path / "root"
    root.mkdir()
    scope = _scope(root)

    assert scope.run_id == "run-red-1"
    assert scope.phase_id == "proof_loop_build_fix"
    assert scope.attempt_ref == "2"
    assert scope.sandbox_profile_id == "workspace-write"
    assert scope.root_directory == str(root.resolve())
    assert scope.working_directory == str(root.resolve())
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.mode = "read-only"

    with pytest.raises(ValueError, match="run_id is required"):
        scope_type(
            run_id=" ",
            phase_id="phase",
            attempt_ref="1",
            root_directory=str(root),
            working_directory=str(root),
            read_roots=(str(root),),
            write_roots=(),
            sandbox_profile_id="profile",
            mode="read-only",
            read_allowlist=("**",),
            write_allowlist=(),
        )

    with pytest.raises(ValueError, match="absolute existing directory"):
        scope_type(
            run_id="run",
            phase_id="phase",
            attempt_ref="1",
            root_directory="relative-root",
            working_directory="relative-root",
            read_roots=(),
            write_roots=(),
            sandbox_profile_id="profile",
            mode="read-only",
            read_allowlist=(),
            write_allowlist=(),
        )

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="working_directory must stay"):
        _scope(root, working_directory=outside)


def test_scope_rejects_undeclared_roots_allowlists_and_modes(tmp_path: Path) -> None:
    scope_type, _, _ = _require_scope_api()
    root = tmp_path / "root"
    read_root = root / "read"
    write_root = root / "write"
    outside = tmp_path / "root-sibling"
    read_root.mkdir(parents=True)
    write_root.mkdir()
    outside.mkdir()

    with pytest.raises(ValueError, match="read_roots entries"):
        _scope(root, read_roots=(outside,))
    with pytest.raises(ValueError, match="canonical root-relative"):
        _scope(root, read_allowlist=("../outside",))
    with pytest.raises(ValueError, match="canonical root-relative"):
        _scope(root, write_allowlist=(str(write_root),))
    with pytest.raises(ValueError, match="read-only mode"):
        _scope(root, mode="read-only", write_roots=(), write_allowlist=("**",))
    with pytest.raises(ValueError, match="requires write_roots"):
        scope_type(
            run_id="run",
            phase_id="phase",
            attempt_ref="1",
            root_directory=str(root),
            working_directory=str(root),
            read_roots=(str(root),),
            write_roots=(),
            sandbox_profile_id="profile",
            mode="read-write",
            read_allowlist=("**",),
            write_allowlist=(),
        )


def test_scope_refuses_lexical_resolved_sibling_and_symlink_escape(tmp_path: Path) -> None:
    _, _, _ = _require_scope_api()
    root = tmp_path / "root"
    allowed = root / "allowed"
    outside = tmp_path / "root-sibling"
    root.mkdir()
    allowed.mkdir()
    outside.mkdir()
    outside_file = outside / "secret.txt"
    outside_file.write_text("outside", encoding="utf-8")
    link = allowed / "link.txt"
    link.symlink_to(outside_file)
    scope = _scope(root, read_allowlist=("allowed/**",), write_allowlist=("allowed/**",))

    lexical = scope.resolve_path("../root-sibling/secret.txt")
    assert not scope.contains(lexical)
    assert not scope.allows_read(lexical)
    assert not scope.allows_write(lexical)

    resolved_symlink = scope.resolve_path("allowed/link.txt")
    assert resolved_symlink == outside_file.resolve()
    assert not scope.contains(resolved_symlink)
    assert not scope.allows_read(resolved_symlink)
    assert not scope.allows_write(resolved_symlink)


def test_scope_enforces_read_write_roots_and_allowlists(tmp_path: Path) -> None:
    _, _, _ = _require_scope_api()
    root = tmp_path / "root"
    read_dir = root / "read"
    write_dir = root / "write"
    undeclared = root / "undeclared"
    read_dir.mkdir(parents=True)
    write_dir.mkdir()
    undeclared.mkdir()
    readable = read_dir / "input.txt"
    writable = write_dir / "output.txt"
    readable.write_text("input", encoding="utf-8")
    scope = _scope(
        root,
        read_roots=(read_dir,),
        write_roots=(write_dir,),
        read_allowlist=("read/**",),
        write_allowlist=("write/**",),
    )

    assert scope.allows_read(readable)
    assert not scope.allows_write(readable)
    assert scope.allows_write(writable)
    assert not scope.allows_read(writable)
    assert not scope.allows_read(undeclared / "hidden.txt")
    assert not scope.allows_write(undeclared / "blocked.txt")
    assert scope.allows_read_tree(read_dir)
    assert not scope.allows_read_tree(undeclared)


def test_scope_binding_is_nested_and_context_local(tmp_path: Path) -> None:
    _, bind, get_scope = _require_scope_api()
    root = tmp_path / "root"
    root.mkdir()
    outer = _scope(root)
    inner_root = root / "inner"
    inner_root.mkdir()
    inner = _scope(inner_root)

    assert get_scope() is None
    with bind(outer):
        assert get_scope() == outer
        with bind(inner):
            assert get_scope() == inner
        assert get_scope() == outer
    assert get_scope() is None

    def observe(local_scope: Any) -> Any:
        with bind(local_scope):
            return get_scope()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        observed = list(pool.map(observe, (outer, inner)))
    assert observed == [outer, inner]
    assert get_scope() is None


def test_file_consumers_reject_undeclared_read_write_before_backend_io(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, bind, _ = _require_scope_api()
    root = tmp_path / "root"
    allowed = root / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir(parents=True)
    outside.mkdir()
    outside_file = outside / "secret.txt"
    outside_file.write_text("secret", encoding="utf-8")
    scope = _scope(root, read_allowlist=("allowed/**",), write_allowlist=("allowed/**",))
    backend = MagicMock()
    read_backend_result = MagicMock()
    read_backend_result.content = ""
    read_backend_result.to_dict.return_value = {"error": "backend reached"}
    backend.read_file.return_value = read_backend_result
    write_backend_result = MagicMock()
    write_backend_result.to_dict.return_value = {"status": "ok"}
    backend.write_file.return_value = write_backend_result
    patch_backend_result = MagicMock()
    patch_backend_result.to_dict.return_value = {"status": "ok"}
    backend.patch_replace.return_value = patch_backend_result
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": backend)

    with bind(scope):
        read_result = json.loads(file_tools.read_file_tool(str(outside_file), task_id="scope-read"))
        write_result = json.loads(
            file_tools.write_file_tool(str(outside / "blocked.txt"), "blocked", task_id="scope-write")
        )
        patch_result = json.loads(
            file_tools.patch_tool(
                mode="replace",
                path=str(outside_file),
                old_string="secret",
                new_string="changed",
                task_id="scope-patch",
            )
        )

    assert "error" in read_result
    backend.read_file.assert_not_called()
    backend.write_file.assert_not_called()
    backend.patch_replace.assert_not_called()
    assert outside_file.read_text(encoding="utf-8") == "secret"


def test_terminal_consumer_refuses_working_directory_escape_before_execute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, bind, _ = _require_scope_api()
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    scope = _scope(root)
    env = MagicMock()
    env.execute.return_value = {"output": "unexpected", "returncode": 0}
    task_id = "scope-terminal"
    monkeypatch.setattr(terminal_tool, "_active_environments", {task_id: env})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "local", "cwd": str(root), "timeout": 15, "lifetime_seconds": 3600},
    )
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_resolve_container_task_id", lambda value: value or "default")
    monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda *args, **kwargs: {"approved": True})

    with bind(scope):
        result = json.loads(
            terminal_tool.terminal_tool(command="printf blocked", task_id=task_id, workdir=str(outside))
        )

    assert result.get("exit_code", -1) != 0 or result.get("status") in {"blocked", "error"}
    env.execute.assert_not_called()


def test_scoped_terminal_read_only_scope_refuses_write_before_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    scope = _scope(root, mode="read-only", write_roots=(), write_allowlist=())
    env = LocalEnvironment(cwd=str(root), timeout=15)
    task_id = "scoped-terminal-read-only"
    _configure_terminal_runtime(monkeypatch, task_id=task_id, env=env, cwd=root)

    with bind_tool_execution_scope(scope):
        result = json.loads(
            terminal_tool.terminal_tool(
                command="printf forbidden > read-only-sentinel.txt", task_id=task_id
            )
        )

    assert result.get("exit_code", -1) != 0 or result.get("status") in {"blocked", "error"}
    assert not (root / "read-only-sentinel.txt").exists()


def test_scoped_terminal_selects_allowed_scope_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    scope = _scope(root)
    env = MagicMock()
    env.execute.side_effect = lambda command, **kwargs: {
        "output": kwargs.get("cwd", ""), "returncode": 0
    }
    task_id = "scoped-terminal-working-directory"
    _configure_terminal_runtime(monkeypatch, task_id=task_id, env=env, cwd=outside)

    with bind_tool_execution_scope(scope):
        result = json.loads(terminal_tool.terminal_tool(command="pwd", task_id=task_id))

    assert result["exit_code"] == 0
    assert result["output"] == str(root)
    assert env.execute.call_args.kwargs["cwd"] == str(root)


def test_scoped_terminal_blocks_relative_traversal_read_and_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outside_root = tmp_path / "outside-root"
    target_root = tmp_path / "target-root"
    (outside_root / "generated").mkdir(parents=True)
    target_root.mkdir()
    contaminant = outside_root / "generated" / "hello-world.txt"
    contaminant.write_text("scope-contaminant\n", encoding="utf-8")
    outside_target = outside_root / "generated" / "written.txt"
    scope = _scope(target_root)
    env = LocalEnvironment(cwd=str(target_root), timeout=15)
    task_id = "scoped-terminal-traversal"
    _configure_terminal_runtime(monkeypatch, task_id=task_id, env=env, cwd=target_root)

    with bind_tool_execution_scope(scope):
        read_result = json.loads(
            terminal_tool.terminal_tool(
                command="cat ../outside-root/generated/hello-world.txt",
                task_id=task_id,
                workdir=str(target_root),
            )
        )
        write_result = json.loads(
            terminal_tool.terminal_tool(
                command="printf escaped > ../outside-root/generated/written.txt",
                task_id=task_id,
                workdir=str(target_root),
            )
        )

    assert read_result["exit_code"] != 0
    assert "scope-contaminant" not in read_result.get("output", "")
    assert write_result["exit_code"] != 0
    assert not outside_target.exists()


def test_scoped_terminal_blocks_symlink_and_interpreter_escapes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outside_root = tmp_path / "outside-root"
    target_root = tmp_path / "target-root"
    (outside_root / "generated").mkdir(parents=True)
    target_root.mkdir()
    contaminant = outside_root / "generated" / "hello-world.txt"
    contaminant.write_text("scope-contaminant\n", encoding="utf-8")
    escape_link = target_root / "escaped.txt"
    escape_link.symlink_to(contaminant)
    scope = _scope(target_root)
    env = LocalEnvironment(cwd=str(target_root), timeout=15)
    task_id = "scoped-terminal-escape"
    _configure_terminal_runtime(monkeypatch, task_id=task_id, env=env, cwd=target_root)

    with bind_tool_execution_scope(scope):
        symlink_result = json.loads(
            terminal_tool.terminal_tool(
                command="cat escaped.txt", task_id=task_id, workdir=str(target_root)
            )
        )
        interpreter_result = json.loads(
            terminal_tool.terminal_tool(
                command=(
                    "python3 -c \"from pathlib import Path; "
                    "print('interpreter-started', flush=True); "
                    "print(Path('../outside-root/generated/hello-world.txt').read_text())\""
                ),
                task_id=task_id,
                workdir=str(target_root),
            )
        )

    assert "interpreter-started" in interpreter_result.get("output", "")
    for result in (symlink_result, interpreter_result):
        assert result["exit_code"] != 0
        assert "scope-contaminant" not in result.get("output", "")


def test_scoped_terminal_intersects_globs_with_effect_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    readable = root / "readable"
    writable = root / "writable"
    forbidden = root / "forbidden"
    for directory in (readable, writable, forbidden):
        directory.mkdir(parents=True, exist_ok=True)
    (readable / "allowed.txt").write_text("allowed-read\n", encoding="utf-8")
    (forbidden / "secret.txt").write_text("forbidden-read\n", encoding="utf-8")
    scope = _scope(
        root,
        read_roots=(readable,),
        write_roots=(writable,),
        read_allowlist=("**",),
        write_allowlist=("**",),
    )
    env = LocalEnvironment(cwd=str(root), timeout=15)
    task_id = "scoped-terminal-effect-roots"
    _configure_terminal_runtime(monkeypatch, task_id=task_id, env=env, cwd=root)

    with bind_tool_execution_scope(scope):
        allowed_read = json.loads(
            terminal_tool.terminal_tool(command="cat readable/allowed.txt", task_id=task_id)
        )
        allowed_write = json.loads(
            terminal_tool.terminal_tool(command="printf allowed > writable/created.txt", task_id=task_id)
        )
        forbidden_read = json.loads(
            terminal_tool.terminal_tool(command="cat forbidden/secret.txt", task_id=task_id)
        )
        forbidden_write = json.loads(
            terminal_tool.terminal_tool(command="printf escaped > forbidden/created.txt", task_id=task_id)
        )

    assert allowed_read["exit_code"] == 0
    assert "allowed-read" in allowed_read["output"]
    assert allowed_write["exit_code"] == 0
    assert (writable / "created.txt").read_text(encoding="utf-8") == "allowed"
    assert forbidden_read["exit_code"] != 0
    assert "forbidden-read" not in forbidden_read.get("output", "")
    assert forbidden_write["exit_code"] != 0
    assert not (forbidden / "created.txt").exists()


def test_scoped_terminal_background_uses_same_landlock_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    scope = _scope(root)
    env = LocalEnvironment(cwd=str(root), timeout=15)
    task_id = "scoped-terminal-background"
    captured: dict[str, Any] = {}
    _configure_terminal_runtime(monkeypatch, task_id=task_id, env=env, cwd=root)

    def capture_background(**kwargs: Any) -> str:
        captured.update(kwargs)
        return json.dumps(
            {"status": "running", "session_id": "proc_scoped", "exit_code": None, "output": ""}
        )

    monkeypatch.setattr(terminal_tool, "spawn_background_process", capture_background)
    with bind_tool_execution_scope(scope):
        result = json.loads(
            terminal_tool.terminal_tool(
                command="printf guarded > background.txt",
                task_id=task_id,
                background=True,
            )
        )

    assert result["session_id"] == "proc_scoped"
    assert "--landlock-launch" in captured["command"]
    assert "tool_execution_scope.py" in captured["command"]
    assert "printf guarded > background.txt" in captured["command"]


def test_code_consumer_refuses_before_unrestricted_kernel_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, bind, _ = _require_scope_api()
    root = tmp_path / "root"
    root.mkdir()
    scope = _scope(root)
    called = False

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {"env_type": "local"})
    monkeypatch.setattr(terminal_tool, "_docker_has_host_access", lambda _config: False)
    monkeypatch.setattr(
        code_execution_tool,
        "_load_config",
        lambda: {"timeout": 15, "max_tool_calls": 5, "mode": "project"},
    )
    monkeypatch.setattr(
        approval,
        "check_execute_code_guard",
        lambda *args, **kwargs: {"approved": True},
    )
    monkeypatch.setattr(code_execution_tool, "_get_execution_mode", lambda: "project")

    def sentinel_kernel(*args: Any, **kwargs: Any) -> str:
        nonlocal called
        called = True
        return json.dumps({"status": "success", "output": "unrestricted", "tool_calls_made": 0})

    monkeypatch.setitem(
        sys.modules,
        "tools.code_kernel",
        SimpleNamespace(execute_in_session_kernel=sentinel_kernel),
    )
    with bind(scope):
        result = json.loads(code_execution_tool.execute_code("print('safe')", task_id="scope-code"))

    assert "error" in result
    assert not called


def test_terminal_scope_context_local_control_remains_green() -> None:
    token = set_terminal_scope({"TERMINAL_ENV": "local"})
    try:
        assert get_terminal_scope() == {"TERMINAL_ENV": "local"}
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(get_terminal_scope).result() is None
    finally:
        reset_terminal_scope(token)
    assert get_terminal_scope() is None


def test_unbound_file_handler_control_remains_green(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MagicMock()
    result = MagicMock()
    result.content = "control"
    result.to_dict.return_value = {"content": "control", "total_lines": 1}
    backend.read_file.return_value = result
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": backend)

    payload = json.loads(file_tools.read_file_tool("/tmp/unbound-control.txt", task_id="unbound-control"))
    assert payload["content"] == "control"
    backend.read_file.assert_called_once_with("/tmp/unbound-control.txt", 1, file_tools.DEFAULT_READ_LIMIT)


def test_unbound_terminal_control_remains_green(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    task_id = "unbound-terminal-control"
    env = MagicMock()
    env.execute.return_value = {"output": "terminal-ok", "returncode": 0}
    monkeypatch.setattr(terminal_tool, "_active_environments", {task_id: env})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {"env_type": "local", "cwd": str(tmp_path), "timeout": 15, "lifetime_seconds": 3600},
    )
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_resolve_container_task_id", lambda value: value or "default")
    monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda *args, **kwargs: {"approved": True})

    payload = json.loads(terminal_tool.terminal_tool(command="printf terminal-ok", task_id=task_id))
    assert payload["exit_code"] == 0
    assert payload["output"] == "terminal-ok"
    env.execute.assert_called_once()
