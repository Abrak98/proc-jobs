"""Safety invariants: PID-reuse guard, registry integrity under concurrency,
and clean's age filter."""

from __future__ import annotations

import os
import threading
import time

from proc_lib import core

from .conftest import ns, start, wait_terminal


def test_alive_true_for_self_with_matching_starttime():
    pid = os.getpid()
    st = core._read_starttime(pid)
    assert st is not None
    assert core._alive(pid, st) is True


def test_alive_false_on_starttime_mismatch():
    """PID-reuse guard: same live PID, wrong starttime -> not our process."""
    pid = os.getpid()
    assert core._alive(pid, 999_999_999) is False


def test_alive_false_when_starttime_unknown():
    """If we never captured a starttime we cannot confirm identity -> False.
    Closes the gap where a reused PID with stored None would read as alive."""
    pid = os.getpid()
    assert core._alive(pid, None) is False


def test_alive_false_for_dead_pid():
    # 2^31-ish pid that won't exist
    assert core._alive(2_000_000_000, 123) is False
    assert core._alive(None, None) is False


def test_kill_does_not_signal_reused_pid():
    """If the recorded process is gone (starttime won't match), kill must NOT
    signal — it reconciles to terminal instead."""
    start("ph", "bash", "-c", "exit 0")
    _ = wait_terminal("ph")
    m = core._load("ph")
    # Corrupt the registry so the PID looks 'running' but starttime is bogus,
    # mimicking a recycled PID now owned by something else.
    m["status"] = core.RUNNING
    m["pid_starttime"] = 1  # guaranteed mismatch
    m["end_ts"] = None
    core._save(m)
    rc = core.cmd_kill(ns(name="ph", force=False))
    assert rc == 0
    assert core._load("ph")["status"] in core.TERMINAL
    assert core._load("ph")["status"] != "killed"  # never signaled


def test_concurrent_starts_do_not_corrupt_registry():
    """20 parallel launches must each persist a readable JSON (flock works)."""
    names = [f"c{i}" for i in range(20)]

    def launch(n):
        start(n, "bash", "-c", "exit 0")

    threads = [threading.Thread(target=launch, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    metas = core._all_metas()
    got = {m["name"] for m in metas}
    assert got == set(names)  # all present, all valid JSON (none corrupted)
    for n in names:
        wait_terminal(n)


def test_clean_age_filter():
    start("old", "bash", "-c", "exit 0")
    start("recent", "bash", "-c", "exit 0")
    wait_terminal("old")
    wait_terminal("recent")
    # backdate 'old' two days
    m = core._load("old")
    m["end_ts"] = time.time() - 2 * 86400
    core._save(m)
    rc = core.cmd_clean(ns(older_than=24))
    assert rc == 0
    assert core._load("old") is None  # removed (older than 24h)
    assert core._load("recent") is not None  # kept (fresh)


def test_clean_keeps_running_jobs():
    start("alive", "bash", "-c", "sleep 30")
    core.cmd_clean(ns(older_than=0))  # 0h cutoff: everything terminal goes
    assert core._load("alive") is not None  # running -> never cleaned
    core.cmd_kill(ns(name="alive", force=True))
