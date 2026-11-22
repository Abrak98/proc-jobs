"""Scheduled start: `start --at` registers a record, the launcher pass starts it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime

import pytest

from proc_lib import cli, core

from .conftest import ns, start, wait_terminal

T0 = 1_800_000_000.0  # fixed fake "now"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


@pytest.fixture
def clock(monkeypatch):
    now = [T0]
    monkeypatch.setattr(core, "_now", lambda: now[0])
    return now


def launch_pass() -> int:
    return core.cmd_launch_due(ns())


def test_start_at_registers_scheduled_entry_and_spawns_nothing(clock):
    assert start("s1", "bash", "-c", "echo hi", at=iso(T0 + 3600)) == 0
    m = core._load("s1")
    assert m["status"] == core.SCHEDULED
    assert m["pid"] is None
    assert m["scheduled_ts"] == T0 + 3600
    assert not core._log_path("s1").exists()


def test_launch_pass_before_time_starts_nothing(clock):
    start("s2", "true", at=iso(T0 + 60))
    clock[0] = T0 + 59
    launch_pass()
    assert core._load("s2")["status"] == core.SCHEDULED


def test_launch_pass_at_time_starts_job_then_it_is_a_normal_job(clock):
    start("s3", "bash", "-c", "echo ran", at=iso(T0 + 60))
    clock[0] = T0 + 60
    launch_pass()
    done = wait_terminal("s3")
    assert done["status"] == core.EXITED and done["exit_code"] == 0
    assert "ran" in core._log_path("s3").read_text()
    assert done["scheduled_for"] == iso(T0 + 60)


def test_exactly_once_under_two_concurrent_passes(clock, tmp_path):
    marker = tmp_path / "runs"
    start("once", "bash", "-c", f"echo x >> {marker}", at=iso(T0))
    clock[0] = T0 + 1
    barrier = threading.Barrier(2)

    def one_pass():
        barrier.wait()
        launch_pass()

    threads = [threading.Thread(target=one_pass) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wait_terminal("once")
    assert marker.read_text().splitlines() == ["x"]


def test_scheduled_entry_survives_a_fresh_process(clock, tmp_path):
    start("persist", "bash", "-c", "echo up", at=iso(T0 + 600))
    code = (
        "import sys; from proc_lib import core; "
        f"core._now = lambda: {T0 + 601}; sys.exit(core.cmd_launch_due(None))"
    )
    env = os.environ | {"PROC_HOME": str(core.BASE)}
    r = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "launched 'persist'" in r.stdout
    assert wait_terminal("persist")["exit_code"] == 0


def test_naive_or_malformed_time_is_refused(clock, capsys):
    assert start("bad1", "true", at="2026-10-23T13:19:00") == core.EXIT_USAGE
    assert start("bad2", "true", at="tomorrow") == core.EXIT_USAGE
    assert core._load("bad1") is None and core._load("bad2") is None


def test_list_and_status_show_scheduled_start(clock, capsys):
    start("shown", "true", at=iso(T0 + 3 * 3600))
    assert core.cmd_list(ns(json=False)) == 0
    out = capsys.readouterr().out
    assert "scheduled" in out and "START" in out
    assert datetime.fromtimestamp(T0 + 3 * 3600, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ") in out
    core.cmd_status(ns(name="shown", json=False))
    assert capsys.readouterr().out.splitlines()[0].startswith("shown: scheduled for ")
    core.cmd_status(ns(name="shown", json=True))
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "scheduled" and data["alive"] is False


def test_kill_cancels_a_scheduled_job(clock):
    start("cancel", "true", at=iso(T0 + 60))
    assert core.cmd_kill(ns(name="cancel", force=False)) == 0
    assert core._load("cancel")["status"] == core.KILLED
    assert not core._schedenv_path("cancel").exists()
    clock[0] = T0 + 120
    launch_pass()
    assert core._load("cancel")["status"] == core.KILLED


def test_wait_on_scheduled_job_waits_through_start_to_end(clock, monkeypatch):
    start("w", "bash", "-c", "exit 0", at=iso(T0 + 30))

    real_sleep = time.sleep

    def advance_and_launch(secs):
        # The fake clock moves only until the job is launched. After that the
        # job is a real process that needs real time to exit and be reaped, so
        # a further fake advance would race it against the fake deadline.
        if core._load("w")["status"] == core.SCHEDULED:
            clock[0] += 30
            launch_pass()
        else:
            real_sleep(min(secs, 0.05))

    monkeypatch.setattr(core.time, "sleep", advance_and_launch)
    assert core.cmd_wait(ns(name="w", timeout=3600)) == 0


def test_start_at_on_existing_scheduled_name_is_refused(clock):
    start("dup", "true", at=iso(T0 + 60))
    assert start("dup", "true", at=iso(T0 + 90)) == core.EXIT_USAGE
    assert core._load("dup")["scheduled_ts"] == T0 + 60


def test_reschedule_moves_a_scheduled_job(clock):
    start("mv", "true", at=iso(T0 + 60))
    assert core.cmd_reschedule(ns(name="mv", at=iso(T0 + 7200))) == 0
    assert core._load("mv")["scheduled_ts"] == T0 + 7200


def test_reschedule_unknown_and_terminal_jobs(clock):
    assert core.cmd_reschedule(ns(name="ghost", at=iso(T0))) == core.EXIT_NOTFOUND
    start("fin", "true")
    wait_terminal("fin")
    assert core.cmd_reschedule(ns(name="fin", at=iso(T0))) == core.EXIT_USAGE


def test_running_job_reschedules_itself_via_start_at(clock, monkeypatch, tmp_path):
    start("loop", "bash", "-c", "sleep 30")
    # the job's own view: PROC_JOB_NAME is set in its environment
    monkeypatch.setenv(core.ENV_JOB_NAME, "loop")
    assert start("loop", "echo", "next", at=iso(T0 + 600), env=["K=v"]) == 0
    assert core._load("loop")["status"] == core.RUNNING
    assert core._load("loop")["next_run"]["at_ts"] == T0 + 600
    # the run ends -> entry becomes scheduled with the new command
    pid_sid = core._load("loop")["pid"]
    core._signal_session(pid_sid, 15)
    deadline_state = wait_scheduled("loop")
    assert deadline_state["cmd"] == ["echo", "next"]
    assert deadline_state["scheduled_ts"] == T0 + 600
    clock[0] = T0 + 601
    launch_pass()
    assert wait_terminal("loop")["exit_code"] == 0
    assert "next" in core._log_path("loop").read_text()


def wait_scheduled(name, deadline_s=10.0):
    end = time.time() + deadline_s
    while time.time() < end:
        m = core._with_lock(lambda: core._reconcile_and_save(name))
        if m["status"] == core.SCHEDULED:
            return m
        time.sleep(0.05)
    raise AssertionError(f"{name} never became scheduled: {core._load(name)}")


def test_reschedule_command_from_inside_reuses_command_and_env(clock, monkeypatch):
    start("again", "bash", "-c", "sleep 30", env=["TOKEN_X=secret"])
    monkeypatch.setenv(core.ENV_JOB_NAME, "again")
    monkeypatch.setenv("TOKEN_X", "secret")
    assert core.cmd_reschedule(ns(name="again", at=iso(T0 + 900))) == 0
    m = core._load("again")
    assert m["next_run"]["spec"]["cmd"] == ["bash", "-c", "sleep 30"]
    assert m["next_run"]["spec"]["env"] == {"TOKEN_X": "***"}
    assert json.loads(core._schedenv_path("again").read_text())["TOKEN_X"] == "secret"
    assert oct(core._schedenv_path("again").stat().st_mode & 0o777) == "0o600"
    core.cmd_kill(ns(name="again", force=True))
    assert "next_run" not in core._load("again")
    assert not core._schedenv_path("again").exists()


def test_running_job_cannot_be_rescheduled_by_an_outsider(clock, monkeypatch):
    start("mine", "bash", "-c", "sleep 30")
    monkeypatch.delenv(core.ENV_JOB_NAME, raising=False)
    assert core.cmd_reschedule(ns(name="mine", at=iso(T0 + 60))) == core.EXIT_USAGE
    assert start("mine", "true", at=iso(T0 + 60)) == core.EXIT_USAGE
    core.cmd_kill(ns(name="mine", force=True))


def test_job_sees_its_own_name(clock, tmp_path):
    out = tmp_path / "name"
    start("who", "bash", "-c", f"echo ${core.ENV_JOB_NAME} > {out}")
    wait_terminal("who")
    assert out.read_text().strip() == "who"


def test_scheduled_env_reaches_the_job_unmasked_but_registry_masks_it(clock, tmp_path):
    out = tmp_path / "v"
    start("envj", "bash", "-c", f"echo $API_TOKEN > {out}", at=iso(T0), env=["API_TOKEN=s3"])
    assert core._load("envj")["env"] == {"API_TOKEN": "***"}
    assert "s3" not in core._meta_path("envj").read_text()
    clock[0] = T0 + 1
    launch_pass()
    wait_terminal("envj")
    assert out.read_text().strip() == "s3"
    assert not core._schedenv_path("envj").exists()


def test_failed_scheduled_launch_is_recorded_not_retried(clock):
    start("gone", "true", cwd="/tmp", at=iso(T0))
    m = core._load("gone")
    m["cwd"] = "/nonexistent-dir-for-test"
    core._save(m)
    clock[0] = T0 + 1
    launch_pass()
    m = core._load("gone")
    assert m["status"] == core.EXITED and m["launch_error"]
    assert "scheduled start failed" in core._log_path("gone").read_text()


def test_heavy_scheduled_job_defers_while_another_heavy_runs(clock, monkeypatch):
    from proc_lib import heavy_job_lock

    holder = heavy_job_lock.LockHolder(pid=1, job_name="other", started_iso="", argv="")

    def busy(name, *_a, **_k):
        raise heavy_job_lock.HeavyJobBusy(holder, name)

    core.cmd_start(
        ns(name="hv", timeout=None, cwd="/tmp", env=[], at=iso(T0), heavy=True, cmd=["--", "true"])
    )
    monkeypatch.setattr(heavy_job_lock, "check_free", busy)
    clock[0] = T0 + 1
    launch_pass()
    assert core._load("hv")["status"] == core.SCHEDULED


def test_cli_parses_start_at_reschedule_launch_due_and_launcher():
    parser = cli.build_parser()
    a = parser.parse_args(["start", "--name", "x", "--at", "2026-10-23T13:19:00Z", "--", "c"])
    assert a.at == "2026-10-23T13:19:00Z"
    assert parser.parse_args(["reschedule", "x", "--at", "2026-10-23T13:19:00Z"]).func
    assert parser.parse_args(["launch-due"]).func is core.cmd_launch_due
    assert parser.parse_args(["launcher", "status"]).action == "status"


# --- owner ---------------------------------------------------------------- #


def test_start_records_owner_from_env(monkeypatch):
    monkeypatch.setenv(core.ENV_OWNER_SESSION, "sess-1")
    monkeypatch.setenv(core.ENV_OWNER_PROJECT, "demo")
    assert start("own1", "true") == 0
    meta = wait_terminal("own1")
    assert meta["owner"] == {"session": "sess-1", "project": "demo"}
    assert core._status_json(meta)["owner"] == meta["owner"]
    assert "owner: session=sess-1 project=demo" in "\n".join(core._status_lines(meta))


def test_start_without_owner_env_has_no_owner_key(monkeypatch):
    assert start("own2", "true") == 0
    assert "owner" not in wait_terminal("own2")


def test_start_with_only_session_records_only_session(monkeypatch):
    monkeypatch.setenv(core.ENV_OWNER_SESSION, "sess-2")
    monkeypatch.setenv(core.ENV_OWNER_PROJECT, "")
    assert start("own3", "true") == 0
    assert wait_terminal("own3")["owner"] == {"session": "sess-2"}


def test_scheduled_job_keeps_the_owner_of_the_scheduling_session(clock, monkeypatch):
    monkeypatch.setenv(core.ENV_OWNER_SESSION, "sess-sched")
    monkeypatch.setenv(core.ENV_OWNER_PROJECT, "proj-a")
    assert start("own4", "true", at=iso(T0 + 60)) == 0
    assert core._load("own4")["owner"] == {"session": "sess-sched", "project": "proj-a"}
    # the launcher runs from another environment (here: another session)
    monkeypatch.setenv(core.ENV_OWNER_SESSION, "launcher-env")
    monkeypatch.delenv(core.ENV_OWNER_PROJECT)
    clock[0] = T0 + 61
    assert launch_pass() == 0
    assert wait_terminal("own4")["owner"] == {"session": "sess-sched", "project": "proj-a"}


def test_scheduled_job_without_owner_env_starts_without_owner(clock, monkeypatch):
    assert start("own5", "true", at=iso(T0 + 60)) == 0
    assert "owner" not in core._load("own5")
    clock[0] = T0 + 61
    assert launch_pass() == 0
    assert "owner" not in wait_terminal("own5")
