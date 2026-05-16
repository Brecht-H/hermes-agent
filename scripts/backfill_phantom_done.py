#!/usr/bin/env python3
"""Backfill triage for phantom-done kanban tasks.

A "phantom-done" task is one that reached ``status='done'`` with an empty
``result`` AND zero ``task_comments`` — i.e. no audit trail of what was
actually delivered. The completion-evidence invariant added to
``hermes_cli.kanban_db.complete_task`` prevents *new* phantom-done tasks,
but pre-existing ones must be triaged and backfilled separately.

This script:
  * enumerates the phantom-done set;
  * for each task, greps ``/home/orion/hermes-plan`` for a deliverable
    file that references the task id, or that closely matches the task
    title;
  * prints a triage table:
        task_id | assignee | title | DELIVERABLE_FOUND:<path> | NO_TRACE
  * with ``--apply`` (default OFF), writes the found deliverable path
    into ``tasks.result`` for tasks with a trace. Tasks with no trace
    are printed for manual review and left untouched (never auto-reopened).

DEFAULT RUN = DRY-RUN ONLY. The live backfill is a Mac-gated operator
step — do not run with ``--apply`` without explicit sign-off.

Usage:
    python scripts/backfill_phantom_done.py           # dry-run triage
    python scripts/backfill_phantom_done.py --apply    # Mac-gated only
    python scripts/backfill_phantom_done.py --db /path/to/kanban.db
    python scripts/backfill_phantom_done.py --plan-dir /home/orion/hermes-plan
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("HERMES_KANBAN_DB", "")).expanduser() or (
    Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
    / "kanban.db"
)
DEFAULT_PLAN_DIR = Path("/home/orion/hermes-plan")

# The canonical phantom-done query (locked by task t_f625039d).
PHANTOM_DONE_SQL = """
    SELECT id, assignee, title
      FROM tasks
     WHERE status = 'done'
       AND (result IS NULL OR result = '')
       AND NOT EXISTS (
           SELECT 1 FROM task_comments WHERE task_id = tasks.id
       )
     ORDER BY assignee, id
"""


def _normalize_title(title: str) -> set[str]:
    """Tokenize a title into lowercased keyword set, dropping ids,
    phase/priority markers and dates so a 'close keyword match' is
    robust to cosmetic noise."""
    s = title.lower()
    s = re.sub(r"\bt_[a-f0-9]{8,}\b", "", s)
    s = re.sub(r"\b(p|phase|prio|priority)\s*\d+[a-z]?\b", "", s)
    s = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return {tok for tok in s.split() if len(tok) > 3}


def _iter_plan_files(plan_dir: Path):
    """Yield (path, text) for every readable text-ish file under plan_dir."""
    if not plan_dir.is_dir():
        return
    for path in sorted(plan_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".md", ".txt", ".rst", ".json", ""}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeError):
            continue
        yield path, text


def find_deliverable(
    task_id: str,
    title: str,
    plan_files: list[tuple[Path, str]],
) -> str | None:
    """Return a deliverable path for the task, or None if no trace found.

    Priority 1: a file whose contents reference the task id verbatim.
    Priority 2: a file whose title keyword overlap with the task title
    is strong (>= 3 shared keywords or full keyword containment).
    """
    tid_lower = task_id.lower()
    # P1: explicit task-id reference.
    for path, text in plan_files:
        if tid_lower in text.lower():
            return f"DELIVERABLE_FOUND:{path}"
    # P2: close title-keyword match.
    title_kw = _normalize_title(title)
    if title_kw:
        best_path = None
        best_overlap = 0
        for path, text in plan_files:
            haystack = (path.name + " " + text[:4000]).lower()
            hay_tokens = set(re.sub(r"[^a-z0-9]+", " ", haystack).split())
            overlap = len(title_kw & hay_tokens)
            if overlap > best_overlap:
                best_overlap = overlap
                best_path = path
        if best_path is not None and (
            best_overlap >= 3 or best_overlap == len(title_kw)
        ):
            return f"DELIVERABLE_FOUND:{best_path}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Triage and (optionally) backfill phantom-done kanban tasks.",
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"Path to kanban.db (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--plan-dir", type=Path, default=DEFAULT_PLAN_DIR,
        help=f"Directory to grep for deliverables (default: {DEFAULT_PLAN_DIR})",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Write found deliverables into tasks.result. "
             "DEFAULT OFF — this is a Mac-gated operator step.",
    )
    args = parser.parse_args(argv)

    if not args.db.is_file():
        print(f"error: kanban DB not found: {args.db}", file=sys.stderr)
        return 2

    mode = "APPLY (live mutation)" if args.apply else "DRY-RUN (no mutation)"
    print(f"# Phantom-done backfill triage — mode: {mode}")
    print(f"# DB:        {args.db}")
    print(f"# Plan dir:  {args.plan_dir}")
    print()

    # Read-only connection for enumeration; reopened RW only under --apply.
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(PHANTOM_DONE_SQL).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No phantom-done tasks found. Nothing to triage.")
        return 0

    plan_files = list(_iter_plan_files(args.plan_dir))
    print(f"# {len(rows)} phantom-done task(s); scanning {len(plan_files)} plan file(s).")
    print()

    header = f"{'TASK_ID':<16} | {'ASSIGNEE':<12} | {'TITLE':<48} | TRACE"
    print(header)
    print("-" * len(header))

    found: list[tuple[str, str]] = []
    no_trace: list[sqlite3.Row] = []
    for row in rows:
        tid = row["id"]
        assignee = (row["assignee"] or "-")[:12]
        title = (row["title"] or "")[:48]
        trace = find_deliverable(tid, row["title"] or "", plan_files)
        if trace:
            found.append((tid, trace.split("DELIVERABLE_FOUND:", 1)[1]))
            print(f"{tid:<16} | {assignee:<12} | {title:<48} | {trace}")
        else:
            no_trace.append(row)
            print(f"{tid:<16} | {assignee:<12} | {title:<48} | NO_TRACE")

    print()
    print(f"# Summary: {len(found)} with deliverable, {len(no_trace)} with NO_TRACE.")

    if no_trace:
        print()
        print("# NO_TRACE tasks (left for manual review — NOT auto-reopened):")
        for row in no_trace:
            print(f"#   {row['id']}  [{row['assignee'] or '-'}]  {row['title']}")

    if not args.apply:
        print()
        print("# DRY-RUN complete. Re-run with --apply (Mac-gated) to write "
              "deliverable paths into tasks.result.")
        return 0

    # --apply: write found deliverables into tasks.result.
    if not found:
        print()
        print("# --apply: nothing to write (no deliverables found).")
        return 0

    print()
    print(f"# --apply: writing {len(found)} deliverable path(s) into tasks.result …")
    conn = sqlite3.connect(str(args.db))
    try:
        now = int(time.time())
        for tid, path in found:
            result_text = f"[backfilled {time.strftime('%Y-%m-%d')}] deliverable: {path}"
            conn.execute(
                "UPDATE tasks SET result = ? WHERE id = ? "
                "AND (result IS NULL OR result = '')",
                (result_text, tid),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, 'result_backfilled', ?, ?)",
                (tid, f'{{"deliverable": "{path}"}}', now),
            )
            print(f"#   {tid} ← {path}")
        conn.commit()
    finally:
        conn.close()
    print(f"# --apply complete. {len(found)} task(s) backfilled. "
          f"{len(no_trace)} NO_TRACE task(s) untouched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
