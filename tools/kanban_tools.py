"""Kanban tools — structured tool-call surface for worker + orchestrator agents.

These tools are only registered into the model's schema when the agent is
running under the dispatcher (env var ``HERMES_KANBAN_TASK`` set). A
normal ``hermes chat`` session sees **zero** kanban tools in its schema.

Why tools instead of just shelling out to ``hermes kanban``?

1. **Backend portability.** A worker whose terminal tool points at Docker
   / Modal / Singularity / SSH would run ``hermes kanban complete …``
   inside the container, where ``hermes`` isn't installed and the DB
   isn't mounted. Tools run in the agent's Python process, so they
   always reach ``~/.hermes/kanban.db`` regardless of terminal backend.

2. **No shell-quoting footguns.** Passing ``--metadata '{"x": [...]}'``
   through shlex+argparse is fragile. Structured tool args skip it.

3. **Better errors.** Tool-call failures return structured JSON the
   model can reason about, not stderr strings it has to parse.

Humans continue to use the CLI (``hermes kanban …``), the dashboard
(``hermes dashboard``), and the slash command (``/kanban …``) — all
three bypass the agent entirely. The tools are ONLY for the worker
agent's handoff back to the kernel.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

KANBAN_LIST_DEFAULT_LIMIT = 50
KANBAN_LIST_MAX_LIMIT = 200
READY_WITH_CONCRETE_CONTINUATION = "READY_WITH_CONCRETE_CONTINUATION"


def _profile_has_kanban_toolset() -> bool:
    # Uses load_config() which has mtime-based caching, so this adds
    # negligible overhead. The check_fn results are further TTL-cached
    # (~30s) by the tool registry.
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        toolsets = cfg.get("toolsets", [])
        return "kanban" in toolsets
    except Exception:
        return False


def _check_kanban_mode() -> bool:
    """Task-lifecycle tools are available when:

    1. ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), OR
    2. The current profile has ``kanban`` in its toolsets config
       (orchestrator profiles like techlead that route work via Kanban).

    Humans running ``hermes chat`` without the kanban toolset see zero
    kanban tools. Workers spawned by the kanban dispatcher (gateway-
    embedded by default) and orchestrator profiles with the kanban
    toolset enabled see the Kanban lifecycle tool surface.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    return _profile_has_kanban_toolset()


def _check_kanban_orchestrator_mode() -> bool:
    """Board-routing tools (kanban_list, kanban_unblock) are intentionally
    hidden from task workers.

    Dispatcher-spawned workers should close their own task via the
    lifecycle tools (complete/block/heartbeat), not enumerate or unblock
    board state. Profiles that explicitly opt into the kanban toolset
    and are NOT scoped to a single task are the orchestrator surface.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    return _profile_has_kanban_toolset()


def _resolver_source_event_exists(conn, *, source_id: str, source_event_id: int) -> bool:
    """Return whether resolver metadata points at a real event on the source.

    pre: source_id is a task id candidate from structured resolver metadata.
    pre: source_event_id is a positive integer candidate from structured resolver metadata.
    post: returns True only when the event id belongs to source_id.

    The source's current ``blocked`` state is the authorization boundary; the
    source event is provenance. Do not couple this check to one event kind,
    because Kanban core can route several transitions into the same blocked
    recovery state (explicit block, gave_up/protocol violation, timeout, etc.).
    """
    row = conn.execute(
        "SELECT id FROM task_events WHERE id = ? AND task_id = ?",
        (source_event_id, source_id),
    ).fetchone()
    return row is not None


def _worker_parent_triage_source_task_id(
    kb,
    conn,
    triage_task_id: str,
    *,
    action: Optional[str] = None,
) -> Optional[str]:
    """Return source task id only when structured resolver metadata authorizes it.

    pre: triage_task_id is the current HERMES_KANBAN_TASK value
    pre: action is None for generic gating checks, or one of complete/unblock
    post: returns source task id only for kernel-emitted structured metadata
    post: returns None for ordinary workers, forged cards, malformed payloads,
          non-proof-loop sources, sources not currently blocked, or disallowed actions
    """
    read_meta = getattr(kb, "read_on_block_parent_triage_resolver_metadata", None)
    if not callable(read_meta):
        return None
    resolver = read_meta(conn, triage_task_id)
    if not isinstance(resolver, dict):
        return None

    source_id = resolver.get("source_task_id")
    if not isinstance(source_id, str) or not source_id.startswith("t_"):
        return None

    allowed_actions = resolver.get("allowed_actions")
    if not isinstance(allowed_actions, list) or not all(isinstance(x, str) for x in allowed_actions):
        return None
    allowed = {x.strip() for x in allowed_actions if x and x.strip()}
    if action in {"complete", "unblock"} and action not in allowed:
        return None

    source = kb.get_task(conn, source_id)
    if source is None or source.status != "blocked":
        return None
    is_phase = getattr(kb, "_is_proof_loop_phase_task", lambda _task: False)
    if not is_phase(source):
        return None

    source_event_id = resolver.get("source_event_id")
    if not isinstance(source_event_id, int):
        return None
    if not _resolver_source_event_exists(
        conn,
        source_id=source_id,
        source_event_id=source_event_id,
    ):
        return None
    return source_id


def _check_kanban_unblock_mode() -> bool:
    """Expose unblock only to orchestrators or kernel-created parent triage.

    Ordinary dispatcher-spawned workers must not even see board-moving tools.
    On-block parent-triage workers are the narrow exception: their whole job is
    to inspect one blocked source card and move it after local verification.
    """
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if not env_tid:
        return _profile_has_kanban_toolset()
    try:
        kb, conn = _connect()
        try:
            return _worker_parent_triage_source_task_id(
                kb,
                conn,
                env_tid,
                action="unblock",
            ) is not None
        finally:
            conn.close()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _default_task_id(arg: Optional[str]) -> Optional[str]:
    """Resolve ``task_id`` arg or fall back to the env var the dispatcher set."""
    if arg:
        return arg
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    return env_tid or None


def _worker_run_id(task_id: str) -> Optional[int]:
    """Return this worker's dispatcher run id when it is scoped to task_id."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _enforce_worker_task_ownership(
    tid: str,
    *,
    action: Optional[str] = None,
    kb=None,
    conn=None,
) -> Optional[str]:
    """Reject worker-driven destructive calls on unauthorized task IDs.

    A process spawned by the dispatcher has ``HERMES_KANBAN_TASK`` set
    to its own task id. Lifecycle tools mutate run state, so ordinary
    workers may only mutate that one task. Kernel-created on-block parent
    triage cards are a narrow exception: they may complete or unblock only the
    exact blocked source card named by Kanban-core structured resolver metadata
    after local verification. Sibling or forged task ids still fail closed.

    Orchestrator profiles (kanban toolset enabled but **no**
    ``HERMES_KANBAN_TASK`` in env) aren't subject to this check — their
    job is routing, and they sometimes legitimately close out child
    tasks or reopen blocked ones.

    Returns ``None`` when the call is allowed, or a tool-error string
    when it must be rejected. Callers should ``return`` the error
    verbatim.
    """
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if not env_tid:
        # Orchestrator or CLI context — no task-scope restriction.
        return None
    if tid != env_tid:
        if action in {"complete", "unblock"} and kb is not None and conn is not None:
            source_id = _worker_parent_triage_source_task_id(
                kb,
                conn,
                env_tid,
                action=action,
            )
            if source_id == tid:
                return None
        return tool_error(
            f"worker is scoped to task {env_tid}; refusing to mutate "
            f"{tid}. Use kanban_comment to hand off information to other "
            f"tasks, or kanban_create to spawn follow-up work."
        )
    return None


def _connect():
    """Import + connect lazily so the module imports cleanly in non-kanban
    contexts (e.g. test rigs that import every tool module)."""
    from hermes_cli import kanban_db as kb
    return kb, kb.connect()


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


def _normalize_profile(value: Any) -> Optional[str]:
    """Normalize CLI-compatible assignee sentinels for the tool surface."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "-", "null"}:
        return None
    return text


def _parse_bool_arg(args: dict, name: str, *, default: bool = False):
    value = args.get(name)
    if value is None:
        return default, None
    if isinstance(value, bool):
        return value, None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True, None
    if text in {"false", "0", "no"}:
        return False, None
    return default, f"{name} must be a boolean or 'true'/'false'"


def _require_orchestrator_tool(tool_name: str) -> Optional[str]:
    """Belt-and-suspenders runtime guard for orchestrator-only handlers.

    The check_fn (`_check_kanban_orchestrator_mode`) keeps these tools
    out of the worker schema entirely, but in case a stale registration
    or test harness routes a worker to one of them anyway, return a
    structured tool_error so the model gets a clear refusal instead of
    silently mutating board state from a worker context.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return tool_error(
            f"{tool_name} is orchestrator-only; dispatcher-spawned workers "
            "must use kanban_complete, kanban_block, kanban_heartbeat, or "
            "kanban_comment for their assigned task."
        )
    return None


def _task_summary_dict(kb, conn, task) -> dict[str, Any]:
    """Compact task shape for board-listing tools."""
    parents = kb.parent_ids(conn, task.id)
    children = kb.child_ids(conn, task.id)
    return {
        "id": task.id,
        "title": task.title,
        "assignee": task.assignee,
        "status": task.status,
        "priority": task.priority,
        "tenant": task.tenant,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "current_run_id": task.current_run_id,
        "parents": parents,
        "children": children,
        "parent_count": len(parents),
        "child_count": len(children),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_show(args: dict, **kw) -> str:
    """Read a task's full state: task row, parents, children, comments,
    runs (attempt history), and the last N events."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    try:
        kb, conn = _connect()
        try:
            task = kb.get_task(conn, tid)
            if task is None:
                return tool_error(f"task {tid} not found")
            comments = kb.list_comments(conn, tid)
            events = kb.list_events(conn, tid)
            runs = kb.list_runs(conn, tid)
            parents = kb.parent_ids(conn, tid)
            children = kb.child_ids(conn, tid)

            def _task_dict(t):
                return {
                    "id": t.id, "title": t.title, "body": t.body,
                    "assignee": t.assignee, "status": t.status,
                    "tenant": t.tenant, "priority": t.priority,
                    "workspace_kind": t.workspace_kind,
                    "workspace_path": t.workspace_path,
                    "created_by": t.created_by, "created_at": t.created_at,
                    "started_at": t.started_at,
                    "completed_at": t.completed_at,
                    "result": t.result,
                    "current_run_id": t.current_run_id,
                }

            def _run_dict(r):
                return {
                    "id": r.id, "profile": r.profile,
                    "status": r.status, "outcome": r.outcome,
                    "summary": r.summary, "error": r.error,
                    "metadata": r.metadata,
                    "started_at": r.started_at, "ended_at": r.ended_at,
                }

            return json.dumps({
                "task": _task_dict(task),
                "parents": parents,
                "children": children,
                "comments": [
                    {"author": c.author, "body": c.body,
                     "created_at": c.created_at}
                    for c in comments
                ],
                "events": [
                    {"kind": e.kind, "payload": e.payload,
                     "created_at": e.created_at, "run_id": e.run_id}
                    for e in events[-50:]   # cap; full log via CLI
                ],
                "runs": [_run_dict(r) for r in runs],
                # Also surface the worker's own context block so the
                # agent can include it directly if it wants. This is
                # the same string build_worker_context returns to the
                # dispatcher at spawn time.
                "worker_context": kb.build_worker_context(conn, tid),
            })
        finally:
            conn.close()
    except Exception as e:
        logger.exception("kanban_show failed")
        return tool_error(f"kanban_show: {e}")


def _handle_list(args: dict, **kw) -> str:
    """List task summaries with the same core filters as the CLI."""
    guard = _require_orchestrator_tool("kanban_list")
    if guard:
        return guard
    assignee = args.get("assignee")
    status = args.get("status")
    tenant = args.get("tenant")
    include_archived, bool_error = _parse_bool_arg(args, "include_archived")
    if bool_error:
        return tool_error(bool_error)
    limit = args.get("limit")
    if limit is None:
        limit = KANBAN_LIST_DEFAULT_LIMIT
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return tool_error("limit must be an integer")
    if limit < 1:
        return tool_error("limit must be >= 1")
    if limit > KANBAN_LIST_MAX_LIMIT:
        return tool_error(f"limit must be <= {KANBAN_LIST_MAX_LIMIT}")
    try:
        kb, conn = _connect()
        try:
            # Match CLI list: dependencies that cleared since the last
            # dispatcher tick should be visible to orchestrators immediately.
            promoted = kb.recompute_ready(conn)
            # Fetch one extra row so model-facing output can report that
            # a bounded listing was truncated without dumping the board.
            rows = kb.list_tasks(
                conn,
                assignee=assignee,
                status=status,
                tenant=tenant,
                include_archived=include_archived,
                limit=limit + 1,
            )
            truncated = len(rows) > limit
            tasks = rows[:limit]
            return json.dumps({
                "tasks": [_task_summary_dict(kb, conn, t) for t in tasks],
                "count": len(tasks),
                "limit": limit,
                "truncated": truncated,
                "next_limit": (
                    min(limit * 2, KANBAN_LIST_MAX_LIMIT)
                    if truncated and limit < KANBAN_LIST_MAX_LIMIT else None
                ),
                "promoted": promoted,
            })
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_list: {e}")
    except Exception as e:
        logger.exception("kanban_list failed")
        return tool_error(f"kanban_list: {e}")


def _handle_complete(args: dict, **kw) -> str:
    """Mark the current task done with a structured handoff."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    summary = args.get("summary")
    metadata = args.get("metadata")
    result = args.get("result")
    created_cards = args.get("created_cards")
    if created_cards is not None:
        if isinstance(created_cards, str):
            # Accept a single id as a string for convenience.
            created_cards = [created_cards]
        if not isinstance(created_cards, (list, tuple)):
            return tool_error(
                f"created_cards must be a list of task ids, got "
                f"{type(created_cards).__name__}"
            )
        # Normalise: strings only, stripped, non-empty.
        created_cards = [
            str(c).strip() for c in created_cards if str(c).strip()
        ]
    if not (summary or result):
        return tool_error(
            "provide at least one of: summary (preferred), result"
        )
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    try:
        kb, conn = _connect()
        try:
            ownership_err = _enforce_worker_task_ownership(
                tid,
                action="complete",
                kb=kb,
                conn=conn,
            )
            if ownership_err:
                return ownership_err
            release_context: Optional[dict[str, Any]] = None
            release_err: Optional[str] = None
            if os.environ.get("HERMES_KANBAN_TASK") == tid:
                release_context, release_err = _prepare_source_gate_release(
                    kb,
                    conn,
                    repair_task_id=tid,
                    result=result,
                    metadata=metadata,
                    created_cards=created_cards,
                )
                if release_err:
                    return release_err
            try:
                ok = kb.complete_task(
                    conn, tid,
                    result=result, summary=summary, metadata=metadata,
                    created_cards=created_cards,
                    expected_run_id=_worker_run_id(tid),
                )
            except kb.HallucinatedCardsError as hall_err:
                # Structured rejection — surface the phantom ids so the
                # worker can retry with a corrected list or drop the
                # field. Audit event already landed in the DB.
                #
                # The task itself was NOT mutated (the gate runs before
                # the write txn), so the worker can simply call
                # kanban_complete again. Spell that out — without it the
                # model often interprets a tool_error as a terminal
                # failure and either blocks or crashes the run instead
                # of retrying. See #22923.
                return tool_error(
                    f"kanban_complete blocked: the following created_cards "
                    f"do not exist or were not created by this worker: "
                    f"{', '.join(hall_err.phantom)}. "
                    f"Your task is still in-flight (no state change). "
                    f"Retry kanban_complete with the same summary/metadata "
                    f"and either drop these ids from created_cards, or pass "
                    f"created_cards=[] to skip the card-claim check entirely."
                )
            if not ok:
                return tool_error(
                    f"could not complete {tid} (unknown id or already terminal)"
                )
            source_gate_payload: dict[str, Any] = {}
            if release_context is not None:
                source_gate_payload, release_err = _complete_source_gate_and_dispatch(
                    kb,
                    conn,
                    repair_task_id=tid,
                    source_task_id=release_context["source_task_id"],
                    continuation_task_ids=release_context["continuation_task_ids"],
                    summary=summary,
                    metadata=metadata,
                )
                if release_err:
                    return release_err
            run = kb.latest_run(conn, tid)
            return _ok(
                task_id=tid,
                run_id=run.id if run else None,
                **source_gate_payload,
            )
        finally:
            conn.close()
    except Exception as e:
        logger.exception("kanban_complete failed")
        return tool_error(f"kanban_complete: {e}")


def _handle_block(args: dict, **kw) -> str:
    """Transition the task to blocked with a reason a human will read."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    reason = args.get("reason")
    if not reason or not str(reason).strip():
        return tool_error("reason is required — explain what input you need")
    try:
        kb, conn = _connect()
        try:
            ok = kb.block_task(
                conn, tid,
                reason=reason,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not block {tid} (unknown id or not in "
                    f"running/ready)"
                )
            run = kb.latest_run(conn, tid)
            return _ok(task_id=tid, run_id=run.id if run else None)
        finally:
            conn.close()
    except Exception as e:
        logger.exception("kanban_block failed")
        return tool_error(f"kanban_block: {e}")


def _handle_heartbeat(args: dict, **kw) -> str:
    """Signal that the worker is still alive during a long operation.

    Extends the claim TTL via ``heartbeat_claim`` AND records a heartbeat
    event via ``heartbeat_worker``. Without the ``heartbeat_claim`` half,
    a diligent worker that loops this tool while a single tool call
    blocks the agent for >DEFAULT_CLAIM_TTL_SECONDS still gets reclaimed
    by ``release_stale_claims`` — which is exactly the trap that
    ``heartbeat_claim``'s docstring warns against.
    """
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    note = args.get("note")
    try:
        kb, conn = _connect()
        try:
            # Extend the claim TTL first. The dispatcher pins
            # HERMES_KANBAN_CLAIM_LOCK in the worker env at spawn time
            # (see _default_spawn in kanban_db.py); falling back to the
            # default _claimer_id() covers locally-driven workers that
            # never went through the dispatcher path.
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            kb.heartbeat_claim(conn, tid, claimer=claim_lock)

            ok = kb.heartbeat_worker(
                conn,
                tid,
                note=note,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not heartbeat {tid} (unknown id or not running)"
                )
            return _ok(task_id=tid)
        finally:
            conn.close()
    except Exception as e:
        logger.exception("kanban_heartbeat failed")
        return tool_error(f"kanban_heartbeat: {e}")


def _handle_comment(args: dict, **kw) -> str:
    """Append a comment to a task's thread."""
    tid = args.get("task_id")
    if not tid:
        return tool_error(
            "task_id is required (use the current task id if that's what "
            "you mean — pulls from env but kept explicit here)"
        )
    body = args.get("body")
    if not body or not str(body).strip():
        return tool_error("body is required")
    # Author is intentionally derived from the worker's own runtime
    # identity, NOT from caller-supplied args. Comments are injected
    # into the next worker's system prompt by ``build_worker_context``
    # as ``**{author}** (timestamp): {body}`` — accepting an
    # ``args["author"]`` override let a worker forge a comment from
    # an authoritative-looking name like ``hermes-system`` and poison
    # the future-worker context with what reads as a system directive.
    # Cross-task commenting itself remains unrestricted (see #19713) —
    # comments are the deliberate handoff channel between tasks.
    author = os.environ.get("HERMES_PROFILE") or "worker"
    try:
        kb, conn = _connect()
        try:
            cid = kb.add_comment(conn, tid, author=author, body=str(body))
            return _ok(task_id=tid, comment_id=cid)
        finally:
            conn.close()
    except Exception as e:
        logger.exception("kanban_comment failed")
        return tool_error(f"kanban_comment: {e}")


def _completion_requests_source_gate_release(
    *,
    result: Any,
    metadata: Any,
) -> bool:
    """Return whether completion requests automatic source-gate release.

    pre: result and metadata are raw kanban_complete arguments
    post: returns True only for the exact READY_WITH_CONCRETE_CONTINUATION token
    """
    if result == READY_WITH_CONCRETE_CONTINUATION:
        return True
    if isinstance(metadata, dict) and metadata.get("result") == READY_WITH_CONCRETE_CONTINUATION:
        return True
    return False


def _extract_concrete_continuation_task_ids(
    *,
    metadata: Any,
    created_cards: Optional[Iterable[str]],
) -> tuple[list[str], Optional[str]]:
    """Extract and normalize source-gate continuation ids from completion data.

    pre: metadata is None or the already type-checked kanban_complete metadata dict
    pre: created_cards is None or a normalized iterable of task-id strings
    post: returns non-empty task ids only when the completion names concrete continuations
    post: returns an error string when the READY_WITH_CONCRETE_CONTINUATION contract is ambiguous
    """
    raw_ids: Any = None
    if isinstance(metadata, dict):
        if metadata.get("continuation_task_id") is not None:
            raw_ids = [metadata.get("continuation_task_id")]
        elif metadata.get("continuation_task_ids") is not None:
            raw_ids = metadata.get("continuation_task_ids")
    if raw_ids is None and created_cards:
        raw_ids = list(created_cards)
    if not isinstance(raw_ids, list):
        return [], tool_error(
            "READY_WITH_CONCRETE_CONTINUATION requires metadata.continuation_task_id "
            "or metadata.continuation_task_ids (or created_cards)"
        )

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_id in raw_ids:
        if not isinstance(raw_id, str):
            return [], tool_error("continuation task ids must be strings")
        tid = raw_id.strip()
        if not tid or not tid.startswith("t_"):
            return [], tool_error("continuation task ids must be Kanban task ids")
        if tid not in seen:
            seen.add(tid)
            normalized.append(tid)
    if not normalized:
        return [], tool_error("READY_WITH_CONCRETE_CONTINUATION requires at least one continuation task id")
    return normalized, None


def _validate_source_gate_continuations(
    kb,
    conn,
    *,
    source_task_id: str,
    continuation_task_ids: list[str],
) -> Optional[str]:
    """Fail closed unless every continuation is a pending child of the source gate.

    pre: source_task_id starts with "t_"
    pre: continuation_task_ids is non-empty and normalized
    post: returns None only when all continuations exist and are source-gated
    """
    for continuation_id in continuation_task_ids:
        task = kb.get_task(conn, continuation_id)
        if task is None:
            return tool_error(f"continuation task {continuation_id} does not exist")
        if task.status not in {"todo", "ready"}:
            return tool_error(
                f"continuation task {continuation_id} must be todo/ready, got {task.status}"
            )
        linked = conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ? LIMIT 1",
            (source_task_id, continuation_id),
        ).fetchone()
        if linked is None:
            return tool_error(
                f"continuation task {continuation_id} is not gated by source task {source_task_id}"
            )
    return None


def _prepare_source_gate_release(
    kb,
    conn,
    *,
    repair_task_id: str,
    result: Any,
    metadata: Any,
    created_cards: Optional[Iterable[str]],
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Validate READY_WITH_CONCRETE_CONTINUATION before any task is completed.

    pre: repair_task_id is the current scoped worker task id
    post: returns release context when the exact READY token is present
    post: returns (None, None) for ordinary completions
    post: returns (None, error_string) for ambiguous or unauthorized READY completions
    """
    if not _completion_requests_source_gate_release(result=result, metadata=metadata):
        return None, None

    source_task_id = _worker_parent_triage_source_task_id(
        kb,
        conn,
        repair_task_id,
        action="complete",
    )
    if source_task_id is None:
        return None, tool_error(
            "READY_WITH_CONCRETE_CONTINUATION requires resolver authority to complete "
            "one blocked source gate"
        )

    continuation_task_ids, err = _extract_concrete_continuation_task_ids(
        metadata=metadata,
        created_cards=created_cards,
    )
    if err:
        return None, err
    err = _validate_source_gate_continuations(
        kb,
        conn,
        source_task_id=source_task_id,
        continuation_task_ids=continuation_task_ids,
    )
    if err:
        return None, err
    return {
        "source_task_id": source_task_id,
        "continuation_task_ids": continuation_task_ids,
    }, None


def _complete_source_gate_and_dispatch(
    kb,
    conn,
    *,
    repair_task_id: str,
    source_task_id: str,
    continuation_task_ids: list[str],
    summary: Optional[str],
    metadata: Optional[dict[str, Any]],
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Complete a resolver source gate and run one dispatcher tick.

    pre: repair_task_id is already completed successfully
    pre: source_task_id is the blocked resolver source gate
    pre: continuation_task_ids name validated source-gated continuation tasks
    post[conn]: source_task_id is done and dependents have been recomputed
    post: dispatch_once has been invoked once after source completion
    """
    source_summary = (
        f"{READY_WITH_CONCRETE_CONTINUATION}: resolver repair {repair_task_id} "
        "provided concrete continuation "
        + ", ".join(continuation_task_ids)
    )
    if summary:
        source_summary = f"{source_summary}. Repair summary: {str(summary).strip()}"
    source_metadata: dict[str, Any] = {
        "classification": READY_WITH_CONCRETE_CONTINUATION,
        "resolved_by_task_id": repair_task_id,
        "continuation_task_ids": continuation_task_ids,
    }
    if metadata:
        source_metadata["repair_metadata"] = metadata

    if not kb.complete_task(
        conn,
        source_task_id,
        result=READY_WITH_CONCRETE_CONTINUATION,
        summary=source_summary,
        metadata=source_metadata,
    ):
        return None, tool_error(f"could not complete source gate {source_task_id}")

    dispatch_result = kb.dispatch_once(conn, max_spawn=1)
    dispatch_payload = {
        "source_gate_completed": True,
        "source_task_id": source_task_id,
        "continuation_task_ids": continuation_task_ids,
        "dispatch_spawned": getattr(dispatch_result, "spawned", []),
        "dispatch_promoted": getattr(dispatch_result, "promoted", 0),
    }
    return dispatch_payload, None


def _build_inherited_resolver_authority(
    kb,
    conn,
    *,
    parent_task_id: str,
    request: Any,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Validate and derive resolver authority for a follow-up child card.

    pre: parent_task_id is the current scoped worker task id
    pre: request is either None or a dict with source_task_id + allowed_actions
    post: returns (metadata, None) when inheritance is explicitly authorized
    post: returns (None, error_string) on malformed, forged, or widened requests
    """
    if request is None:
        return None, None
    if not isinstance(request, dict):
        return None, tool_error("inherit_parent_resolver_authority must be an object")

    read_meta = getattr(kb, "read_on_block_parent_triage_resolver_metadata", None)
    if not callable(read_meta):
        return None, tool_error("inherited resolver authority is unavailable in this runtime")
    parent_meta = read_meta(conn, parent_task_id)
    if not isinstance(parent_meta, dict):
        return None, tool_error(
            "current worker task has no structured resolver authority to inherit"
        )

    source_task_id = request.get("source_task_id")
    if not isinstance(source_task_id, str) or not source_task_id.startswith("t_"):
        return None, tool_error("inherit_parent_resolver_authority.source_task_id must be a task id")
    if source_task_id != parent_meta.get("source_task_id"):
        return None, tool_error("inherited source_task_id must match current resolver authority")

    requested_actions = request.get("allowed_actions")
    if not isinstance(requested_actions, list) or not all(isinstance(x, str) for x in requested_actions):
        return None, tool_error(
            "inherit_parent_resolver_authority.allowed_actions must be a list of strings"
        )
    normalized_requested: list[str] = []
    seen: set[str] = set()
    for action in requested_actions:
        cleaned = action.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized_requested.append(cleaned)
    if not normalized_requested:
        return None, tool_error("inherit_parent_resolver_authority.allowed_actions cannot be empty")

    parent_allowed_raw = parent_meta.get("allowed_actions")
    if not isinstance(parent_allowed_raw, list) or not all(isinstance(x, str) for x in parent_allowed_raw):
        return None, tool_error("current resolver authority has invalid allowed_actions")
    parent_allowed = {x.strip() for x in parent_allowed_raw if x and x.strip()}
    if not set(normalized_requested).issubset(parent_allowed):
        return None, tool_error("inherited allowed_actions must be a subset of current resolver authority")

    source_event_id = parent_meta.get("source_event_id")
    if not isinstance(source_event_id, int):
        return None, tool_error("current resolver authority has invalid source_event_id")

    inherited = {
        "version": parent_meta.get("version") or "v1",
        "source_task_id": source_task_id,
        "source_event_id": source_event_id,
        "allowed_actions": normalized_requested,
        "review_gate_task_id": parent_meta.get("review_gate_task_id"),
        "issued_by": parent_meta.get("issued_by"),
        "inherited_from_task_id": parent_task_id,
    }
    inherited_from_event_id = parent_meta.get("source_event_id")
    if isinstance(inherited_from_event_id, int):
        inherited["inherited_from_event_id"] = inherited_from_event_id
    return inherited, None


def _handle_create(args: dict, **kw) -> str:
    """Create a child task. Orchestrator workers use this to fan out.

    ``parents`` can be a list of task ids; dependency-gated promotion
    works as usual.
    """
    title = args.get("title")
    if not title or not str(title).strip():
        return tool_error("title is required")
    assignee = args.get("assignee")
    if not assignee:
        return tool_error(
            "assignee is required — name the profile that should execute this "
            "task (the dispatcher will only spawn tasks with an assignee)"
        )
    body = args.get("body")
    parents = args.get("parents") or []
    tenant = args.get("tenant") or os.environ.get("HERMES_TENANT")
    priority = args.get("priority")
    workspace_kind = args.get("workspace_kind") or "scratch"
    workspace_path = args.get("workspace_path")
    triage, bool_error = _parse_bool_arg(args, "triage")
    if bool_error:
        return tool_error(bool_error)
    idempotency_key = args.get("idempotency_key")
    max_runtime_seconds = args.get("max_runtime_seconds")
    skills = args.get("skills")
    if isinstance(skills, str):
        # Accept a single skill name as a string for convenience.
        skills = [skills]
    if skills is not None and not isinstance(skills, (list, tuple)):
        return tool_error(
            f"skills must be a list of skill names, got {type(skills).__name__}"
        )
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, (list, tuple)):
        return tool_error(
            f"parents must be a list of task ids, got {type(parents).__name__}"
        )
    inherit_parent_resolver_authority = args.get("inherit_parent_resolver_authority")
    try:
        kb, conn = _connect()
        try:
            created_event_extra = None
            current_task_id = os.environ.get("HERMES_KANBAN_TASK")
            if inherit_parent_resolver_authority is not None:
                if not current_task_id:
                    return tool_error(
                        "inherit_parent_resolver_authority is only available for scoped worker tasks"
                    )
                created_resolver, resolver_err = _build_inherited_resolver_authority(
                    kb,
                    conn,
                    parent_task_id=current_task_id,
                    request=inherit_parent_resolver_authority,
                )
                if resolver_err:
                    return resolver_err
                if created_resolver is not None:
                    created_event_extra = {"resolver_authority": created_resolver}

            new_tid = kb.create_task(
                conn,
                title=str(title).strip(),
                body=body,
                assignee=str(assignee),
                parents=tuple(parents),
                tenant=tenant,
                priority=int(priority) if priority is not None else 0,
                workspace_kind=str(workspace_kind),
                workspace_path=workspace_path,
                triage=triage,
                idempotency_key=idempotency_key,
                max_runtime_seconds=(
                    int(max_runtime_seconds)
                    if max_runtime_seconds is not None else None
                ),
                skills=skills,
                created_by=os.environ.get("HERMES_PROFILE") or "worker",
                created_event_extra=created_event_extra,
            )
            new_task = kb.get_task(conn, new_tid)
            return _ok(
                task_id=new_tid,
                status=new_task.status if new_task else None,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_create: {e}")
    except Exception as e:
        logger.exception("kanban_create failed")
        return tool_error(f"kanban_create: {e}")


def _handle_unblock(args: dict, **kw) -> str:
    """Transition a blocked task back to ready/todo when authorized."""
    tid = args.get("task_id")
    if not tid:
        return tool_error("task_id is required")
    try:
        kb, conn = _connect()
        try:
            if os.environ.get("HERMES_KANBAN_TASK"):
                ownership_err = _enforce_worker_task_ownership(
                    str(tid),
                    action="unblock",
                    kb=kb,
                    conn=conn,
                )
                if ownership_err:
                    return ownership_err
            ok = kb.unblock_task(conn, str(tid))
            if not ok:
                return tool_error(f"could not unblock {tid} (not blocked or unknown)")
            task = kb.get_task(conn, str(tid))
            return _ok(task_id=str(tid), status=task.status if task else "ready")
        finally:
            conn.close()
    except Exception as e:
        logger.exception("kanban_unblock failed")
        return tool_error(f"kanban_unblock: {e}")


def _handle_link(args: dict, **kw) -> str:
    """Add a parent→child dependency edge after the fact."""
    parent_id = args.get("parent_id")
    child_id = args.get("child_id")
    if not parent_id or not child_id:
        return tool_error("both parent_id and child_id are required")
    try:
        kb, conn = _connect()
        try:
            kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)
            return _ok(parent_id=parent_id, child_id=child_id)
        finally:
            conn.close()
    except ValueError as e:
        # Covers cycle + self-parent rejections
        return tool_error(f"kanban_link: {e}")
    except Exception as e:
        logger.exception("kanban_link failed")
        return tool_error(f"kanban_link: {e}")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_DESC_TASK_ID_DEFAULT = (
    "Task id. If omitted, defaults to HERMES_KANBAN_TASK from the env "
    "(the task the dispatcher spawned you to work on)."
)

KANBAN_SHOW_SCHEMA = {
    "name": "kanban_show",
    "description": (
        "Read a task's full state — title, body, assignee, parent task "
        "handoffs, your prior attempts on this task if any, comments, "
        "and recent events. Use this to (re)orient yourself before "
        "starting work, especially on retries. The response includes a "
        "pre-formatted ``worker_context`` string suitable for inclusion "
        "verbatim in your reasoning."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
        },
        "required": [],
    },
}

KANBAN_LIST_SCHEMA = {
    "name": "kanban_list",
    "description": (
        "List Kanban task summaries so an orchestrator profile can discover "
        "work to route. Supports the same core filters as the CLI: assignee, "
        "status, tenant, include_archived, and limit. Returns compact rows "
        "with ids, title, status, assignee, priority, parent/child ids, and "
        "counts. Bounded to 50 rows by default, 200 max, with truncation "
        "metadata. Also recomputes ready tasks before listing, matching the "
        "CLI. Orchestrator-only — dispatcher-spawned task workers never see "
        "this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "assignee": {
                "type": "string",
                "description": "Optional assignee/profile filter.",
            },
            "status": {
                "type": "string",
                "enum": [
                    "triage", "todo", "ready", "running",
                    "blocked", "done", "archived",
                ],
                "description": "Optional task status filter.",
            },
            "tenant": {
                "type": "string",
                "description": "Optional tenant/project namespace filter.",
            },
            "include_archived": {
                "type": "boolean",
                "description": "Include archived tasks. Defaults to false.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional maximum rows to return (default 50, max 200).",
            },
        },
        "required": [],
    },
}

KANBAN_COMPLETE_SCHEMA = {
    "name": "kanban_complete",
    "description": (
        "Mark your current task done with a structured handoff for "
        "downstream workers and humans. Kernel-created parent triage workers "
        "may also complete the exact blocked source card they were created "
        "to repair after local verification. Prefer ``summary`` for a "
        "human-readable 1-3 sentence description of what you did; put "
        "machine-readable facts in ``metadata`` (changed_files, "
        "tests_run, decisions, findings, etc). At least one of "
        "``summary`` or ``result`` is required. If you created new "
        "tasks via ``kanban_create`` during this run, list their ids "
        "in ``created_cards`` — the kernel verifies them so phantom "
        "references are caught before they leak into downstream "
        "automation. If this is a resolver-authorized repair card and "
        "the verified handoff is ready, set ``result`` to "
        "``READY_WITH_CONCRETE_CONTINUATION`` and provide "
        "``metadata.continuation_task_id``/``continuation_task_ids``; "
        "the kernel will fail closed unless those ids are concrete children "
        "of the blocked source gate, then complete that source gate and run "
        "a dispatcher tick."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "summary": {
                "type": "string",
                "description": (
                    "Human-readable handoff, 1-3 sentences. Appears in "
                    "Run History on the dashboard and in downstream "
                    "workers' context."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Free-form dict of structured facts about this "
                    "attempt — {\"changed_files\": [...], \"tests_run\": 12, "
                    "\"findings\": [...]}. Surfaced to downstream "
                    "workers alongside ``summary``."
                ),
            },
            "result": {
                "type": "string",
                "description": (
                    "Short result log line (legacy field, maps to "
                    "task.result). Use ``summary`` instead when "
                    "possible; this exists for compatibility with "
                    "callers that still set --result on the CLI."
                ),
            },
            "created_cards": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional structured manifest of task ids you "
                    "created via ``kanban_create`` during this run. "
                    "The kernel verifies each id exists and was "
                    "created by this worker's profile; any phantom "
                    "id blocks the completion with an error listing "
                    "what went wrong (auditable in the task's events). "
                    "Only list ids you got back from a successful "
                    "``kanban_create`` call — do not invent or "
                    "remember ids from prose. Omit the field if you "
                    "did not create any cards."
                ),
            },
        },
        "required": [],
    },
}

KANBAN_BLOCK_SCHEMA = {
    "name": "kanban_block",
    "description": (
        "Transition the task to blocked because you need human input "
        "to proceed. ``reason`` will be shown to the human on the "
        "board and included in context when someone unblocks you. "
        "Use for genuine blockers only — don't block on things you can "
        "resolve yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "reason": {
                "type": "string",
                "description": (
                    "What you need answered, in one or two sentences. "
                    "Don't paste the whole conversation; the human has "
                    "the board and can ask follow-ups via comments."
                ),
            },
        },
        "required": ["reason"],
    },
}

KANBAN_HEARTBEAT_SCHEMA = {
    "name": "kanban_heartbeat",
    "description": (
        "Signal that you're still alive during a long operation "
        "(training, encoding, large crawls). Call every few minutes so "
        "humans see liveness separately from PID checks. Pure side "
        "effect — no work changes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional short note describing current progress. "
                    "Shown in the event log."
                ),
            },
        },
        "required": [],
    },
}

KANBAN_COMMENT_SCHEMA = {
    "name": "kanban_comment",
    "description": (
        "Append a comment to a task's thread. Use for durable notes "
        "that should outlive this run (questions for the next worker, "
        "partial findings, rationale). Ephemeral reasoning doesn't "
        "belong here — use your normal response instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Task id. Required (may be your own task or "
                    "another's — comment threads are per-task)."
                ),
            },
            "body": {
                "type": "string",
                "description": "Markdown-supported comment body.",
            },
        },
        "required": ["task_id", "body"],
    },
}

KANBAN_CREATE_SCHEMA = {
    "name": "kanban_create",
    "description": (
        "Create a new kanban task, optionally as a child of the current "
        "one (pass the current task id in ``parents``). Used by "
        "orchestrator workers to fan out — decompose work into child "
        "tasks with specific assignees, link them into a pipeline, "
        "then complete your own task. The dispatcher picks up the new "
        "tasks on its next tick and spawns the assigned profiles."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short task title (required).",
            },
            "assignee": {
                "type": "string",
                "description": (
                    "Profile name that should execute this task "
                    "(e.g. 'researcher-a', 'reviewer', 'writer'). "
                    "Required — tasks without an assignee are never "
                    "dispatched."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "Opening post: full spec, acceptance criteria, "
                    "links. The assigned worker reads this as part of "
                    "its context."
                ),
            },
            "parents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Parent task ids. The new task stays in 'todo' "
                    "until every parent reaches 'done'; then it "
                    "auto-promotes to 'ready'. Typical fan-in: list "
                    "all the researcher task ids when creating a "
                    "synthesizer task."
                ),
            },
            "tenant": {
                "type": "string",
                "description": (
                    "Optional namespace for multi-project isolation. "
                    "Defaults to HERMES_TENANT env if set."
                ),
            },
            "priority": {
                "type": "integer",
                "description": (
                    "Dispatcher tiebreaker. Higher = picked sooner "
                    "when multiple ready tasks share an assignee."
                ),
            },
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "dir", "worktree"],
                "description": (
                    "Workspace flavor: 'scratch' (fresh tmp dir, "
                    "default), 'dir' (shared directory, requires "
                    "absolute workspace_path), 'worktree' (git worktree)."
                ),
            },
            "workspace_path": {
                "type": "string",
                "description": (
                    "Absolute path for 'dir' or 'worktree' workspace. "
                    "Relative paths are rejected at dispatch."
                ),
            },
            "triage": {
                "type": "boolean",
                "description": (
                    "If true, task lands in 'triage' instead of 'todo' "
                    "— a specifier profile is expected to flesh out "
                    "the body before work starts."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "If a non-archived task with this key already "
                    "exists, return that task's id instead of creating "
                    "a duplicate. Useful for retry-safe automation."
                ),
            },
            "max_runtime_seconds": {
                "type": "integer",
                "description": (
                    "Per-task runtime cap. When exceeded, the "
                    "dispatcher SIGTERMs the worker and re-queues the "
                    "task with outcome='timed_out'."
                ),
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Skill names to force-load into the dispatched "
                    "worker (in addition to the built-in kanban-worker "
                    "skill). Use this to pin a task to a specialist "
                    "context — e.g. ['translation'] for a translation "
                    "task, ['github-code-review'] for a reviewer task. "
                    "The names must match skills installed on the "
                    "assignee's profile."
                ),
            },
            "inherit_parent_resolver_authority": {
                "type": "object",
                "description": (
                    "Optional narrow inheritance for resolver-scoped workers: "
                    "derive child resolver_authority from the current task's "
                    "structured resolver metadata. source_task_id must match "
                    "the current authority source, and allowed_actions must be "
                    "an equal-or-narrower subset."
                ),
                "properties": {
                    "source_task_id": {"type": "string"},
                    "allowed_actions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["source_task_id", "allowed_actions"],
            },
        },
        "required": ["title", "assignee"],
    },
}

KANBAN_UNBLOCK_SCHEMA = {
    "name": "kanban_unblock",
    "description": (
        "Move a blocked Kanban task back to ready/todo. Available to "
        "orchestrator profiles, and to kernel-created parent triage workers "
        "only for the exact blocked source card they were created to repair. "
        "Ordinary dispatcher-spawned workers never see this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Blocked task id to return to ready.",
            },
        },
        "required": ["task_id"],
    },
}

KANBAN_LINK_SCHEMA = {
    "name": "kanban_link",
    "description": (
        "Add a parent→child dependency edge after both tasks already "
        "exist. The child won't promote to 'ready' until all parents "
        "are 'done'. Cycles and self-links are rejected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "parent_id": {"type": "string", "description": "Parent task id."},
            "child_id":  {"type": "string", "description": "Child task id."},
        },
        "required": ["parent_id", "child_id"],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="kanban_show",
    toolset="kanban",
    schema=KANBAN_SHOW_SCHEMA,
    handler=_handle_show,
    check_fn=_check_kanban_mode,
    emoji="📋",
)

registry.register(
    name="kanban_list",
    toolset="kanban",
    schema=KANBAN_LIST_SCHEMA,
    handler=_handle_list,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="📋",
)

registry.register(
    name="kanban_complete",
    toolset="kanban",
    schema=KANBAN_COMPLETE_SCHEMA,
    handler=_handle_complete,
    check_fn=_check_kanban_mode,
    emoji="✔",
)

registry.register(
    name="kanban_block",
    toolset="kanban",
    schema=KANBAN_BLOCK_SCHEMA,
    handler=_handle_block,
    check_fn=_check_kanban_mode,
    emoji="⏸",
)

registry.register(
    name="kanban_heartbeat",
    toolset="kanban",
    schema=KANBAN_HEARTBEAT_SCHEMA,
    handler=_handle_heartbeat,
    check_fn=_check_kanban_mode,
    emoji="💓",
)

registry.register(
    name="kanban_comment",
    toolset="kanban",
    schema=KANBAN_COMMENT_SCHEMA,
    handler=_handle_comment,
    check_fn=_check_kanban_mode,
    emoji="💬",
)

registry.register(
    name="kanban_create",
    toolset="kanban",
    schema=KANBAN_CREATE_SCHEMA,
    handler=_handle_create,
    check_fn=_check_kanban_mode,
    emoji="➕",
)

registry.register(
    name="kanban_unblock",
    toolset="kanban",
    schema=KANBAN_UNBLOCK_SCHEMA,
    handler=_handle_unblock,
    check_fn=_check_kanban_unblock_mode,
    emoji="▶",
)

registry.register(
    name="kanban_link",
    toolset="kanban",
    schema=KANBAN_LINK_SCHEMA,
    handler=_handle_link,
    check_fn=_check_kanban_mode,
    emoji="🔗",
)
