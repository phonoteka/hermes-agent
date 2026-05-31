"""Tests for the Kanban tool surface (tools/kanban_tools.py).

Verifies:
  - Tools are gated on HERMES_KANBAN_TASK: a normal chat session sees
    zero kanban tools in its schema; a worker session sees the kanban set.
  - Each handler's happy path.
  - Error paths (missing required args, bad metadata type, etc).
"""
from __future__ import annotations

import json
import os

import pytest


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_kanban_tools_hidden_without_env_var(monkeypatch, tmp_path):
    """Normal `hermes chat` sessions (no HERMES_KANBAN_TASK) must have
    zero kanban_* tools in their schema."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    assert kanban == set(), (
        f"kanban tools leaked into normal chat schema: {kanban}"
    )


def test_kanban_tools_visible_with_env_var(monkeypatch, tmp_path):
    """Worker sessions get task lifecycle tools, not board-routing tools."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    expected = {
        "kanban_show", "kanban_complete", "kanban_block", "kanban_heartbeat",
        "kanban_comment", "kanban_create", "kanban_link",
    }
    assert kanban == expected, f"expected {expected}, got {kanban}"


def test_worker_with_kanban_toolset_still_hides_board_routing(monkeypatch, tmp_path):
    """Task scope wins over profile config for board-routing tools.

    Even if a worker process happens to also have ``toolsets: [kanban]``
    in its config, the HERMES_KANBAN_TASK env var means it's a focused
    worker and must not see kanban_list / kanban_unblock.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("toolsets:\n  - kanban\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    assert {
        "kanban_list",
        "kanban_unblock",
    }.isdisjoint(kanban), (
        f"Board-routing tools leaked into worker schema: "
        f"{kanban & {'kanban_list', 'kanban_unblock'}}"
    )


def test_kanban_tools_visible_with_toolset_config(monkeypatch, tmp_path):
    """Orchestrator profiles with toolsets: [kanban] see all kanban tools."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("toolsets:\n  - kanban\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    expected = {
        "kanban_list",
        "kanban_show", "kanban_complete", "kanban_block", "kanban_heartbeat",
        "kanban_comment", "kanban_create", "kanban_link",
        "kanban_unblock",
    }
    assert kanban == expected, f"expected {expected}, got {kanban}"


def test_parent_triage_worker_sees_unblock_for_source_card(monkeypatch, tmp_path):
    """Kernel-created parent-triage workers get the narrow board-moving tool.

    Ordinary workers stay scoped to their own card, but an on-block parent
    triage card is created specifically to move one blocked source card after
    local verification, so its schema must include ``kanban_unblock``.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")

    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        source = kb.create_task(
            conn,
            title="phase",
            assignee="proofloopworker",
            idempotency_key="proof-loop:plan:task:S1:BUILD_FIX",
            skills=[kb.PROOF_LOOP_SKILL],
        )
        assert kb.block_task(conn, source, reason="needs parent triage")
        triage = conn.execute(
            "SELECT id FROM tasks WHERE created_by = ?",
            (kb.ON_BLOCK_PARENT_TRIAGE_CREATED_BY,),
        ).fetchone()["id"]
        kb._append_event(
            conn,
            triage,
            "resolver_authority",
            {
                "resolver": "v1",
                "source_task_id": source,
                "allowed_actions": ["complete", "unblock"],
            },
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", triage)

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    assert "kanban_unblock" in names
    assert "kanban_list" not in names


# ---------------------------------------------------------------------------
# Handler happy paths
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Simulate being a worker: HERMES_HOME isolated, HERMES_KANBAN_TASK set
    after we've created the task."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    # Clear dispatcher-scoped run/claim tokens inherited from outer kanban
    # worker processes so local fixture tasks are validated against their own run.
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_CLAIM_LOCK", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def test_show_defaults_to_env_task_id(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_show({})
    d = json.loads(out)
    assert "task" in d
    assert d["task"]["id"] == worker_env
    assert d["task"]["status"] == "running"
    assert "worker_context" in d
    assert "runs" in d


def test_show_explicit_task_id(worker_env):
    """Peek at a different task than the one in env."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="other task", assignee="peer")
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = kt._handle_show({"task_id": other})
    d = json.loads(out)
    assert d["task"]["id"] == other


def test_list_filters_tasks(monkeypatch, worker_env):
    """kanban_list gives orchestrators filtered board discovery."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="alpha", assignee="factory", priority=5)
        b = kb.create_task(conn, title="beta", assignee="reviewer")
        c = kb.create_task(conn, title="gamma", assignee="factory", tenant="other")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_list({"assignee": "factory", "status": "ready", "limit": 10})
    d = json.loads(out)
    ids = [t["id"] for t in d["tasks"]]
    assert ids == [a, c]
    assert d["count"] == 2
    assert d["tasks"][0]["title"] == "alpha"
    assert d["tasks"][0]["parent_count"] == 0
    assert b not in ids

    tenant_out = kt._handle_list({
        "assignee": "factory",
        "status": "ready",
        "tenant": "other",
    })
    tenant_ids = [t["id"] for t in json.loads(tenant_out)["tasks"]]
    assert tenant_ids == [c]


def test_list_rejects_invalid_status(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    out = kt._handle_list({"status": "not-a-state"})
    assert "status must be one of" in json.loads(out).get("error", "")


def test_list_rejects_bad_limit(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_list({"limit": "nope"})).get("error")
    assert json.loads(kt._handle_list({"limit": 0})).get("error")


def test_list_parses_include_archived_string_false(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        live = kb.create_task(conn, title="live task", assignee="factory")
        archived = kb.create_task(conn, title="archived task", assignee="factory")
        assert kb.archive_task(conn, archived)
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_list({
        "assignee": "factory",
        "include_archived": "false",
    })
    ids = [t["id"] for t in json.loads(out)["tasks"]]
    assert live in ids
    assert archived not in ids


def test_list_parses_include_archived_string_true(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        live = kb.create_task(conn, title="live task", assignee="factory")
        archived = kb.create_task(conn, title="archived task", assignee="factory")
        assert kb.archive_task(conn, archived)
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_list({
        "assignee": "factory",
        "include_archived": "true",
    })
    ids = [t["id"] for t in json.loads(out)["tasks"]]
    assert live in ids
    assert archived in ids


def test_list_rejects_bad_include_archived(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    out = kt._handle_list({"include_archived": "sometimes"})
    assert "include_archived must be" in json.loads(out).get("error", "")


def test_complete_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_complete({
        "summary": "got the thing done",
        "metadata": {"files": 2},
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["task_id"] == worker_env
    # Verify via kernel
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.outcome == "completed"
        assert run.summary == "got the thing done"
        assert run.metadata == {"files": 2}
    finally:
        conn.close()


def test_complete_metadata_round_trips_through_show(worker_env):
    """Structured completion metadata should be visible to downstream agents."""
    from tools import kanban_tools as kt

    handoff = {
        "changed_files": ["hermes_cli/kanban.py"],
        "verification": ["pytest tests/tools/test_kanban_tools.py -q"],
        "dependencies": [],
        "blocked_reason": None,
        "retry_notes": "none",
        "residual_risk": ["dashboard rendering not exercised"],
    }

    complete_out = kt._handle_complete({
        "summary": "finished with structured evidence",
        "metadata": handoff,
    })
    assert json.loads(complete_out)["ok"] is True

    show_out = kt._handle_show({"task_id": worker_env})
    shown = json.loads(show_out)
    assert shown["task"]["status"] == "done"
    assert shown["runs"][-1]["summary"] == "finished with structured evidence"
    assert shown["runs"][-1]["metadata"] == handoff


def test_complete_with_result_only(worker_env):
    """`result` alone (without summary) is accepted for legacy compat."""
    from tools import kanban_tools as kt
    out = kt._handle_complete({"result": "legacy result"})
    d = json.loads(out)
    assert d["ok"] is True


def test_complete_rejects_no_handoff(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_complete({})
    assert json.loads(out).get("error"), "should have errored"


def test_complete_rejects_non_dict_metadata(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_complete({"summary": "x", "metadata": [1, 2, 3]})
    assert json.loads(out).get("error")


def test_complete_phantom_card_message_advertises_retry(worker_env):
    """A phantom-card rejection must surface a tool_error that explicitly
    tells the worker the task is still in-flight and how to retry — the
    worker has no other channel to discover that. Regression for #22923,
    where the previous wording read like a terminal failure and workers
    routinely abandoned the run instead of trying again.
    """
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "oops claimed a phantom",
        "created_cards": ["t_phantomdeadbeef"],
    })
    err = json.loads(out).get("error", "")
    assert err, f"expected an error, got {out!r}"
    # Phantom id surfaced verbatim.
    assert "t_phantomdeadbeef" in err
    # The retry-is-supported phrasing — these are the literal cues a
    # worker reads to decide whether to retry vs block/abandon. If a
    # future change rewords the message, these checks will catch the
    # regression. See #22923 for the failure mode.
    assert "still in-flight" in err
    assert "Retry kanban_complete" in err
    assert "created_cards=[]" in err

    # Critically: the task is genuinely still in-flight — the gate
    # rejection did not mutate state, so the worker's retry can land.
    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "running"
    finally:
        conn.close()


def test_complete_retry_with_empty_created_cards_succeeds(worker_env):
    """After a phantom rejection, retrying kanban_complete with
    created_cards=[] (the documented escape hatch) must complete the
    task. Regression for #22923."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Hit the gate first.
    rejected = json.loads(kt._handle_complete({
        "summary": "oops",
        "created_cards": ["t_phantomdeadbeef"],
    }))
    assert rejected.get("error")

    # Retry with the escape hatch.
    ok = json.loads(kt._handle_complete({
        "summary": "retry without claims",
        "created_cards": [],
    }))
    assert ok.get("ok") is True

    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "done"
    finally:
        conn.close()


def test_complete_retry_with_corrected_created_cards_succeeds(worker_env):
    """After a phantom rejection, retrying kanban_complete with a
    corrected created_cards list (phantom ids removed) must complete the
    task. Regression for #22923."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Create a real child via the tool so it gets the worker-profile
    # attribution the gate trusts.
    child = json.loads(kt._handle_create({
        "title": "real child", "assignee": "peer",
    }))
    assert child["ok"]
    real_id = child["task_id"]

    # First attempt mixes real + phantom — gate rejects.
    rejected = json.loads(kt._handle_complete({
        "summary": "oops",
        "created_cards": [real_id, "t_phantomdeadbeef"],
    }))
    assert rejected.get("error")
    assert "t_phantomdeadbeef" in rejected["error"]

    # Retry with corrected list.
    ok = json.loads(kt._handle_complete({
        "summary": "retry with corrected list",
        "created_cards": [real_id],
    }))
    assert ok.get("ok") is True

    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "done"
    finally:
        conn.close()


def test_block_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "need clarification"})
    d = json.loads(out)
    assert d["ok"] is True
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "blocked"
    finally:
        conn.close()


def test_block_rejects_empty_reason(worker_env):
    from tools import kanban_tools as kt
    for bad in ["", "   ", None]:
        out = kt._handle_block({"reason": bad})
        assert json.loads(out).get("error")


def test_heartbeat_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({"note": "progress"})
    d = json.loads(out)
    assert d["ok"] is True


def test_heartbeat_without_note(worker_env):
    """note is optional."""
    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({})
    d = json.loads(out)
    assert d["ok"] is True


def test_heartbeat_extends_claim_expires(worker_env):
    """The kanban_heartbeat tool MUST extend claim_expires, not just
    update last_heartbeat_at — otherwise long-running workers loop the
    heartbeat tool diligently and still get reclaimed by
    release_stale_claims at DEFAULT_CLAIM_TTL_SECONDS.

    Regression test for the bug where _handle_heartbeat called
    heartbeat_worker but never heartbeat_claim, so claim_expires sat
    static while last_heartbeat_at advanced.
    """
    import time as _time
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Rewind claim_expires into the past so any forward movement is
    # unambiguous (avoids time.sleep flakiness).
    conn = kb.connect()
    try:
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (1, worker_env),
        )
        conn.commit()
        before = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (worker_env,)
        ).fetchone()["claim_expires"]
    finally:
        conn.close()
    assert before == 1

    out = kt._handle_heartbeat({"note": "still alive"})
    assert json.loads(out).get("ok") is True

    conn = kb.connect()
    try:
        after = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (worker_env,)
        ).fetchone()["claim_expires"]
    finally:
        conn.close()

    now = int(_time.time())
    # claim_expires should be roughly now + DEFAULT_CLAIM_TTL_SECONDS.
    # We assert a generous floor (now + half the default TTL) to keep the
    # test stable against future TTL changes.
    assert after > before, (
        f"claim_expires did not advance ({before} -> {after}); workers "
        f"would be reclaimed at TTL despite heartbeating"
    )
    assert after >= now + (kb.DEFAULT_CLAIM_TTL_SECONDS // 2), (
        f"claim_expires={after} is suspiciously close to now={now}; "
        f"expected at least now + {kb.DEFAULT_CLAIM_TTL_SECONDS // 2}"
    )


def test_comment_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": worker_env,
        "body": "hello thread",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["comment_id"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
        assert len(comments) == 1
        # Author defaults to HERMES_PROFILE env we set in the fixture
        assert comments[0].author == "test-worker"
        assert comments[0].body == "hello thread"
    finally:
        conn.close()


def test_comment_rejects_empty_body(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_comment({"task_id": worker_env, "body": "   "})
    assert json.loads(out).get("error")


def test_comment_ignores_caller_supplied_author(worker_env):
    """``args["author"]`` is no longer honored — the author is always
    derived from ``HERMES_PROFILE`` so a worker can't forge a comment
    under an authoritative-looking name like ``hermes-system`` and
    poison the next worker's prompt context. Cross-task commenting
    itself remains unrestricted (see #19713); only the author override
    is removed.
    """
    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": worker_env, "body": "hi", "author": "hermes-system",
    })
    assert json.loads(out)["ok"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
        # Author comes from HERMES_PROFILE in the fixture, not the
        # caller-supplied "hermes-system" override.
        assert comments[0].author == "test-worker"
    finally:
        conn.close()


def test_comment_schema_omits_author_override():
    """The ``author`` property must not appear on KANBAN_COMMENT_SCHEMA;
    exposing it to the LLM would re-introduce the forgery surface this
    handler is hardened against.
    """
    from tools.kanban_tools import KANBAN_COMMENT_SCHEMA
    props = KANBAN_COMMENT_SCHEMA["parameters"]["properties"]
    assert "author" not in props


def test_create_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "child task",
        "assignee": "peer",
        "parents": [worker_env],
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["task_id"]
    assert d["status"] == "todo"  # parent isn't done yet
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child.title == "child task"
        assert child.assignee == "peer"
    finally:
        conn.close()


def test_create_rejects_no_title(worker_env):
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_create({"assignee": "x"})).get("error")
    assert json.loads(kt._handle_create({"title": "   ", "assignee": "x"})).get("error")


def test_create_rejects_no_assignee(worker_env):
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_create({"title": "t"})).get("error")


def test_create_rejects_non_list_parents(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_create({"title": "t", "assignee": "a", "parents": 42})
    assert json.loads(out).get("error")


def test_create_parses_triage_string_false(worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "not triage",
        "assignee": "peer",
        "triage": "false",
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        task = kb.get_task(conn, d["task_id"])
        assert task.status == "ready"
    finally:
        conn.close()


def test_create_parses_triage_string_true(worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "needs triage",
        "assignee": "peer",
        "triage": "true",
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        task = kb.get_task(conn, d["task_id"])
        assert task.status == "triage"
    finally:
        conn.close()


def test_create_rejects_bad_triage(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "bad triage",
        "assignee": "peer",
        "triage": "sometimes",
    })
    assert "triage must be" in json.loads(out).get("error", "")


def test_create_accepts_string_parent(worker_env):
    """Convenience: a single parent id as string is coerced to [id]."""
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "t", "assignee": "a", "parents": worker_env,
    })
    assert json.loads(out)["ok"]


def test_create_accepts_skills_list(worker_env):
    """Tool writes the per-task skills through to the kernel."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "skilled",
        "assignee": "linguist",
        "skills": ["translation", "github-code-review"],
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, d["task_id"])
    assert task.skills == ["translation", "github-code-review"]


def test_create_accepts_skills_string(worker_env):
    """Convenience: a single skill name as string is coerced to [name]."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "one-skill",
        "assignee": "a",
        "skills": "translation",
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, d["task_id"])
    assert task.skills == ["translation"]


def test_create_rejects_non_list_skills(worker_env):
    """skills: 42 must be rejected, not silently dropped."""
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "t", "assignee": "a", "skills": 42,
    })
    assert json.loads(out).get("error")


def test_link_happy_path(worker_env):
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="A", assignee="x")
        b = kb.create_task(conn, title="B", assignee="x")
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = kt._handle_link({"parent_id": a, "child_id": b})
    d = json.loads(out)
    assert d["ok"] is True


def test_link_rejects_self_reference(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_link({"parent_id": worker_env, "child_id": worker_env})
    assert json.loads(out).get("error")


def test_link_rejects_missing_args(worker_env):
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_link({"parent_id": "x"})).get("error")
    assert json.loads(kt._handle_link({"child_id": "y"})).get("error")


def test_link_rejects_cycle(worker_env):
    """A → B, then try to link B → A."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="A", assignee="x")
        b = kb.create_task(conn, title="B", assignee="x", parents=[a])
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = kt._handle_link({"parent_id": b, "child_id": a})
    assert json.loads(out).get("error")


def test_unblock_happy_path(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="blocked", assignee="worker")
        kb.block_task(conn, tid, reason="waiting")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": tid})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["status"] == "ready"

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()


def test_unblock_rejects_non_blocked_task(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": worker_env})
    assert json.loads(out).get("error")


def test_worker_lifecycle_through_tools(worker_env):
    """Drive the full claim -> heartbeat -> comment -> complete lifecycle
    exclusively through the tools, then verify the DB state matches what
    the dispatcher/notifier expect."""
    from tools import kanban_tools as kt

    # 1. show — worker orientation
    show = json.loads(kt._handle_show({}))
    assert show["task"]["id"] == worker_env

    # 2. heartbeat during long op
    assert json.loads(kt._handle_heartbeat({"note": "warming up"}))["ok"]

    # 3. comment for a future peer
    assert json.loads(kt._handle_comment({
        "task_id": worker_env,
        "body": "note: using stdlib sqlite3 bindings",
    }))["ok"]

    # 4. spawn a child task for follow-up
    child_out = json.loads(kt._handle_create({
        "title": "write integration test",
        "assignee": "qa",
        "parents": [worker_env],
    }))
    assert child_out["ok"]

    # 5. complete with structured handoff
    comp = json.loads(kt._handle_complete({
        "summary": "implemented + spawned QA follow-up",
        "metadata": {"child_task": child_out["task_id"]},
    }))
    assert comp["ok"]

    # Verify final state
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        parent = kb.get_task(conn, worker_env)
        assert parent.status == "done"
        assert parent.current_run_id is None
        run = kb.latest_run(conn, worker_env)
        assert run.outcome == "completed"
        assert run.metadata == {"child_task": child_out["task_id"]}
        # Child is todo (parent just finished, but recompute_ready may
        # have promoted it — complete_task runs recompute internally).
        child = kb.get_task(conn, child_out["task_id"])
        assert child.status == "ready", (
            f"child should be ready after parent done, got {child.status}"
        )
        # Comment is visible
        assert len(kb.list_comments(conn, worker_env)) == 1
        # Heartbeat event recorded
        hb = [e for e in kb.list_events(conn, worker_env) if e.kind == "heartbeat"]
        assert len(hb) == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# System-prompt guidance injection
# ---------------------------------------------------------------------------

def test_kanban_guidance_not_in_normal_prompt(monkeypatch, tmp_path):
    """A normal chat session (no HERMES_KANBAN_TASK) must NOT have
    KANBAN_GUIDANCE in its system prompt."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from run_agent import AIAgent
    a = AIAgent(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    prompt = a._build_system_prompt()
    assert "You are a Kanban worker" not in prompt
    assert "kanban_show()" not in prompt


def test_kanban_guidance_in_worker_prompt(monkeypatch, tmp_path):
    """A worker session (HERMES_KANBAN_TASK set) MUST have the full
    lifecycle guidance in its system prompt."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from run_agent import AIAgent
    a = AIAgent(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    prompt = a._build_system_prompt()
    # Header phrase (identity-free — SOUL.md owns identity, layer 3 is protocol)
    assert "Kanban task execution protocol" in prompt
    # Lifecycle signals
    assert "kanban_show()" in prompt
    assert "kanban_complete" in prompt
    assert "kanban_block" in prompt
    assert "kanban_create" in prompt
    # Anti-shell guidance
    assert "Do not shell out" in prompt or "tools — they work" in prompt


def test_kanban_guidance_prompt_size_bounded(monkeypatch, tmp_path):
    """Sanity: the guidance block is under 4 KB so it doesn't blow
    up the cached prompt."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from agent.prompt_builder import KANBAN_GUIDANCE
    assert 1_500 < len(KANBAN_GUIDANCE) < 4_096, (
        f"KANBAN_GUIDANCE is {len(KANBAN_GUIDANCE)} chars — too short (missing?) or too long"
    )


# ---------------------------------------------------------------------------
# Worker task-ownership enforcement (regression tests for #19534)
# ---------------------------------------------------------------------------
#
# A worker process has HERMES_KANBAN_TASK set to its own task id. The
# destructive tools (kanban_complete, kanban_block, kanban_heartbeat,
# kanban_unblock) must refuse to operate
# on any OTHER task id, even if the caller supplies an explicit `task_id`
# argument. Workers legitimately call kanban_show / kanban_list /
# kanban_comment / kanban_create / kanban_link on other tasks, so those
# are unrestricted.
#
# Orchestrator profiles (no HERMES_KANBAN_TASK in env) are intentionally
# exempt — their job is routing, and they sometimes close out child
# tasks on behalf of the child.


def _make_parent_triage_context(monkeypatch, tmp_path):
    """Create a blocked proof-loop source and scope env to its parent triage."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        source = kb.create_task(
            conn,
            title="proof-loop source",
            assignee="proofloopworker",
            idempotency_key="proof-loop:plan:task:S1:BUILD_FIX",
            skills=[kb.PROOF_LOOP_SKILL],
        )
        assert kb.block_task(conn, source, reason="parent triage required")
        triage = conn.execute(
            "SELECT id FROM tasks WHERE created_by = ?",
            (kb.ON_BLOCK_PARENT_TRIAGE_CREATED_BY,),
        ).fetchone()["id"]
        _append_resolver_v1_event(conn, triage, source)
        sibling = kb.create_task(conn, title="sibling", assignee="peer")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", triage)
    return source, triage, sibling


def _append_resolver_v1_event(
    conn,
    triage_task_id: str,
    source_task_id: str,
    *,
    source_event_kind: str = "blocked",
) -> None:
    """Attach structured resolver authority metadata to a triage task.

    @precondition triage_task_id and source_task_id are existing task ids.
    @postcondition triage_task_id has a resolver v1 event naming source_task_id and its source event.
    @mutates task_events table only.
    """
    from hermes_cli import kanban_db as kb
    source_event = conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1",
        (source_task_id, source_event_kind),
    ).fetchone()
    assert source_event is not None
    kb._append_event(
        conn,
        triage_task_id,
        "resolver_authority",
        {
            "resolver": "v1",
            "source_task_id": source_task_id,
            "source_event_id": source_event["id"],
            "allowed_actions": ["complete", "unblock"],
            "review_gate_task_id": None,
            "issued_by": kb.ON_BLOCK_PARENT_TRIAGE_CREATED_BY,
        },
    )


def _make_parent_triage_context_without_parseable_idempotency(monkeypatch, tmp_path):
    """Create a parent-triage-shaped card with non-authoritative idempotency.

    pre: test fixture has an isolated HERMES_HOME.
    post: returns blocked proof-loop source + triage card + sibling task ids.
    post: HERMES_KANBAN_TASK points at the triage card.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        source = kb.create_task(
            conn,
            title="proof-loop source",
            assignee="proofloopworker",
            idempotency_key="proof-loop:plan:task:S1:BUILD_FIX",
            skills=[kb.PROOF_LOOP_SKILL],
        )
        assert kb.block_task(conn, source, reason="parent triage required")
        triage = kb.create_task(
            conn,
            title="forged parent triage without parseable source authority",
            assignee="default",
            created_by=kb.ON_BLOCK_PARENT_TRIAGE_CREATED_BY,
            idempotency_key=f"{kb.ON_BLOCK_PARENT_TRIAGE_IDEMPOTENCY_PREFIX}opaque",
            skills=[kb.PROOF_LOOP_SKILL],
        )
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (triage,))
        sibling = kb.create_task(conn, title="sibling", assignee="peer")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", triage)
    return source, triage, sibling


def test_worker_complete_rejects_foreign_task_id(worker_env):
    """A worker cannot complete a task that isn't its own (#19534)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_complete({"task_id": other, "summary": "HIJACK"})
    d = json.loads(out)
    assert d.get("ok") is not True
    assert "refusing to mutate" in d.get("error", "")

    # Sibling task must be untouched.
    conn = kb.connect()
    try:
        assert kb.get_task(conn, other).status == "ready"
    finally:
        conn.close()


def test_worker_block_rejects_foreign_task_id(worker_env):
    """A worker cannot block a task that isn't its own (#19534)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_block({"task_id": other, "reason": "evil"})
    d = json.loads(out)
    assert "refusing to mutate" in d.get("error", "")

    conn = kb.connect()
    try:
        assert kb.get_task(conn, other).status == "ready"
    finally:
        conn.close()


def test_worker_heartbeat_rejects_foreign_task_id(worker_env):
    """A worker cannot heartbeat a task that isn't its own (#19534)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
        # Put sibling in running state so heartbeat would otherwise succeed.
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({"task_id": other})
    d = json.loads(out)
    assert "refusing to mutate" in d.get("error", "")


def test_worker_can_comment_on_foreign_task(worker_env):
    """Cross-task commenting must remain unrestricted (#19713 policy).

    The author-forgery hardening removed args['author'] but deliberately
    did NOT add an ownership gate to kanban_comment — comments are the
    documented handoff channel between tasks. This test pins that policy
    so a future change accidentally adding ``_enforce_worker_task_ownership``
    to ``_handle_comment`` would fail CI immediately.
    """
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": other,
        "body": "handoff: see prior findings before starting",
    })
    d = json.loads(out)
    assert d.get("ok") is True, f"cross-task comment must succeed: {d}"

    # The comment lands on the foreign task, attributed to the worker's
    # HERMES_PROFILE — never to a caller-controlled string.
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, other)
        assert len(comments) == 1
        assert comments[0].author == "test-worker"
        assert comments[0].body.startswith("handoff:")
    finally:
        conn.close()


def test_worker_unblock_rejects_foreign_task_id(worker_env):
    """A worker cannot unblock any task — kanban_unblock is orchestrator-only.

    The check fires before the per-task ownership check, so the error
    surface is the orchestrator-only refusal rather than the
    cross-task-ownership refusal. Either is fine — the property we're
    pinning is "worker cannot mutate foreign task via kanban_unblock".
    """
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="blocked sibling", assignee="peer")
        kb.block_task(conn, other, reason="waiting")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": other})
    d = json.loads(out)
    err = d.get("error", "")
    assert "orchestrator-only" in err or "refusing to mutate" in err, (
        f"expected worker-rejection error, got {err}"
    )

    conn = kb.connect()
    try:
        assert kb.get_task(conn, other).status == "blocked"
    finally:
        conn.close()


def test_parent_triage_worker_can_complete_blocked_source_task(monkeypatch, tmp_path):
    """On-block parent triage may close the exact source card after proof."""
    source, triage, _sibling = _make_parent_triage_context(monkeypatch, tmp_path)

    from tools import kanban_tools as kt
    out = kt._handle_complete({
        "task_id": source,
        "summary": "FALSE_BLOCKER: parent verified locally; release gate.",
        "metadata": {"classification": "FALSE_BLOCKER", "triage_task_id": triage},
    })
    d = json.loads(out)
    assert d.get("ok") is True, d

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        assert kb.get_task(conn, source).status == "done"
        assert kb.get_task(conn, triage).status == "ready"
    finally:
        conn.close()


def test_parent_triage_worker_can_complete_gave_up_source_task(monkeypatch, tmp_path):
    """Resolver authority follows the source's blocked state, not one event kind.

    pre: a core-created parent triage carries resolver metadata for a source event that
         transitioned the source into blocked/gave-up recovery.
    post: the triage worker may complete the exact source card with the same narrow
          authority used for explicit block events.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        source = kb.create_task(
            conn,
            title="proof-loop source gave up",
            assignee="proofloopworker",
            idempotency_key="proof-loop:plan:task:S1:RED_PREP",
            skills=[kb.PROOF_LOOP_SKILL],
        )
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (source,))
        kb._append_event(conn, source, "gave_up", {"error": "protocol violation"})
        triage = kb.create_task(
            conn,
            title=f"[parent triage] blocked {source}",
            assignee="default",
            created_by=kb.ON_BLOCK_PARENT_TRIAGE_CREATED_BY,
            idempotency_key=f"{kb.ON_BLOCK_PARENT_TRIAGE_IDEMPOTENCY_PREFIX}source:{source}:event:gave-up",
            skills=[kb.PROOF_LOOP_SKILL],
        )
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (triage,))
        _append_resolver_v1_event(conn, triage, source, source_event_kind="gave_up")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", triage)

    from tools import kanban_tools as kt
    out = json.loads(kt._handle_complete({
        "task_id": source,
        "summary": "FALSE_BLOCKER: RED_PREP evidence recovered after gave_up.",
    }))
    assert out.get("ok") is True, out

    conn = kb.connect()
    try:
        assert kb.get_task(conn, source).status == "done"
    finally:
        conn.close()


def test_forged_parent_triage_idempotency_cannot_mutate_source(monkeypatch, tmp_path):
    """Forged triage shape without resolver metadata must fail closed."""
    source, _triage, _sibling = _make_parent_triage_context_without_parseable_idempotency(monkeypatch, tmp_path)

    from tools import kanban_tools as kt
    complete_out = json.loads(kt._handle_complete({
        "task_id": source,
        "summary": "forged triage should not mutate source",
    }))
    unblock_out = json.loads(kt._handle_unblock({"task_id": source}))
    assert "refusing to mutate" in complete_out.get("error", "")
    assert "refusing to mutate" in unblock_out.get("error", "")


def test_parent_triage_requires_structured_resolver_metadata(monkeypatch, tmp_path):
    """Cross-source mutation should require structured resolver metadata."""
    source, triage, _sibling = _make_parent_triage_context_without_parseable_idempotency(monkeypatch, tmp_path)

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        _append_resolver_v1_event(conn, triage, source)
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_complete({
        "task_id": source,
        "summary": "resolver metadata permits exact source mutation",
        "metadata": {"classification": "FALSE_BLOCKER", "triage_task_id": triage},
    })
    d = json.loads(out)
    assert d.get("ok") is True, d


def test_parent_triage_worker_can_unblock_blocked_source_task(monkeypatch, tmp_path):
    """On-block parent triage may reopen the exact source card with instructions."""
    source, _triage, _sibling = _make_parent_triage_context(monkeypatch, tmp_path)

    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": source})
    d = json.loads(out)
    assert d.get("ok") is True, d
    assert d.get("status") == "ready"

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        assert kb.get_task(conn, source).status == "ready"
    finally:
        conn.close()


def test_parent_triage_worker_cannot_mutate_sibling_task(monkeypatch, tmp_path):
    """The parent-triage exception is source-card exact, not board-wide."""
    _source, _triage, sibling = _make_parent_triage_context(monkeypatch, tmp_path)

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        kb.block_task(conn, sibling, reason="ordinary sibling blocker")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    complete_out = json.loads(kt._handle_complete({
        "task_id": sibling,
        "summary": "should not be allowed",
    }))
    unblock_out = json.loads(kt._handle_unblock({"task_id": sibling}))
    assert "refusing to mutate" in complete_out.get("error", "")
    assert "refusing to mutate" in unblock_out.get("error", "")

    conn = kb.connect()
    try:
        assert kb.get_task(conn, sibling).status == "blocked"
    finally:
        conn.close()


def test_resolver_authorized_worker_can_create_followup_with_inherited_source_authority(monkeypatch, tmp_path):
    """A resolver-scoped triage task can spawn a follow-up with inherited authority.

    pre: env is scoped to a real on-block parent-triage task with structured metadata
    post: create succeeds and returns a new task id for the follow-up card
    post: follow-up receives structured resolver authority payload derived from parent
    """
    source, triage, _sibling = _make_parent_triage_context(monkeypatch, tmp_path)

    from tools import kanban_tools as kt
    created = json.loads(kt._handle_create({
        "title": "follow-up repair/rereview",
        "assignee": "proofloopworker",
        "parents": [triage],
        "inherit_parent_resolver_authority": {
            "source_task_id": source,
            "allowed_actions": ["complete", "unblock"],
        },
    }))
    assert created.get("ok") is True, created

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        followup_id = created["task_id"]
        resolver = kb.read_on_block_parent_triage_resolver_metadata(conn, followup_id)
        assert isinstance(resolver, dict), resolver
        assert resolver.get("source_task_id") == source
        assert set(resolver.get("allowed_actions", [])) == {"complete", "unblock"}
    finally:
        conn.close()


def test_inherited_resolver_followup_can_complete_source_gate(monkeypatch, tmp_path):
    """An inherited follow-up card can complete the exact blocked source gate."""
    source, triage, _sibling = _make_parent_triage_context(monkeypatch, tmp_path)

    from tools import kanban_tools as kt
    created = json.loads(kt._handle_create({
        "title": "follow-up repair",
        "assignee": "proofloopworker",
        "parents": [triage],
        "inherit_parent_resolver_authority": {
            "source_task_id": source,
            "allowed_actions": ["complete", "unblock"],
        },
    }))
    assert created.get("ok") is True, created

    monkeypatch.setenv("HERMES_KANBAN_TASK", created["task_id"])
    out = json.loads(kt._handle_complete({
        "task_id": source,
        "summary": "accepted rereview; release source gate",
    }))
    assert out.get("ok") is True, out


def test_ready_with_concrete_continuation_auto_completes_source_and_dispatches(monkeypatch, tmp_path):
    """Completing a resolver repair card with a concrete continuation releases source.

    pre: repair card carries inherited resolver authority for a blocked source gate.
    post: completing repair with READY_WITH_CONCRETE_CONTINUATION completes the source gate.
    post: the continuation becomes ready and the dispatcher is invoked immediately.
    """
    source, triage, _sibling = _make_parent_triage_context(monkeypatch, tmp_path)

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    created = json.loads(kt._handle_create({
        "title": "hook repair",
        "assignee": "proofloopworker",
        "inherit_parent_resolver_authority": {
            "source_task_id": source,
            "allowed_actions": ["complete"],
        },
    }))
    assert created.get("ok") is True, created
    repair = created["task_id"]

    conn = kb.connect()
    try:
        continuation = kb.create_task(
            conn,
            title="parent triage continuation",
            assignee="default",
            parents=[source],
        )
        assert kb.complete_task(conn, triage, result="triage handed off to repair")
        assert kb.claim_task(conn, repair) is not None
        assert kb.get_task(conn, continuation).status == "todo"
    finally:
        conn.close()

    dispatch_calls = []

    def fake_dispatch_once(conn, **kwargs):
        dispatch_calls.append(dict(kwargs))
        return kb.DispatchResult(spawned=[(continuation, "default", "/tmp/continuation")])

    monkeypatch.setattr(kb, "dispatch_once", fake_dispatch_once)
    monkeypatch.setenv("HERMES_KANBAN_TASK", repair)

    out = json.loads(kt._handle_complete({
        "summary": "hook repair finished; continuation is concrete",
        "result": "READY_WITH_CONCRETE_CONTINUATION",
        "metadata": {"continuation_task_id": continuation},
    }))
    assert out.get("ok") is True, out
    assert out.get("source_gate_completed") is True, out
    assert out.get("continuation_task_ids") == [continuation]
    assert dispatch_calls, "source-gate release must dispatch immediately"

    conn = kb.connect()
    try:
        assert kb.get_task(conn, repair).status == "done"
        assert kb.get_task(conn, source).status == "done"
        assert kb.get_task(conn, continuation).status == "ready"
    finally:
        conn.close()


def test_ready_with_concrete_continuation_requires_concrete_source_gated_target(monkeypatch, tmp_path):
    """READY_WITH_CONCRETE_CONTINUATION fails closed without a source child target.

    post: ambiguous READY completion does not complete the repair card or source gate.
    post: an ungated sibling continuation is also rejected before mutation.
    """
    source, triage, _sibling = _make_parent_triage_context(monkeypatch, tmp_path)

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    created = json.loads(kt._handle_create({
        "title": "hook repair",
        "assignee": "proofloopworker",
        "inherit_parent_resolver_authority": {
            "source_task_id": source,
            "allowed_actions": ["complete"],
        },
    }))
    assert created.get("ok") is True, created
    repair = created["task_id"]

    conn = kb.connect()
    try:
        assert kb.complete_task(conn, triage, result="triage handed off to repair")
        assert kb.claim_task(conn, repair) is not None
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", repair)
    ambiguous = json.loads(kt._handle_complete({
        "summary": "ready but no concrete target",
        "result": "READY_WITH_CONCRETE_CONTINUATION",
    }))
    assert ambiguous.get("ok") is not True
    assert "requires metadata.continuation_task_id" in ambiguous.get("error", "")

    conn = kb.connect()
    try:
        ungated = kb.create_task(conn, title="not source gated", assignee="default")
    finally:
        conn.close()

    ungated_attempt = json.loads(kt._handle_complete({
        "summary": "ready with wrong target",
        "result": "READY_WITH_CONCRETE_CONTINUATION",
        "metadata": {"continuation_task_id": ungated},
    }))
    assert ungated_attempt.get("ok") is not True
    assert "is not gated by source task" in ungated_attempt.get("error", "")

    conn = kb.connect()
    try:
        assert kb.get_task(conn, repair).status == "running"
        assert kb.get_task(conn, source).status == "blocked"
    finally:
        conn.close()


def test_inherited_resolver_followup_cannot_mutate_sibling_or_forge_source(monkeypatch, tmp_path):
    """Inherited authority stays exact-source and cannot be forged/widened."""
    source, triage, sibling = _make_parent_triage_context(monkeypatch, tmp_path)

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        kb.block_task(conn, sibling, reason="sibling blocker")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    created = json.loads(kt._handle_create({
        "title": "follow-up repair",
        "assignee": "proofloopworker",
        "parents": [triage],
        "inherit_parent_resolver_authority": {
            "source_task_id": source,
            "allowed_actions": ["complete", "unblock"],
        },
    }))
    assert created.get("ok") is True, created

    monkeypatch.setenv("HERMES_KANBAN_TASK", created["task_id"])
    sibling_attempt = json.loads(kt._handle_complete({
        "task_id": sibling,
        "summary": "forged sibling mutation",
    }))
    assert "refusing to mutate" in sibling_attempt.get("error", "")


def test_worker_complete_own_task_still_works(worker_env):
    """The ownership check doesn't break the normal own-task happy path."""
    from tools import kanban_tools as kt
    # Both implicit (no task_id arg) and explicit (matching env) must work.
    out = kt._handle_complete({"task_id": worker_env, "summary": "explicit own"})
    d = json.loads(out)
    assert d.get("ok") is True and d.get("task_id") == worker_env


def test_worker_complete_rejects_stale_run_id(worker_env, monkeypatch):
    """A retried worker cannot complete the task using an old run token."""
    from hermes_cli import kanban_db as kb
    import hermes_cli.kanban_db as _kb

    conn = kb.connect()
    try:
        run1 = kb.latest_run(conn, worker_env)
        kb._set_worker_pid(conn, worker_env, 98765)
        monkeypatch.setattr(_kb, "_pid_alive", lambda pid: False)
        assert kb.detect_crashed_workers(conn) == [worker_env]

        kb.claim_task(conn, worker_env)
        run2 = kb.latest_run(conn, worker_env)
        assert run2.id != run1.id
    finally:
        conn.close()

    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run1.id))
    out = kt._handle_complete({"summary": "late stale completion"})
    d = json.loads(out)
    assert d.get("ok") is not True

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        assert task.status == "running"
        assert task.current_run_id == run2.id
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run2.id))
    out = kt._handle_complete({"summary": "current completion"})
    d = json.loads(out)
    assert d.get("ok") is True


def test_orchestrator_complete_any_task_allowed(monkeypatch, tmp_path):
    """Orchestrator profiles (no HERMES_KANBAN_TASK) can still complete
    any task via explicit task_id. The check only applies to workers."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="child to close out")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_complete({"task_id": tid, "summary": "orchestrator close"})
    d = json.loads(out)
    assert d.get("ok") is True and d.get("task_id") == tid
