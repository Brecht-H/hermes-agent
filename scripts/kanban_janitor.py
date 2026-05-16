#!/usr/bin/env python3
"""Hermes kanban janitor.

Conservatively keeps the Hermes kanban board usable by auto-closing tasks that
already have hard completion evidence, and reporting stale/redundant work for
human or agent follow-up.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

TERMINAL_STATUSES = {"done", "archived"}
ACTIVE_STATUSES = {"todo", "ready", "running", "blocked", "triage", "backlog", "in_review", "in_progress"}
VALID_STATUSES = {"triage", "todo", "ready", "running", "blocked", "done", "archived"}
DEFAULT_REPORT_DIR = Path("/home/orion/.hermes/reports")
DEFAULT_BACKUP_DIR = Path("/home/orion/.hermes/backups/kanban_janitor")
DEFAULT_MARKDOWN_COPY = Path("/home/orion/hermes-plan/KANBAN_JANITOR_LATEST.md")
JANITOR_AUTHOR = "kanban-janitor"

COMPLETE_COMMENT_PATTERNS = [
    re.compile(r"STUCK-IN-REVIEW SWEEP[\s\S]{0,600}?please run kanban complete\s+(t_[a-f0-9]+)", re.IGNORECASE),
    re.compile(r"\b(?:closing|close|closed|marking|marked)\s+(t_[a-f0-9]+)\s+(?:as\s+)?(?:approved/)?done\b", re.IGNORECASE),
    re.compile(r"\b(?:closing|close|closed|marking|marked)\s+(t_[a-f0-9]+)\s+(?:as\s+)?complete(?:d)?\b", re.IGNORECASE),
    re.compile(r"\bcomplete\s+(t_[a-f0-9]+)\b", re.IGNORECASE),
]

WEAK_COMPLETE_RE = re.compile(
    r"\b(merged|approved|done|complete(?:d)?|post-merge|verified|ship(?:ped)?|closed)\b",
    re.IGNORECASE,
)
REDUNDANT_RE = re.compile(
    r"\b(duplicate|duplicates|superseded|obsolete|redundant|folded into|replaced by|covered by)\b",
    re.IGNORECASE,
)
REFERENCE_TASK_RE = re.compile(r"\bt_[a-f0-9]{8}\b", re.IGNORECASE)


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    body: str | None
    assignee: str | None
    status: str
    priority: int | None
    created_by: str | None
    created_at: int | None
    started_at: int | None
    completed_at: int | None
    result: str | None
    current_run_id: str | None
    claim_lock: str | None
    claim_expires: int | None
    worker_pid: int | None
    consecutive_failures: int | None
    last_failure_error: str | None


@dataclass(frozen=True)
class Comment:
    id: str
    task_id: str
    author: str | None
    body: str
    created_at: int | None


@dataclass(frozen=True)
class CloseDecision:
    task_id: str
    title: str
    previous_status: str
    reason: str
    evidence: str


def now_ts() -> int:
    return int(time.time())


def to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def rget(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=rget(row, "id"),
        title=rget(row, "title", "") or "",
        body=rget(row, "body"),
        assignee=rget(row, "assignee"),
        status=rget(row, "status", "") or "",
        priority=to_int(rget(row, "priority")),
        created_by=rget(row, "created_by"),
        created_at=to_int(rget(row, "created_at")),
        started_at=to_int(rget(row, "started_at")),
        completed_at=to_int(rget(row, "completed_at")),
        result=rget(row, "result"),
        current_run_id=rget(row, "current_run_id"),
        claim_lock=rget(row, "claim_lock"),
        claim_expires=to_int(rget(row, "claim_expires")),
        worker_pid=to_int(rget(row, "worker_pid")),
        consecutive_failures=to_int(rget(row, "consecutive_failures")),
        last_failure_error=rget(row, "last_failure_error"),
    )


def row_to_comment(row: sqlite3.Row) -> Comment:
    return Comment(
        id=str(row["id"]),
        task_id=row["task_id"],
        author=row["author"],
        body=row["body"] or "",
        created_at=to_int(row["created_at"]),
    )


def fetch_tasks(con: sqlite3.Connection) -> list[Task]:
    return [row_to_task(r) for r in con.execute("SELECT * FROM tasks ORDER BY created_at ASC, id ASC")]


def fetch_comments(con: sqlite3.Connection) -> dict[str, list[Comment]]:
    comments: dict[str, list[Comment]] = defaultdict(list)
    for row in con.execute("SELECT * FROM task_comments ORDER BY created_at ASC, id ASC"):
        comment = row_to_comment(row)
        comments[comment.task_id].append(comment)
    return comments


def shorten(text: str | None, max_len: int = 260) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1].rstrip() + "..."


def normalize_title(title: str) -> str:
    s = title.lower()
    s = re.sub(r"\bt_[a-f0-9]{8}\b", "", s)
    s = re.sub(r"\b(p|phase|prio|priority)\s*\d+[a-z]?\b", "", s)
    s = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def comments_text(comments: Iterable[Comment]) -> str:
    return "\n".join(c.body for c in comments if c.body)


def has_completion_evidence(task: Task, comments: list[Comment]) -> bool:
    """Return True if the task carries real completion evidence.

    Evidence is satisfied by ANY of:
      * a non-empty ``result`` (after strip);
      * at least one comment whose body is non-empty (after strip).

    A bare ``completed_at`` timestamp is NOT evidence — a brief worker
    claim can stamp ``completed_at`` without ever recording what was
    delivered. This mirrors ``kanban_db._has_completion_evidence``; the
    janitor uses its own ``Task``/``Comment`` dataclasses so it needs a
    local copy of the predicate, kept semantically identical.
    """
    if (task.result or "").strip():
        return True
    return any((c.body or "").strip() for c in comments)


def explicit_completion_evidence(task: Task, comments: list[Comment]) -> tuple[str, str] | None:
    if task.status == "running":
        return None
    if (
        task.completed_at is not None
        and task.status not in TERMINAL_STATUSES
        and has_completion_evidence(task, comments)
    ):
        evidence = task.result or comments[-1].body if comments else task.result
        return "completed_at_present", shorten(evidence or "task has completed_at but non-terminal status")

    text = comments_text(comments)
    for pat in COMPLETE_COMMENT_PATTERNS:
        for match in pat.finditer(text):
            if match.group(1).lower() == task.id.lower():
                return "explicit_complete_comment", shorten(match.group(0))

    for comment in reversed(comments):
        if (comment.author or "").lower() not in {"mac", "opus", "codex", "orion-cc", "kanban-janitor"}:
            continue
        body = comment.body or ""
        if task.id.lower() not in body.lower():
            continue
        if re.search(r"\b(close|closing|closed|complete|completed|done)\b", body, re.IGNORECASE):
            return "trusted_operator_completion_comment", shorten(body)

    return None


def weak_completion_candidate(task: Task, comments: list[Comment]) -> str | None:
    if task.status in TERMINAL_STATUSES or task.status == "running":
        return None
    text = "\n".join([task.result or "", comments_text(comments)])
    if WEAK_COMPLETE_RE.search(text):
        return shorten(text)
    return None


def redundant_evidence(task: Task, comments: list[Comment]) -> dict[str, Any] | None:
    text = "\n".join([task.title, task.body or "", task.result or "", comments_text(comments)])
    if not REDUNDANT_RE.search(text):
        return None
    refs = sorted({r.lower() for r in REFERENCE_TASK_RE.findall(text) if r.lower() != task.id.lower()})
    return {"task_id": task.id, "title": task.title, "status": task.status, "references": refs, "evidence": shorten(text)}


def latest_activity(task: Task, comments: list[Comment]) -> int:
    values = [v for v in [task.created_at, task.started_at, task.completed_at] if v]
    values.extend(c.created_at for c in comments if c.created_at)
    return max(values) if values else 0


def build_scan(db_path: Path, legacy: bool = False) -> dict[str, Any]:
    with connect(db_path) as con:
        tasks = fetch_tasks(con)
        comments_by_task = fetch_comments(con)

    status_counts = Counter(t.status for t in tasks)
    close_decisions: list[CloseDecision] = []
    weak_candidates: list[dict[str, Any]] = []
    redundant_candidates: list[dict[str, Any]] = []
    phantom_completed_at: list[dict[str, Any]] = []
    stale_ready: list[dict[str, Any]] = []
    stale_blocked: list[dict[str, Any]] = []
    actionable: list[dict[str, Any]] = []
    unknown_statuses = sorted(s for s in status_counts if s not in VALID_STATUSES)
    n = now_ts()

    nonterminal_by_title: dict[str, list[Task]] = defaultdict(list)

    for task in tasks:
        comments = comments_by_task.get(task.id, [])
        decision = explicit_completion_evidence(task, comments)
        if decision and task.status not in TERMINAL_STATUSES:
            reason, evidence = decision
            close_decisions.append(CloseDecision(task.id, task.title, task.status, reason, evidence))
        else:
            weak = weak_completion_candidate(task, comments)
            if weak:
                weak_candidates.append({"task_id": task.id, "title": task.title, "status": task.status, "evidence": weak})

        red = redundant_evidence(task, comments)
        if red and task.status not in TERMINAL_STATUSES:
            redundant_candidates.append(red)

        if (
            task.completed_at is not None
            and task.status not in TERMINAL_STATUSES
            and task.status != "running"
            and not has_completion_evidence(task, comments)
        ):
            phantom_completed_at.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    "status": task.status,
                    "completed_at": task.completed_at,
                }
            )

        key = normalize_title(task.title)
        if key and task.status not in TERMINAL_STATUSES:
            nonterminal_by_title[key].append(task)

        last = latest_activity(task, comments)
        age_days = round((n - last) / 86400, 1) if last else None
        if task.status == "ready" and last and n - last > 3 * 86400:
            stale_ready.append({"task_id": task.id, "title": task.title, "priority": task.priority, "age_days": age_days})
        if task.status == "blocked" and last and n - last > 7 * 86400:
            stale_blocked.append({"task_id": task.id, "title": task.title, "priority": task.priority, "age_days": age_days, "last_error": shorten(task.last_failure_error)})
        if task.status in {"ready", "todo", "triage", "backlog"}:
            actionable.append({"task_id": task.id, "title": task.title, "status": task.status, "priority": task.priority, "age_days": age_days})

    duplicate_groups = []
    for key, group in nonterminal_by_title.items():
        if len(group) < 2:
            continue
        duplicate_groups.append(
            {
                "normalized_title": key,
                "tasks": [
                    {"task_id": t.id, "title": t.title, "status": t.status, "priority": t.priority}
                    for t in sorted(group, key=lambda x: (x.priority is None, x.priority or 999, x.created_at or 0))
                ],
            }
        )

    actionable.sort(key=lambda x: (x["priority"] is None, x["priority"] if x["priority"] is not None else 999, x["age_days"] or 0))

    return {
        "db_path": str(db_path),
        "legacy": legacy,
        "exists": db_path.exists(),
        "generated_at": n,
        "task_count": len(tasks),
        "status_counts": dict(sorted(status_counts.items())),
        "unknown_statuses": unknown_statuses,
        "close_decisions": [d.__dict__ for d in close_decisions],
        "weak_completion_candidates": weak_candidates[:50],
        "redundant_candidates": redundant_candidates[:50],
        "phantom_completed_at": phantom_completed_at[:50],
        "duplicate_title_groups": duplicate_groups[:50],
        "stale_ready": stale_ready[:50],
        "stale_blocked": stale_blocked[:50],
        "actionable_next": actionable[:30],
    }


def backup_db(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    target = backup_dir / f"{db_path.name}.{stamp}.bak"
    shutil.copy2(db_path, target)
    backups = sorted(backup_dir.glob(f"{db_path.name}.*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[25:]:
        try:
            old.unlink()
        except OSError:
            pass
    return target


def apply_closes(db_path: Path, decisions: list[dict[str, Any]], backup_dir: Path) -> dict[str, Any]:
    if not decisions:
        return {"applied": 0, "backup": None, "closed": []}

    backup = backup_db(db_path, backup_dir)
    closed: list[dict[str, Any]] = []
    n = now_ts()

    with connect(db_path) as con:
        con.execute("BEGIN IMMEDIATE")
        for item in decisions:
            task_id = item["task_id"]
            row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                continue
            task = row_to_task(row)
            if task.status in TERMINAL_STATUSES or task.status == "running":
                continue
            reason = item["reason"]
            evidence = item["evidence"]
            comment = (
                f"Auto-closed by kanban_janitor: {reason}. "
                f"Previous status={task.status}. Evidence: {evidence}"
            )
            result = f"Auto-closed by kanban_janitor: {reason}. Evidence: {evidence}"
            con.execute(
                """
                UPDATE tasks
                   SET status = 'done',
                       completed_at = COALESCE(completed_at, ?),
                       claim_lock = NULL,
                       claim_expires = NULL,
                       worker_pid = NULL,
                       current_run_id = NULL,
                       result = COALESCE(result, ?)
                 WHERE id = ?
                   AND status NOT IN ('done', 'archived', 'running')
                """,
                (n, result, task_id),
            )
            if con.total_changes:
                con.execute(
                    "INSERT INTO task_comments(task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                    (task_id, JANITOR_AUTHOR, comment, n),
                )
                con.execute(
                    "INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                    (
                        task_id,
                        "janitor_completed",
                        json.dumps(
                            {
                                "previous_status": task.status,
                                "reason": reason,
                                "evidence": evidence,
                                "source": JANITOR_AUTHOR,
                            },
                            sort_keys=True,
                        ),
                        n,
                    ),
                )
                if task.current_run_id:
                    con.execute(
                        """
                        UPDATE task_runs
                           SET status = CASE WHEN status IN ('running', 'claimed', 'ready') THEN 'done' ELSE status END,
                               outcome = COALESCE(outcome, 'completed'),
                               ended_at = COALESCE(ended_at, ?),
                               summary = COALESCE(summary, ?)
                         WHERE id = ?
                           AND ended_at IS NULL
                        """,
                        (n, result, task.current_run_id),
                    )
                closed.append(
                    {
                        "task_id": task_id,
                        "title": task.title,
                        "previous_status": task.status,
                        "reason": reason,
                    }
                )
        con.commit()

    return {"applied": len(closed), "backup": str(backup), "closed": closed}


def render_markdown(report: dict[str, Any]) -> str:
    primary = report["primary"]
    applied = report.get("apply_result", {})
    lines = [
        "# Hermes Kanban Janitor Latest",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(report['generated_at']))}",
        f"Mode: {'apply' if report.get('apply') else 'dry-run'}",
        f"Primary DB: `{primary['db_path']}`",
        "",
        "## Status Counts",
        "",
    ]
    for status, count in primary["status_counts"].items():
        lines.append(f"- {status}: {count}")
    if primary["unknown_statuses"]:
        lines.extend(["", f"Unknown/nonstandard statuses: {', '.join(primary['unknown_statuses'])}"])

    scheduler = report.get("scheduler")
    if scheduler:
        lines.extend(["", "## Scheduler", ""])
        if scheduler.get("kind") == "hermes-cron" and scheduler.get("id"):
            lines.append(f"Owner: Hermes cron `{scheduler['id']}` ({scheduler.get('name')})")
            lines.append(f"Schedule: {scheduler.get('schedule')} | enabled={scheduler.get('enabled')} | state={scheduler.get('state')}")
            lines.append(f"Last run: {scheduler.get('last_run_at')} ({scheduler.get('last_status')})")
            lines.append(f"Next run: {scheduler.get('next_run_at')}")
        else:
            lines.append(f"Owner detection: `{json.dumps(scheduler, sort_keys=True)}`")

    lines.extend(["", "## Auto Close", ""])
    lines.append(f"Applied: {applied.get('applied', 0)}")
    if applied.get("backup"):
        lines.append(f"Backup: `{applied['backup']}`")
    decisions = applied.get("closed") if report.get("apply") else primary["close_decisions"]
    if decisions:
        for item in decisions[:50]:
            lines.append(f"- {item['task_id']} [{item.get('previous_status', '')}] {item['title']} ({item['reason']})")
    else:
        lines.append("- None")

    lines.extend(["", "## Needs Triage", ""])
    if primary["redundant_candidates"]:
        lines.append("Redundant/superseded candidates:")
        for item in primary["redundant_candidates"][:20]:
            refs = f" refs={','.join(item['references'])}" if item.get("references") else ""
            lines.append(f"- {item['task_id']} [{item['status']}] {item['title']}{refs}")
    else:
        lines.append("Redundant/superseded candidates: none")

    phantom = primary.get("phantom_completed_at") or []
    lines.append("")
    lines.append(
        "Phantom completed_at — bare timestamp, no evidence, NOT auto-closed — needs human triage:"
    )
    if phantom:
        for item in phantom[:20]:
            lines.append(
                f"- {item['task_id']} [{item['status']}] completed_at={item.get('completed_at')} {item['title']}"
            )
    else:
        lines.append("- None")

    if primary["stale_ready"]:
        lines.append("")
        lines.append("Stale ready tasks:")
        for item in primary["stale_ready"][:20]:
            lines.append(f"- {item['task_id']} P{item.get('priority')} age={item.get('age_days')}d {item['title']}")

    if primary["stale_blocked"]:
        lines.append("")
        lines.append("Stale blocked tasks:")
        for item in primary["stale_blocked"][:20]:
            lines.append(f"- {item['task_id']} P{item.get('priority')} age={item.get('age_days')}d {item['title']}")

    if primary["duplicate_title_groups"]:
        lines.append("")
        lines.append("Duplicate title groups:")
        for group in primary["duplicate_title_groups"][:10]:
            ids = ", ".join(t["task_id"] for t in group["tasks"])
            lines.append(f"- {group['normalized_title']}: {ids}")

    lines.extend(["", "## Actionable Next", ""])
    if primary["actionable_next"]:
        for item in primary["actionable_next"][:20]:
            lines.append(f"- {item['task_id']} [{item['status']}] P{item.get('priority')} {item['title']}")
    else:
        lines.append("- None")

    legacy = report.get("legacy")
    if legacy:
        lines.extend(["", "## Legacy/Profile DB", ""])
        lines.append(f"DB: `{legacy['db_path']}`")
        lines.append(f"Status counts: `{json.dumps(legacy['status_counts'], sort_keys=True)}`")
        lines.append(f"Would auto-close: {len(legacy['close_decisions'])}")
    return "\n".join(lines) + "\n"


def write_reports(report: dict[str, Any], report_dir: Path, markdown_copy: Path | None) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    latest_json = report_dir / "kanban_janitor_latest.json"
    latest_md = report_dir / "KANBAN_JANITOR_LATEST.md"
    history = report_dir / "kanban_janitor.jsonl"
    latest_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    md = render_markdown(report)
    latest_md.write_text(md)
    with history.open("a") as fh:
        fh.write(json.dumps(report, sort_keys=True) + "\n")
    if markdown_copy:
        markdown_copy.parent.mkdir(parents=True, exist_ok=True)
        markdown_copy.write_text(md)


def _scheduler_jobs_paths(hermes_home: Path) -> list[Path]:
    profile = os.environ.get("HERMES_PROFILE", "daily")
    paths = [
        hermes_home / "profiles" / profile / "cron" / "jobs.json",
        hermes_home / "profiles" / "daily" / "cron" / "jobs.json",
        hermes_home / "cron" / "jobs.json",
    ]
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        if path not in seen:
            out.append(path)
            seen.add(path)
    return out


def _is_janitor_job(job: dict[str, Any]) -> bool:
    script = str(job.get("script") or "")
    name = str(job.get("name") or "").lower()
    return script in {"kanban_janitor_cron.py", "kanban_janitor_daily.py"} or (
        "kanban" in name and "janitor" in name
    )


def _scheduler_snapshot(job: dict[str, Any], jobs_path: Path) -> dict[str, Any]:
    return {
        "kind": "hermes-cron",
        "jobs_path": str(jobs_path),
        "id": job.get("id"),
        "name": job.get("name"),
        "script": job.get("script"),
        "workdir": job.get("workdir"),
        "no_agent": job.get("no_agent"),
        "enabled": job.get("enabled"),
        "state": job.get("state"),
        "schedule": job.get("schedule_display"),
        "next_run_at": job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "last_error": job.get("last_error"),
    }


def detect_scheduler() -> dict[str, Any] | None:
    hermes_home = Path(os.environ.get("HERMES_HOME", "/home/orion/.hermes")).expanduser()
    scanned: list[str] = []
    fallback: dict[str, Any] | None = None

    for jobs_path in _scheduler_jobs_paths(hermes_home):
        scanned.append(str(jobs_path))
        if not jobs_path.exists():
            continue
        try:
            data = json.loads(jobs_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"kind": "hermes-cron", "jobs_path": str(jobs_path), "read_error": True}

        for job in data.get("jobs", []):
            if not _is_janitor_job(job):
                continue
            snapshot = _scheduler_snapshot(job, jobs_path)
            if job.get("enabled") and job.get("state") == "scheduled":
                return snapshot | {"scanned": scanned}
            fallback = fallback or snapshot

    if fallback:
        return fallback | {"scanned": scanned, "active_found": False}
    return {"kind": "unknown", "scanned": scanned, "found": False}


def run(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"kanban_janitor: DB not found: {db_path}", file=sys.stderr)
        return 2

    primary_scan = build_scan(db_path, legacy=False)
    apply_result = {"applied": 0, "backup": None, "closed": []}
    if args.apply:
        apply_result = apply_closes(db_path, primary_scan["close_decisions"], Path(args.backup_dir).expanduser())
        primary_scan = build_scan(db_path, legacy=False)

    legacy_scan = None
    if args.legacy_db:
        legacy_path = Path(args.legacy_db).expanduser()
        if legacy_path.exists():
            legacy_scan = build_scan(legacy_path, legacy=True)

    report = {
        "generated_at": now_ts(),
        "apply": bool(args.apply),
        "primary": primary_scan,
        "legacy": legacy_scan,
        "apply_result": apply_result,
        "scheduler": detect_scheduler(),
    }
    write_reports(report, Path(args.report_dir).expanduser(), None if args.no_markdown_copy else Path(args.markdown_copy).expanduser())
    summary = {
        "apply": args.apply,
        "applied": apply_result["applied"],
        "status_counts": primary_scan["status_counts"],
        "report": str(Path(args.report_dir).expanduser() / "KANBAN_JANITOR_LATEST.md"),
    }
    if not args.silent_noop or apply_result["applied"]:
        print(json.dumps(summary, sort_keys=True))
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Conservative janitor for Hermes kanban boards")
    p.add_argument("--db", default="/home/orion/.hermes/kanban.db", help="Primary kanban SQLite DB")
    p.add_argument("--legacy-db", default="/home/orion/.hermes/profiles/daily/kanban.db", help="Optional legacy/profile DB to scan read-only")
    p.add_argument("--apply", action="store_true", help="Apply safe auto-close decisions")
    p.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="Report output directory")
    p.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR), help="Backup directory for DB copies before mutations")
    p.add_argument("--markdown-copy", default=str(DEFAULT_MARKDOWN_COPY), help="Extra markdown report copy path")
    p.add_argument("--no-markdown-copy", action="store_true", help="Do not write the extra markdown copy")
    p.add_argument("--silent-noop", action="store_true", help="Print nothing when an apply run makes no changes")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
