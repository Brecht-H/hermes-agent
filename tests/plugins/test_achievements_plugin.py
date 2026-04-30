"""Tests for the bundled hermes-achievements dashboard plugin.

These target the two behaviors that matter for official integration:

* The 200-session scan cap is removed — the plugin now walks the entire
  session history by default. Lifetime badges (tens of thousands of
  tool calls) were unreachable before this fix on long-running installs.
* First-ever scans run in a background thread so the dashboard request
  path never blocks, even on 8000+ session databases where a cold scan
  takes minutes.

The upstream repo ships its own unittest suite under
``plugins/hermes-achievements/tests/`` covering the achievement engine
internals (tier math, secret-state handling, catalog invariants). These
tests live at the hermes-agent level and focus on the integration
contract: the plugin scans ALL of your sessions, not the first 200.
"""
from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

PLUGIN_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "hermes-achievements"
    / "dashboard"
    / "plugin_api.py"
)


@pytest.fixture
def plugin_api(tmp_path, monkeypatch):
    """Load plugin_api with isolated ~/.hermes so state/snapshot files don't collide.

    We load the module fresh per test because the plugin keeps module-level
    caches (``_SNAPSHOT_CACHE``, ``_SCAN_STATUS``, background thread handle).
    Reloading gives each test a clean world.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    spec = importlib.util.spec_from_file_location(
        f"plugin_api_test_{id(tmp_path)}", PLUGIN_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module


class _FakeSessionDB:
    """Stand-in for hermes_state.SessionDB that records scan calls."""

    def __init__(self, session_count: int):
        self.session_count = session_count
        self.last_limit: Optional[int] = None
        self.last_include_children: Optional[bool] = None
        self.list_calls = 0
        self.messages_calls = 0

    def list_sessions_rich(
        self,
        source: Optional[str] = None,
        exclude_sources: Optional[List[str]] = None,
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
        project_compression_tips: bool = True,
    ) -> List[Dict[str, Any]]:
        self.last_limit = limit
        self.last_include_children = include_children
        self.list_calls += 1
        # SQLite semantics: LIMIT -1 = unlimited. Honor that here.
        effective = self.session_count if limit == -1 else min(self.session_count, limit)
        now = int(time.time())
        return [
            {
                "id": f"sess-{i}",
                "title": f"Session {i}",
                "preview": f"preview {i}",
                "started_at": now - (self.session_count - i) * 60,
                "last_active": now - (self.session_count - i) * 60 + 30,
                "source": "cli",
                "model": "test-model",
            }
            for i in range(effective)
        ]

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        self.messages_calls += 1
        return [
            {"role": "user", "content": f"ask {session_id}"},
            {
                "role": "assistant",
                "tool_calls": [{"function": {"name": "terminal"}}],
            },
            {"role": "tool", "tool_name": "terminal", "content": "ok"},
        ]

    def close(self) -> None:
        pass


def _install_fake_session_db(plugin_api, fake_db):
    """Inject a fake SessionDB so ``scan_sessions`` finds it via its local import."""
    fake_module = type(sys)("hermes_state")
    fake_module.SessionDB = lambda: fake_db
    sys.modules["hermes_state"] = fake_module


def test_scan_sessions_default_scans_all_history_not_first_200(plugin_api):
    """Bug regression: ``scan_sessions()`` used to cap at limit=200.

    A user with 8000+ sessions would only see ~2% of their history in
    achievement totals, making lifetime badges unreachable. The default
    now passes ``LIMIT -1`` (SQLite "unlimited") to ``list_sessions_rich``.
    """
    fake_db = _FakeSessionDB(session_count=500)  # > old 200 cap
    _install_fake_session_db(plugin_api, fake_db)

    result = plugin_api.scan_sessions()

    assert fake_db.last_limit == -1, (
        "scan_sessions() must pass LIMIT=-1 (unlimited) to list_sessions_rich "
        f"by default, got {fake_db.last_limit}"
    )
    assert fake_db.last_include_children is True, (
        "scan_sessions() must include subagent/compression child sessions so "
        "tool calls made in delegated agents still count toward achievements"
    )
    assert len(result["sessions"]) == 500
    assert result["scan_meta"]["sessions_total"] == 500


def test_scan_sessions_explicit_positive_limit_is_honored(plugin_api):
    """Callers can still pass a small limit for smoke tests."""
    fake_db = _FakeSessionDB(session_count=500)
    _install_fake_session_db(plugin_api, fake_db)

    result = plugin_api.scan_sessions(limit=10)

    assert fake_db.last_limit == 10
    assert len(result["sessions"]) == 10


def test_scan_sessions_zero_or_negative_limit_means_unlimited(plugin_api):
    """``limit=0`` and ``limit=-1`` both map to the unlimited path."""
    fake_db = _FakeSessionDB(session_count=300)
    _install_fake_session_db(plugin_api, fake_db)

    plugin_api.scan_sessions(limit=0)
    assert fake_db.last_limit == -1

    plugin_api.scan_sessions(limit=-1)
    assert fake_db.last_limit == -1


def test_evaluate_all_first_run_returns_pending_and_starts_background_scan(plugin_api):
    """First-ever evaluate_all with no cache returns a pending placeholder
    immediately and kicks off a background scan thread. Cold scans on
    large DBs take minutes — blocking the dashboard request path is not
    acceptable.
    """
    fake_db = _FakeSessionDB(session_count=50)
    _install_fake_session_db(plugin_api, fake_db)

    # Wrap _run_scan_and_update_cache so we can release it on demand,
    # simulating a slow cold scan without actually waiting.
    scan_started = threading.Event()
    allow_scan_finish = threading.Event()
    original_run = plugin_api._run_scan_and_update_cache

    def gated_run():
        scan_started.set()
        allow_scan_finish.wait(timeout=5)
        original_run()

    plugin_api._run_scan_and_update_cache = gated_run

    t0 = time.time()
    result = plugin_api.evaluate_all()
    elapsed = time.time() - t0

    # Immediate return — should not block waiting for the scan.
    assert elapsed < 1.0, f"evaluate_all blocked for {elapsed:.2f}s on first run"
    assert result["scan_meta"]["mode"] == "pending"
    assert result["unlocked_count"] == 0
    # Catalog still rendered so UI has something to draw.
    assert result["total_count"] >= 60

    # Background scan is running.
    assert scan_started.wait(timeout=2), "background scan did not start"

    # Let the scan complete, then a second call returns real data.
    allow_scan_finish.set()
    # Wait for thread to finish.
    thread = plugin_api._BACKGROUND_SCAN_THREAD
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()

    second = plugin_api.evaluate_all()
    assert second["scan_meta"]["mode"] != "pending"
    assert second["scan_meta"].get("sessions_total") == 50


def test_evaluate_all_stale_cache_serves_stale_and_refreshes_in_background(plugin_api):
    """When the snapshot is on-disk but older than TTL, evaluate_all returns
    the stale data immediately and kicks a background refresh. Users don't
    stare at a loading spinner every time TTL expires.
    """
    fake_db = _FakeSessionDB(session_count=10)
    _install_fake_session_db(plugin_api, fake_db)

    # Seed a stale snapshot on disk.
    stale_generated_at = int(time.time()) - plugin_api.SNAPSHOT_TTL_SECONDS - 60
    stale_payload = {
        "achievements": [],
        "sessions": [],
        "aggregate": {},
        "scan_meta": {"mode": "full", "sessions_total": 1, "sessions_rescanned": 1, "sessions_reused": 0},
        "error": None,
        "unlocked_count": 0,
        "discovered_count": 0,
        "secret_count": 0,
        "total_count": 0,
        "generated_at": stale_generated_at,
    }
    plugin_api.save_snapshot(stale_payload)

    t0 = time.time()
    result = plugin_api.evaluate_all()
    elapsed = time.time() - t0

    assert elapsed < 1.0, f"evaluate_all blocked for {elapsed:.2f}s serving stale data"
    assert result["generated_at"] == stale_generated_at

    # Background scan should be running or have completed.
    thread = plugin_api._BACKGROUND_SCAN_THREAD
    assert thread is not None
    thread.join(timeout=5)

    fresh = plugin_api.evaluate_all()
    assert fresh["generated_at"] >= stale_generated_at


def test_evaluate_all_force_runs_synchronously(plugin_api):
    """Manual /rescan (force=True) blocks the caller — users clicking
    the rescan button expect up-to-date data when the call returns.
    """
    fake_db = _FakeSessionDB(session_count=25)
    _install_fake_session_db(plugin_api, fake_db)

    result = plugin_api.evaluate_all(force=True)

    # Synchronous — snapshot is fresh on return.
    assert result["scan_meta"].get("sessions_total") == 25
    assert result["scan_meta"]["mode"] in ("full", "incremental")


def test_start_background_scan_is_idempotent_while_running(plugin_api):
    """Multiple concurrent dashboard requests must not spawn duplicate scans."""
    fake_db = _FakeSessionDB(session_count=5)
    _install_fake_session_db(plugin_api, fake_db)

    release = threading.Event()
    original_run = plugin_api._run_scan_and_update_cache

    def gated_run():
        release.wait(timeout=5)
        original_run()

    plugin_api._run_scan_and_update_cache = gated_run

    plugin_api._start_background_scan()
    first_thread = plugin_api._BACKGROUND_SCAN_THREAD
    assert first_thread is not None and first_thread.is_alive()

    plugin_api._start_background_scan()
    plugin_api._start_background_scan()

    assert plugin_api._BACKGROUND_SCAN_THREAD is first_thread

    release.set()
    first_thread.join(timeout=5)
