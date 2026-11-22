# spec: specs/proc_lib/heavy_job_lock.spec.md
"""Tests for the heavy-job lock; BASE is isolated by the autouse conftest fixture."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from proc_lib import core
from proc_lib import heavy_job_lock as hjl

from .conftest import ns


def _lock(name):
    return core.BASE / "heavy_job" / f"{name}.lock"


@pytest.fixture
def live_child():
    p = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
    yield p
    p.kill()
    p.wait()


@pytest.fixture
def dead_pid():
    p = subprocess.Popen([sys.executable, "-I", "-c", "pass"])
    p.wait()
    return p.pid


def test_check_free_without_locks_writes_nothing():
    hjl.check_free("a")
    assert not (core.BASE / "heavy_job").exists() or not list(_lock("a").parent.iterdir())


def test_claim_file_format(live_child):
    path = hjl.claim("a", live_child.pid, "x y")
    pid, name, started, argv = path.read_text().split("\n")
    assert (int(pid), name, argv) == (live_child.pid, "a", "x y")
    assert started.endswith("+00:00")
    assert path == _lock("a")


def test_live_holder_blocks_check_free(live_child):
    hjl.claim("a", live_child.pid, "sleep 60")
    with pytest.raises(hjl.HeavyJobBusy) as ei:
        hjl.check_free("b")
    msg = str(ei.value)
    assert str(live_child.pid) in msg and "sleep 60" in msg and "--force-concurrent" in msg
    assert ei.value.holder.job_name == "a"
    assert ei.value.attempted_job == "b"
    hjl.check_free("b", allow_concurrent=True)


def test_acquire_blocked_by_live_holder(live_child):
    hjl.claim("a", live_child.pid, "x")
    with pytest.raises(hjl.HeavyJobBusy), hjl.acquire("b"):
        pass
    assert not _lock("b").exists()


def test_dead_pid_lock_is_reclaimed(dead_pid):
    hjl.claim("a", dead_pid, "x")
    hjl.check_free("b")
    assert not _lock("a").exists()
    assert hjl.current_holders() == []


def test_claim_replaces_stale_leftover(dead_pid):
    hjl.claim("a", dead_pid, "old")
    hjl.claim("a", os.getpid(), "new")
    assert [h.argv for h in hjl.current_holders()] == ["new"]


def test_claim_refuses_live_same_name(live_child):
    hjl.claim("a", live_child.pid, "x")
    with pytest.raises(hjl.HeavyJobBusy):
        hjl.claim("a", os.getpid(), "y")


def test_old_lock_is_stale_even_with_live_pid(live_child, monkeypatch):
    hjl.claim("a", live_child.pid, "x")
    monkeypatch.setattr(hjl, "STALE_AFTER_SEC", -1)
    hjl.check_free("b")
    assert not _lock("a").exists()


def test_acquire_claims_and_releases():
    with hjl.acquire("a") as path:
        assert path == _lock("a")
        assert int(path.read_text().split("\n")[0]) == os.getpid()
    assert not _lock("a").exists()


def test_acquire_reentry_is_passthrough():
    with hjl.acquire("a"), hjl.acquire("a") as inner:
        assert inner == _lock("a")
        text = _lock("a").read_text()
    assert not _lock("a").exists()
    assert text.startswith(f"{os.getpid()}\n")


def test_acquire_releases_on_exception_and_keyboard_interrupt():
    for exc in (ValueError, KeyboardInterrupt):
        with pytest.raises(exc), hjl.acquire("a"):
            raise exc
        assert not _lock("a").exists()


def test_acquire_does_not_remove_lock_taken_over_by_other_pid(live_child):
    with hjl.acquire("a"):
        _lock("a").write_text(f"{live_child.pid}\na\nnow\nother")
    assert _lock("a").exists()


def test_acquire_allow_concurrent_runs_beside_holder(live_child):
    hjl.claim("a", live_child.pid, "x")
    with hjl.acquire("b", allow_concurrent=True):
        assert _lock("b").exists()
    assert _lock("a").exists()
    assert not _lock("b").exists()


def test_current_holders_lists_live_only(live_child, dead_pid):
    hjl.claim("a", live_child.pid, "x")
    hjl.claim("b", dead_pid, "y")
    assert [(h.pid, h.job_name) for h in hjl.current_holders()] == [(live_child.pid, "a")]


def test_start_heavy_refuses_when_busy(live_child, capsys):
    hjl.claim("a", live_child.pid, "x")
    rc = core.cmd_start(
        ns(
            name="b",
            timeout=None,
            cwd="/tmp",
            env=[],
            at=None,
            heavy=True,
            force_concurrent=False,
            cmd=["--", "true"],
        )
    )
    assert rc == core.EXIT_USAGE
