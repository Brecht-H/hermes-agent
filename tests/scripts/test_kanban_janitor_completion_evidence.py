"""Tests for the kanban janitor's completion-evidence guard.

The janitor is a SECOND phantom-done path: ``explicit_completion_evidence``
emits a ``completed_at_present`` close decision and ``apply_closes`` raw-
``UPDATE``s ``status='done'`` — bypassing ``kanban_db.complete_task`` and its
``CompletionEvidenceError`` guard entirely. Before this fix the janitor closed
any non-terminal task that merely had ``completed_at`` stamped (e.g. by a brief
worker claim), rubber-stamping it ``done`` with a tautological placeholder as
its only "evidence" (live proof: ``t_5dbfc384``).

These tests pin the invariant: a bare ``completed_at`` timestamp is NOT
evidence — a non-empty ``result`` or a non-empty comment is required.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path


def _load_janitor():
    """Import scripts/kanban_janitor.py by path (it is a loose script).

    The module is registered in ``sys.modules`` before ``exec_module`` so
    its ``@dataclass``-decorated classes can resolve their own module.
    """
    name = "_kanban_janitor_under_test"
    spec = importlib.util.spec_from_file_location(
        name,
        Path(__file__).resolve().parents[2] / "scripts" / "kanban_janitor.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


janitor = _load_janitor()


def _task(**overrides):
    """Build a janitor Task dataclass with sane defaults."""
    defaults = dict(
        id="t_deadbeef",
        title="Some task",
        body=None,
        assignee=None,
        status="in_progress",
        priority=None,
        created_by=None,
        created_at=1_000,
        started_at=None,
        completed_at=None,
        result=None,
        current_run_id=None,
        claim_lock=None,
        claim_expires=None,
        worker_pid=None,
        consecutive_failures=None,
        last_failure_error=None,
    )
    defaults.update(overrides)
    return janitor.Task(**defaults)


def _comment(body: str, task_id: str = "t_deadbeef"):
    return janitor.Comment(
        id="c1", task_id=task_id, author="researcher", body=body, created_at=2_000
    )


# --------------------------------------------------------------------------
# has_completion_evidence
# --------------------------------------------------------------------------

def test_has_completion_evidence_result_only_is_true():
    task = _task(result="Delivered the spec doc.")
    assert janitor.has_completion_evidence(task, []) is True


def test_has_completion_evidence_comment_only_is_true():
    task = _task(result=None)
    assert janitor.has_completion_evidence(task, [_comment("PR merged at abc123.")]) is True


def test_has_completion_evidence_whitespace_only_is_false():
    """A whitespace-only result + whitespace-only comment is NOT evidence."""
    task = _task(result="   \n\t  ")
    assert janitor.has_completion_evidence(task, [_comment("  \n  ")]) is False


def test_has_completion_evidence_nothing_is_false():
    task = _task(result=None)
    assert janitor.has_completion_evidence(task, []) is False


# --------------------------------------------------------------------------
# explicit_completion_evidence — the completed_at_present branch
# --------------------------------------------------------------------------

def test_completed_at_without_evidence_returns_none():
    """Bare completed_at on a non-terminal task with no result/comment must
    NOT yield a close decision — this is the phantom-done bug."""
    task = _task(status="in_progress", completed_at=5_000, result=None)
    assert janitor.explicit_completion_evidence(task, []) is None


def test_completed_at_with_result_returns_completed_at_present():
    task = _task(status="in_progress", completed_at=5_000, result="Shipped PR #999.")
    decision = janitor.explicit_completion_evidence(task, [])
    assert decision is not None
    assert decision[0] == "completed_at_present"


def test_completed_at_with_comment_returns_completed_at_present():
    task = _task(status="in_progress", completed_at=5_000, result=None)
    decision = janitor.explicit_completion_evidence(
        task, [_comment("Verified live; closing.")]
    )
    assert decision is not None
    assert decision[0] == "completed_at_present"


def test_completed_at_with_whitespace_only_evidence_returns_none():
    task = _task(status="in_progress", completed_at=5_000, result="   ")
    assert janitor.explicit_completion_evidence(task, [_comment("   ")]) is None


# --------------------------------------------------------------------------
# build_scan — phantom_completed_at anomaly category
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT,
    body TEXT,
    assignee TEXT,
    status TEXT,
    priority INTEGER,
    created_by TEXT,
    created_at INTEGER,
    started_at INTEGER,
    completed_at INTEGER,
    result TEXT,
    current_run_id TEXT,
    claim_lock TEXT,
    claim_expires INTEGER,
    worker_pid INTEGER,
    consecutive_failures INTEGER,
    last_failure_error TEXT
);
CREATE TABLE task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    author TEXT,
    body TEXT,
    created_at INTEGER
);
"""


def _make_db(tmp_path: Path, tasks: list[dict], comments: list[dict]) -> Path:
    db_path = tmp_path / "kanban.db"
    con = sqlite3.connect(str(db_path))
    con.executescript(_SCHEMA)
    for t in tasks:
        cols = ", ".join(t.keys())
        ph = ", ".join("?" for _ in t)
        con.execute(f"INSERT INTO tasks ({cols}) VALUES ({ph})", tuple(t.values()))
    for c in comments:
        cols = ", ".join(c.keys())
        ph = ", ".join("?" for _ in c)
        con.execute(f"INSERT INTO task_comments ({cols}) VALUES ({ph})", tuple(c.values()))
    con.commit()
    con.close()
    return db_path


def test_build_scan_flags_phantom_completed_at_and_skips_close(tmp_path):
    """A bare-completed_at no-evidence task appears in phantom_completed_at
    and NOT in close_decisions."""
    db_path = _make_db(
        tmp_path,
        tasks=[
            {
                "id": "t_phantom01",
                "title": "Bare completed_at, no evidence",
                "status": "in_progress",
                "created_at": 1_000,
                "completed_at": 2_000,
                "result": None,
            }
        ],
        comments=[],
    )
    scan = janitor.build_scan(db_path)

    phantom_ids = {p["task_id"] for p in scan["phantom_completed_at"]}
    close_ids = {d["task_id"] for d in scan["close_decisions"]}

    assert "t_phantom01" in phantom_ids
    assert "t_phantom01" not in close_ids

    entry = next(p for p in scan["phantom_completed_at"] if p["task_id"] == "t_phantom01")
    assert entry["status"] == "in_progress"
    assert entry["completed_at"] == 2_000


def test_build_scan_real_evidence_task_closes_not_flagged(tmp_path):
    """A completed_at task WITH a real result is a legitimate close — it
    appears in close_decisions and NOT in phantom_completed_at."""
    db_path = _make_db(
        tmp_path,
        tasks=[
            {
                "id": "t_realdone1",
                "title": "Completed with evidence",
                "status": "in_progress",
                "created_at": 1_000,
                "completed_at": 2_000,
                "result": "Delivered: shipped PR #1234, all tests green.",
            }
        ],
        comments=[],
    )
    scan = janitor.build_scan(db_path)

    phantom_ids = {p["task_id"] for p in scan["phantom_completed_at"]}
    close_ids = {d["task_id"] for d in scan["close_decisions"]}

    assert "t_realdone1" in close_ids
    assert "t_realdone1" not in phantom_ids
