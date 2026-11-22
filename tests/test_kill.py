"""kill / rm — termination, the SIGTERM->SIGKILL escalation, and the
tech-debt regression: a cleanly-exited stale job must reconcile to 'exited'
with its real exit code, never be mislabeled 'lost' (which discarded .rc)."""

from __future__ import annotations

import time

from proc_lib import core

from .conftest import ns, start, wait_terminal


def _wait_process_gone_without_reconcile(name, deadline_s=5.0):
    """Wait until the launched process has actually exited AND written its .rc,
    WITHOUT calling reconcile — so the registry keeps saying 'running'. This
    reproduces the stale-registry condition the lost-bug needed."""
    m = core._load(name)
    rc_path = core.BASE / f"{name}.rc"
    end = time.time() + deadline_s
    while time.time() < end:
        gone = not core._alive(m.get("pid"), m.get("pid_starttime"))
        if gone and rc_path.exists() and rc_path.read_text().strip():
            return
        time.sleep(0.03)
    raise AssertionError(f"{name} did not exit+write rc within {deadline_s}s")


def test_kill_of_cleanly_exited_stale_job_reconciles_not_lost(capsys):
    """THE regression. Job exits 7; registry still says 'running' (nothing
    reconciled). `proc kill` must finalize it as exited/7 from .rc, not 'lost'."""
    start("stale", "bash", "-c", "exit 7")
    _wait_process_gone_without_reconcile("stale")
    # Precondition: registry is still stale 'running'.
    assert core._load("stale")["status"] == "running"

    rc = core.cmd_kill(ns(name="stale", force=False))
    assert rc == 0
    m = core._load("stale")
    assert m["status"] == "exited", f"expected exited, got {m['status']}"
    assert m["status"] != "lost"
    assert m["exit_code"] == 7  # real code recovered from the .rc sidecar
    assert "already exited" in capsys.readouterr().out


def test_kill_already_terminal_is_noop(capsys):
    start("done", "bash", "-c", "exit 0")
    wait_terminal("done")
    rc = core.cmd_kill(ns(name="done", force=False))
    assert rc == 0
    assert "already" in capsys.readouterr().out


def test_kill_unknown_job_is_notfound():
    assert core.cmd_kill(ns(name="ghost", force=False)) == core.EXIT_NOTFOUND


def test_force_kill_running_job():
    start("fk", "bash", "-c", "sleep 30")
    rc = core.cmd_kill(ns(name="fk", force=True))
    assert rc == 0
    m = core._load("fk")
    assert m["status"] == "killed"
    assert not core._session_alive(m["pid"])


def test_sigterm_escalates_to_sigkill_on_term_ignoring_process(monkeypatch):
    """A process that traps & ignores SIGTERM must still die: SIGTERM, short
    grace, then SIGKILL of the whole session."""
    monkeypatch.setattr(core, "KILL_GRACE_SECONDS", 1.0)
    start("stubborn", "bash", "-c", "trap '' TERM; while true; do sleep 0.05; done")
    # let the trap install
    time.sleep(0.3)
    m = core._load("stubborn")
    assert core._session_alive(m["pid"])
    rc = core.cmd_kill(ns(name="stubborn", force=False))
    assert rc == 0
    assert not core._session_alive(m["pid"])  # SIGKILL escalation got it
    assert core._load("stubborn")["status"] == "killed"


def test_kill_with_surviving_member_is_not_recorded_killed(monkeypatch, capsys):
    """A member that outlives SIGTERM and SIGKILL (here: signals are swallowed)
    must not be recorded as killed; the pids are named and the exit is non-zero."""
    monkeypatch.setattr(core, "KILL_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(core, "KILL_DRAIN_POLLS", 2)
    start("immortal", "bash", "-c", "sleep 30")
    m = core._load("immortal")
    real = core._signal_session
    monkeypatch.setattr(core, "_signal_session", lambda sid, sig: 1)
    rc = core.cmd_kill(ns(name="immortal", force=False))
    assert rc == core.EXIT_KILL_SURVIVORS
    cur = core._load("immortal")
    assert cur["status"] == "running"
    assert cur["kill_survivors"]
    assert str(cur["kill_survivors"][0]) in capsys.readouterr().err
    # rm --force stops on the same code and keeps the record
    assert core.cmd_rm(ns(name="immortal", force=True)) == core.EXIT_KILL_SURVIVORS
    assert core._load("immortal") is not None
    # a real kill afterwards clears it
    monkeypatch.setattr(core, "_signal_session", real)
    assert core.cmd_kill(ns(name="immortal", force=True)) == 0
    cur = core._load("immortal")
    assert cur["status"] == "killed" and "kill_survivors" not in cur
    assert not core._session_alive(m["pid"])


def test_rm_running_job_refused():
    start("keep", "bash", "-c", "sleep 30")
    rc = core.cmd_rm(ns(name="keep", force=False))
    assert rc == core.EXIT_USAGE
    assert core._load("keep") is not None  # not removed
    core.cmd_kill(ns(name="keep", force=True))


def test_rm_removes_all_sidecars():
    start("gone", "bash", "-c", "exit 0")
    wait_terminal("gone")
    assert (core.BASE / "gone.json").exists()
    rc = core.cmd_rm(ns(name="gone", force=False))
    assert rc == 0
    assert not (core.BASE / "gone.json").exists()
    assert not (core.BASE / "gone.log").exists()
    assert not (core.BASE / "gone.rc").exists()


def test_rm_force_kills_then_removes():
    start("fr", "bash", "-c", "sleep 30")
    m = core._load("fr")
    rc = core.cmd_rm(ns(name="fr", force=True))
    assert rc == 0
    assert core._load("fr") is None
    assert not core._session_alive(m["pid"])


def test_rm_unknown_job_is_notfound():
    assert core.cmd_rm(ns(name="ghost", force=False)) == core.EXIT_NOTFOUND
