"""Regression tests for source-loaded modules that import ``runtime_context``.

These modules are sometimes loaded directly from their subdirectories via
``spec_from_file_location(...)`` in subprocesses and helper entrypoints. In that
shape, the repo root may be missing from ``sys.path`` even though the module's
own directory is present. They must bootstrap the repo root before importing the
repo-root ``runtime_context.py`` seam.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("relative_path", "symbol_name"),
    [
        ("hermes_cli/oneshot.py", "scoped_runtime_cwd"),
        ("tools/file_tools.py", "get_runtime_cwd"),
        ("tools/terminal_tool.py", "get_runtime_cwd"),
        ("agent/prompt_builder.py", "get_runtime_cwd"),
    ],
)
def test_runtime_context_import_survives_subdir_only_sys_path_source_launch(
    relative_path: str,
    symbol_name: str,
) -> None:
    """Direct source loads must repair ``sys.path`` before importing runtime_context.

    pre: the subprocess starts with the target module's own directory on ``sys.path`` but every
         repo-root path entry removed.
    post: the module executes successfully, exposes the expected imported runtime_context symbol,
          and imports ``runtime_context`` from the repo root.
    """

    module_path = REPO_ROOT / relative_path
    loader_name = f"runtime_context_bootstrap_{module_path.stem}"
    script = textwrap.dedent(
        f"""
        import importlib.util
        import json
        import sys
        import types
        from pathlib import Path

        repo_root = Path({str(REPO_ROOT)!r}).resolve()
        module_path = Path({str(module_path)!r}).resolve()
        sys.modules.setdefault(
            "yaml",
            types.SimpleNamespace(
                safe_load=lambda *args, **kwargs: None,
                safe_dump=lambda *args, **kwargs: "",
                dump=lambda *args, **kwargs: "",
            ),
        )
        sys.modules.setdefault("requests", types.SimpleNamespace())
        sys.path = [
            str(module_path.parent),
            *[
                entry
                for entry in sys.path
                if entry
                and not str(Path(entry or '.').resolve()).startswith(str(repo_root))
            ],
        ]

        spec = importlib.util.spec_from_file_location({loader_name!r}, module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"failed to create spec for {{module_path}}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        imported = sys.modules["runtime_context"]
        payload = {{
            "symbol_present": callable(getattr(module, {symbol_name!r}, None)),
            "runtime_context_file": str(Path(imported.__file__).resolve()),
            "repo_root_present": str(repo_root) in sys.path,
        }}
        print(json.dumps(payload))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip())
    assert payload["symbol_present"] is True
    assert payload["repo_root_present"] is True
    assert payload["runtime_context_file"] == str((REPO_ROOT / "runtime_context.py").resolve())
