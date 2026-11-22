"""Reconcile: bring a stale 'running' entry in line with reality via the .rc
sidecar and the /proc liveness + starttime guard."""

from __future__ import annotations

from proc_lib import core

from .conftest import ns, start, wait_terminal


def test_running_to_exited_via_rc():
    start("e", "bash", "-c", "exit 5")
    m = wait_terminal("e")
    assert m["status"] == "exited"
    assert m["exit_code"] == 5
    assert m["end_ts"] is not None


def test_timeout_status_on_rc_124():
    # OS-enforced 1s cap on a 5s sleep: timeout fires -> rc 124 -> status timeout.
    start("to", "bash", "-c", "sleep 5", timeout=1)
    m = wait_terminal("to", deadline_s=8)
    assert m["status"] == "timeout"
    assert m["exit_code"] == 124


def test_rc_124_without_timeout_is_plain_exited():
    # A job that genuinely exits 124 but had NO --timeout must not be mislabeled.
    start("x124", "bash", "-c", "exit 124")
    m = wait_terminal("x124")
    assert m["status"] == "exited"
    assert m["exit_code"] == 124


def test_missing_rc_still_terminalizes(monkeypatch):
    start("nr", "bash", "-c", "exit 0")
    wait_terminal("nr")
    # Simulate the SIGKILL-beat-the-wrapper case: no .rc sidecar, process gone.
    m = core._load("nr")
    (core.BASE / "nr.rc").unlink(missing_ok=True)
    m["status"] = core.RUNNING
    m["exit_code"] = None
    m["end_ts"] = None
    core._save(m)
    core._reconcile(m)
    assert m["status"] == "exited"  # known-ended, exit_code unknown
    assert m["exit_code"] is None


def test_reconcile_leaves_terminal_untouched():
    start("t", "bash", "-c", "exit 9")
    m = wait_terminal("t")
    snapshot = dict(m)
    core._reconcile(m)
    assert m == snapshot


def test_alive_job_stays_running():
    start("run", "bash", "-c", "sleep 3")
    m = core._with_lock(lambda: core._reconcile_and_save("run"))
    assert m["status"] == "running"
    core.cmd_kill(ns(name="run", force=True))
