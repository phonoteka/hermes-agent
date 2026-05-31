"""S9 static guard for active Hermes gateway bypass/fake production surfaces."""

from __future__ import annotations

from pathlib import Path


HERMES_REPO_ROOT = Path(__file__).resolve().parents[2]

HERMES_PROD_PATHS = (
    "gateway/run.py",
    "tools/canon_workflow_command.py",
)

# AC-S9-002 allowlist: these snippets are the hardened access-gate implementation
# and are backed by explicit behavior tests below. New occurrences fail until a
# reviewer proves that the line is not a bypass/exception path.
HERMES_ALLOWED_BYPASS_MARKERS = {
    "gateway/run.py": {
        "if command and canonical and is_gateway_known_command(canonical):",
        "if command and is_gateway_known_command(canonical):",
        'if decision == "rewrite":',
        "# quick commands run in the gateway process which",
    },
}

FORBIDDEN_BYPASS_MARKERS = (
    "if command and canonical and is_gateway_known_command(canonical)",
    "if command and is_gateway_known_command(canonical)",
    'if decision == "rewrite"',
    "quick commands run in the gateway process",
)

REQUIRED_GATE_TESTS = {
    "tests/gateway/test_slash_access_gate_closure.py": (
        "test_hook_rewrite_rechecked_after_canonicalization",
        "test_unknown_quick_exec_cannot_bypass_slash_gate",
    ),
    "tests/gateway/test_canon_workflow_command.py": (
        "test_canon_command_requires_task_topic_and_generic_workflow_facade",
        "test_quick_exec_cannot_bypass_canon_command_gates",
    ),
}


def _unallowlisted_bypass_lines(rel_path: str) -> list[str]:
    """Return gateway marker lines that are not backed by the S9 explicit allowlist.

    pre: rel_path names a UTF-8 text file below HERMES_REPO_ROOT.
    post: returned items include path, line number, marker, and stripped source text.
    raises: OSError when the production file cannot be read.
    """

    allowed = HERMES_ALLOWED_BYPASS_MARKERS.get(rel_path, set())
    violations: list[str] = []
    text = (HERMES_REPO_ROOT / rel_path).read_text(encoding="utf-8")
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        for marker in FORBIDDEN_BYPASS_MARKERS:
            if marker in stripped and stripped not in allowed:
                violations.append(f"{rel_path}:{line_number}: marker {marker!r}: {stripped}")
    return violations


def test_active_gateway_prod_paths_have_no_unallowlisted_bypass_markers() -> None:
    """AC-S9-002: bypass-looking gateway markers are allowed only at reviewed hardened gate sites."""

    violations: list[str] = []
    for rel_path in HERMES_PROD_PATHS:
        violations.extend(_unallowlisted_bypass_lines(rel_path))

    assert not violations, "unallowlisted Hermes gateway bypass markers found:\n" + "\n".join(violations)


def test_gateway_bypass_marker_allowlist_is_backed_by_explicit_tests() -> None:
    """AC-S9-002: every allowed bypass marker is paired with executable gate-regression tests."""

    missing: list[str] = []
    for rel_path, test_names in REQUIRED_GATE_TESTS.items():
        text = (HERMES_REPO_ROOT / rel_path).read_text(encoding="utf-8")
        for test_name in test_names:
            if f"def {test_name}" not in text and f"async def {test_name}" not in text:
                missing.append(f"{rel_path}: missing {test_name}")

    assert not missing, "Hermes gateway bypass allowlist lacks explicit tests:\n" + "\n".join(missing)
