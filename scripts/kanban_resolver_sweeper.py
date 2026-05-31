#!/usr/bin/env python3
"""One-shot sweeper for resolver-metadata-backed parent-triage cards.

This operator tool is intentionally strict:
- default mode is dry-run;
- authority comes only from structured resolver metadata in task events;
- it never infers authority from task body/title/idempotency/comments;
- legacy/no-metadata cases are reported as manual_only.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from hermes_cli import kanban_db as kb

_ACCEPT_RE = re.compile(r"\bACCEPT(?:_[A-Z0-9_]+)?\b")
_RETRY_RE = re.compile(r"\bREADY_FOR_RETRY\b")


@dataclass(frozen=True)
class Candidate:
    """Resolver-backed triage candidate extracted from board state.

    pre: triage_task_id.startswith("t_")
    pre: source_task_id.startswith("t_")
    pre: source_event_id > 0
    post: __return__.triage_task_id == triage_task_id
    post: __return__.source_task_id == source_task_id
    """

    triage_task_id: str
    source_task_id: str
    source_event_id: int
    review_gate_task_id: Optional[str]
    triage_status: str
    source_status: Optional[str]
    evidence: str


@dataclass(frozen=True)
class Decision:
    """Action decision for one resolver-backed candidate.

    pre: action in {"complete", "unblock", "leave", "manual_only"}
    post: __return__.action == action
    """

    action: str
    reason: str


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--board", default=None, help="Kanban board slug (default resolver chain)")
    p.add_argument("--apply", action="store_true", help="Apply mutations (default: dry-run)")
    p.add_argument("--dry-run", action="store_true", help="Force dry-run output even if --apply not set")
    p.add_argument("--json", action="store_true", help="Emit JSON summary")
    return p.parse_args()


def _latest_evidence_text(conn: Any, task_id: str) -> str:
    """Return lowercase evidence text from latest run summary/metadata + comments.

    pre: task_id.startswith("t_")
    post: isinstance(__return__, str)
    """

    pieces: list[str] = []
    run = kb.latest_run(conn, task_id)
    if run is not None:
        if run.summary:
            pieces.append(run.summary)
        if run.metadata:
            try:
                pieces.append(json.dumps(run.metadata, ensure_ascii=False))
            except Exception:
                pass
    for c in kb.list_comments(conn, task_id):
        pieces.append(c.body)
    return "\n".join(pieces)


def _collect_candidates(conn: Any) -> list[Candidate]:
    """Collect triage tasks that carry strict resolver authority metadata.

    pre: conn is an open kanban_db connection
    post: all returned candidates are generated recovery-card tasks with resolver metadata
    """

    out: list[Candidate] = []
    for task in kb.list_tasks(conn, include_archived=True):
        # Only generated resolver/triage cards are actionable candidates. Source
        # tasks also receive a parent_triage_created event containing the same
        # resolver metadata for audit, but that event is not mutation authority
        # for the source task itself.
        if task.created_by != kb.ON_BLOCK_PARENT_TRIAGE_CREATED_BY:
            continue
        if not (task.idempotency_key or "").startswith(kb.ON_BLOCK_PARENT_TRIAGE_IDEMPOTENCY_PREFIX):
            continue

        # Never infer from idempotency/body; only structured resolver metadata.
        resolver_meta: Optional[dict[str, Any]] = None
        for ev in reversed(kb.list_events(conn, task.id)):
            resolver_meta = kb.read_on_block_parent_triage_resolver_metadata(ev.payload)
            if resolver_meta is not None:
                break
        if resolver_meta is None:
            continue

        source_task_id = str(resolver_meta["source_task_id"])
        if source_task_id == task.id:
            continue
        source_event_id = int(resolver_meta["source_event_id"])
        review_gate_task_id = resolver_meta.get("review_gate_task_id")
        source = kb.get_task(conn, source_task_id)

        evidence = _latest_evidence_text(conn, task.id)
        out.append(
            Candidate(
                triage_task_id=task.id,
                source_task_id=source_task_id,
                source_event_id=source_event_id,
                review_gate_task_id=(str(review_gate_task_id) if isinstance(review_gate_task_id, str) else None),
                triage_status=task.status,
                source_status=(source.status if source is not None else None),
                evidence=evidence,
            )
        )
    return out


def _has_parent_triage_created_event(conn: Any, task_id: str) -> bool:
    """Return True when task is a blocked source with a generated resolver card.

    pre: task_id.startswith("t_")
    post: __return__ is bool
    """

    for ev in kb.list_events(conn, task_id):
        if ev.kind != "parent_triage_created":
            continue
        if kb.read_on_block_parent_triage_resolver_metadata(ev.payload) is not None:
            return True
    return False


def _collect_manual_only_rows(conn: Any, candidate_ids: set[str]) -> list[dict[str, Any]]:
    """Report blocked non-candidate rows that require manual operator handling.

    pre: conn is an open kanban_db connection
    post: returned rows never authorize mutation and always have action manual_only
    """

    rows: list[dict[str, Any]] = []
    for task in kb.list_tasks(conn, include_archived=True):
        if task.id in candidate_ids:
            continue
        if task.status != "blocked":
            continue
        if _has_parent_triage_created_event(conn, task.id):
            continue
        rows.append(
            {
                "triage_task_id": task.id,
                "source_task_id": None,
                "source_event_id": None,
                "triage_status": task.status,
                "source_status_before": None,
                "action": "manual_only",
                "reason": "blocked task has no structured resolver authority",
                "mutated": False,
            }
        )
    return rows


def _has_real_user_decision_marker(conn: Any, task_id: str) -> bool:
    """Return True when source task explicitly requires external user decision.

    pre: task_id.startswith("t_")
    post: __return__ is bool
    """

    for ev in kb.list_events(conn, task_id):
        if ev.kind != "blocked_waiting_user_decision" or not isinstance(ev.payload, dict):
            continue
        if ev.payload.get("classification") == "REAL_USER_DECISION" and ev.payload.get("source_blocked_task_id") == task_id:
            return True
    return False


def _decide(conn: Any, candidate: Candidate) -> Decision:
    """Classify resolver-backed triage with strict metadata + blocker checks.

    pre: candidate was produced by _collect_candidates
    post: __return__.action in {"complete", "unblock", "leave"}
    """

    if _has_real_user_decision_marker(conn, candidate.source_task_id):
        return Decision("leave", "source has REAL_USER_DECISION marker")

    evidence = candidate.evidence
    if _RETRY_RE.search(evidence):
        return Decision("unblock", "READY_FOR_RETRY evidence found")
    if _ACCEPT_RE.search(evidence):
        return Decision("complete", "ACCEPT evidence found")

    # Conservative default for unresolved active triage: return source to queue,
    # do not hard-close it. This keeps real engineering blockers blocked only when
    # they are explicitly marked as REAL_USER_DECISION.
    if candidate.triage_status in {"ready", "running", "blocked"} and candidate.source_status == "blocked":
        return Decision("unblock", "metadata-backed triage without terminal marker -> unblock for retry")

    return Decision("leave", "no actionable evidence")


def _apply_decision(conn: Any, candidate: Candidate, decision: Decision) -> dict[str, Any]:
    """Apply one decision with strict source-task targeting.

    pre: decision.action in {"complete", "unblock", "leave", "manual_only"}
    post[conn]: mutates only candidate.source_task_id and candidate.triage_task_id for complete/unblock
    post: __return__["action"] == decision.action
    """

    result: dict[str, Any] = {
        "triage_task_id": candidate.triage_task_id,
        "source_task_id": candidate.source_task_id,
        "source_event_id": candidate.source_event_id,
        "triage_status": candidate.triage_status,
        "source_status_before": candidate.source_status,
        "action": decision.action,
        "reason": decision.reason,
        "mutated": False,
    }

    if decision.action in {"leave", "manual_only"}:
        return result

    source = kb.get_task(conn, candidate.source_task_id)
    triage = kb.get_task(conn, candidate.triage_task_id)
    if source is None or triage is None:
        result["action"] = "leave"
        result["reason"] = "source/triage task missing"
        return result

    summary = (
        "resolver sweeper apply: "
        f"{decision.action} by metadata authority "
        f"(source_event_id={candidate.source_event_id})"
    )

    if decision.action == "complete":
        source_ok = source.status in {"blocked", "ready", "running"} and kb.complete_task(
            conn,
            source.id,
            summary=summary,
            metadata={
                "source": "kanban_resolver_sweeper",
                "triage_task_id": candidate.triage_task_id,
                "source_event_id": candidate.source_event_id,
                "action": "complete",
            },
        )
        triage_ok = triage.status in {"blocked", "ready", "running"} and kb.complete_task(
            conn,
            triage.id,
            summary=f"resolver sweeper settled source {source.id} via complete",
            metadata={
                "source": "kanban_resolver_sweeper",
                "source_task_id": source.id,
                "source_event_id": candidate.source_event_id,
                "action": "complete",
            },
        )
        if source_ok:
            kb.add_comment(conn, source.id, "kanban_resolver_sweeper", summary)
        if triage_ok:
            kb.add_comment(conn, triage.id, "kanban_resolver_sweeper", f"applied complete for source {source.id}")
        result["mutated"] = bool(source_ok or triage_ok)
        result["source_mutated"] = bool(source_ok)
        result["triage_mutated"] = bool(triage_ok)
        return result

    # unblock path
    source_ok = source.status == "blocked" and kb.unblock_task(conn, source.id)
    triage_ok = triage.status in {"blocked", "ready", "running"} and kb.complete_task(
        conn,
        triage.id,
        summary=f"resolver sweeper unblocked source {source.id} for retry",
        metadata={
            "source": "kanban_resolver_sweeper",
            "source_task_id": source.id,
            "source_event_id": candidate.source_event_id,
            "action": "unblock",
        },
    )
    if source_ok:
        kb.add_comment(conn, source.id, "kanban_resolver_sweeper", summary)
    if triage_ok:
        kb.add_comment(conn, triage.id, "kanban_resolver_sweeper", f"applied unblock for source {source.id}")
    result["mutated"] = bool(source_ok or triage_ok)
    result["source_mutated"] = bool(source_ok)
    result["triage_mutated"] = bool(triage_ok)
    return result


def main() -> int:
    """Run the one-shot resolver sweeper.

    pre: kanban DB is reachable for the selected board
    post: returns process exit code 0 on normal completion
    """

    args = _parse_args()
    do_apply = bool(args.apply and not args.dry_run)

    board_for_connect = args.board
    if board_for_connect and not kb.board_exists(board_for_connect):
        board_for_connect = None

    with kb.connect(board=board_for_connect) as conn:
        candidates = _collect_candidates(conn)
        manual_rows = _collect_manual_only_rows(conn, {c.triage_task_id for c in candidates})
        decisions = [{"candidate": c, "decision": _decide(conn, c)} for c in candidates]

        rows: list[dict[str, Any]] = []
        if do_apply:
            for item in decisions:
                rows.append(_apply_decision(conn, item["candidate"], item["decision"]))
            rows.extend(manual_rows)
        else:
            for item in decisions:
                c = item["candidate"]
                d = item["decision"]
                rows.append(
                    {
                        "triage_task_id": c.triage_task_id,
                        "source_task_id": c.source_task_id,
                        "source_event_id": c.source_event_id,
                        "triage_status": c.triage_status,
                        "source_status_before": c.source_status,
                        "action": d.action,
                        "reason": d.reason,
                        "mutated": False,
                    }
                )
            rows.extend(manual_rows)

    payload = {
        "board": board_for_connect,
        "requested_board": args.board,
        "mode": "apply" if do_apply else "dry_run",
        "count": len(rows),
        "results": rows,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"mode={payload['mode']} count={payload['count']}")
        for row in rows:
            print(
                f"- triage={row['triage_task_id']} source={row['source_task_id']} "
                f"action={row['action']} mutated={row['mutated']} reason={row['reason']}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
