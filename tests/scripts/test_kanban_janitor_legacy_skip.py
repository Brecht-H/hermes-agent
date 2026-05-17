"""Tests for the kanban janitor's tolerance of a tableless legacy DB.

The default ``--legacy-db`` (``~/.hermes/profiles/daily/kanban.db``) is a
per-profile DB that has NO ``tasks`` table. Before this fix ``build_scan`` ran
``SELECT * FROM tasks`` unconditionally, so the whole janitor crashed with
``sqlite3.OperationalError: no such table: tasks`` on its DEFAULT arguments —
any unattended (cron/triager) run produced no report at all (live proof:
day-30 verification 2026-05-17).

These tests pin the invariant: a DB missing ``tasks`` / ``task_comments`` is
scanned as empty, never raised on.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path


def _load_janitor():
    """Import scripts/kanban_janitor.py by path (it is a loose script)."""
    name = "_kanban_janitor_legacy_under_test"
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


def _tableless_db(path: Path) -> Path:
    """A real DB file with a connection but no kanban tables."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE unrelated (x INTEGER)")
    con.commit()
    con.close()
    return path


def test_table_exists_helper(tmp_path):
    db = _tableless_db(tmp_path / "k.db")
    con = sqlite3.connect(db)
    try:
        assert janitor._table_exists(con, "unrelated") is True
        assert janitor._table_exists(con, "tasks") is False
        assert janitor._table_exists(con, "task_comments") is False
    finally:
        con.close()


def test_fetch_tasks_missing_table_returns_empty(tmp_path):
    db = _tableless_db(tmp_path / "k.db")
    con = janitor.connect(db)
    try:
        assert janitor.fetch_tasks(con) == []
    finally:
        con.close()


def test_fetch_comments_missing_table_returns_empty(tmp_path):
    db = _tableless_db(tmp_path / "k.db")
    con = janitor.connect(db)
    try:
        assert janitor.fetch_comments(con) == {}
    finally:
        con.close()


def test_build_scan_on_tableless_legacy_db_does_not_crash(tmp_path):
    """The actual bug: build_scan(legacy=True) on a tasks-less DB must not raise."""
    db = _tableless_db(tmp_path / "legacy.db")
    scan = janitor.build_scan(db, legacy=True)
    assert scan["legacy"] is True
    assert scan["task_count"] == 0
    assert scan["status_counts"] == {}
    assert scan["close_decisions"] == []
    assert scan["phantom_completed_at"] == []


def test_build_scan_still_works_on_a_real_tasks_db(tmp_path):
    """Regression guard: the table-exists check must not break a normal DB."""
    db = tmp_path / "real.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, created_at INTEGER)"
    )
    con.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES "
        "('t_aaaaaaaa', 'Real task', 'in_progress', 1000)"
    )
    con.commit()
    con.close()
    scan = janitor.build_scan(db, legacy=False)
    assert scan["task_count"] == 1
    assert scan["status_counts"] == {"in_progress": 1}
