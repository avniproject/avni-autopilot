"""Unit tests for `src/web/sessions.py`.

Usage:
    pytest tests/test_web_sessions.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from web.sessions import ChatSession, SessionStore


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_store(tmp_path: Path, idle_min: int = 30, max_hours: int = 2) -> SessionStore:
    return SessionStore(root_dir=tmp_path / "sessions", idle_minutes=idle_min, max_hours=max_hours)


# ── create / get / remove ─────────────────────────────────────────────────────


def test_create_allocates_workdir_and_subdirs(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    session = store.create(org_name="srijan", username="alice", auth_token="bearer-xyz")
    assert session.org_name == "srijan"
    assert session.username == "alice"
    assert session.auth_token == "bearer-xyz"
    assert session.workdir.is_dir()
    assert (session.workdir / "input").is_dir()
    assert (session.workdir / "output").is_dir()
    assert store.get(session.session_id) is session
    assert session.session_id in store


def test_create_assigns_unique_session_ids(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    a = store.create("org", "alice", "t")
    b = store.create("org", "alice", "t")
    assert a.session_id != b.session_id


def test_remove_deletes_workdir_and_record(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    session = store.create("org", "alice", "t")
    workdir = session.workdir
    store.remove(session.session_id, reason="test")
    assert not workdir.exists()
    assert store.get(session.session_id) is None
    assert session.session_id not in store
    assert session.bus.closed


def test_remove_is_idempotent(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.remove("not-a-real-id")  # no exception


# ── touch + expiry ────────────────────────────────────────────────────────────


def test_touch_bumps_last_activity(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    session = store.create("org", "alice", "t")
    before = session.last_activity_at
    # Force a tiny delta so touch's now() is later.
    session.last_activity_at = before - timedelta(seconds=10)
    session.touch()
    assert session.last_activity_at > before - timedelta(seconds=10)


def test_expired_sessions_by_idle_threshold(tmp_path: Path) -> None:
    store = _make_store(tmp_path, idle_min=30, max_hours=2)
    fresh = store.create("org", "alice", "t")
    stale = store.create("org", "alice", "t")
    stale.last_activity_at = datetime.now(timezone.utc) - timedelta(minutes=45)
    expired = store.expired_session_ids()
    assert stale.session_id in expired
    assert fresh.session_id not in expired


def test_expired_sessions_by_absolute_threshold(tmp_path: Path) -> None:
    store = _make_store(tmp_path, idle_min=30, max_hours=2)
    active = store.create("org", "alice", "t")
    # `last_activity_at` recent but created_at past absolute window.
    active.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
    active.last_activity_at = datetime.now(timezone.utc)
    expired = store.expired_session_ids()
    assert active.session_id in expired


def test_reap_once_removes_expired(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    stale = store.create("org", "alice", "t")
    fresh = store.create("org", "alice", "t")
    stale.last_activity_at = datetime.now(timezone.utc) - timedelta(hours=1)
    store.reap_once()
    assert stale.session_id not in store
    assert fresh.session_id in store


# ── reaper task ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reaper_cancels_cleanly_and_removes_all_sessions(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    a = store.create("org", "alice", "t")
    b = store.create("org", "alice", "t")

    task = asyncio.create_task(store.run_reaper(interval_sec=10.0))
    await asyncio.sleep(0)  # let it start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Both sessions should be gone, workdirs cleaned up.
    assert len(store) == 0
    assert not a.workdir.exists()
    assert not b.workdir.exists()
