"""The live index (running and scheduled jobs, jobs per session), the routine paths that
read only it, the lock-free single-record reads, and the job cost the wrapper records."""

from __future__ import annotations

import builtins
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from proc_lib import cli, core, cost, launcher

from .conftest import ns, start, wait_terminal

T0 = 1_800_000_000.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


@pytest.fixture
def clock(monkeypatch):
    now = [T0]
    monkeypatch.setattr(core, "_now", lambda: now[0])
    return now


@pytest.fixture
def opened(monkeypatch):
    """Paths under the registry that open() is called on, in order."""
    seen: list[str] = []
    real = builtins.open

    def spy(file, *a, **k):
        if isinstance(file, str | Path) and str(file).startswith(str(core.BASE)):
            seen.append(Path(file).name)
        return real(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    return seen


def index_jobs() -> dict:
    return json.loads((core.BASE / ".live-index").read_text())["jobs"]


def finished_job(name: str) -> None:
    start(name, "true")
    wait_terminal(name)


# --- the index is kept by the write that changes a job's state -------------- #


def test_start_puts_a_running_job_in_the_index_and_finishing_takes_it_out(monkeypatch):
    monkeypatch.setenv(core.ENV_OWNER_SESSION, "sess-a")
    start("run1", "bash", "-c", "sleep 30")
    entry = index_jobs()["run1"]
    meta = core._load("run1")
    assert entry["status"] == core.RUNNING
    assert entry["pid"] == meta["pid"] and entry["st"] == meta["pid_starttime"]
    assert entry["owner"] == {"session": "sess-a"}
    core.cmd_kill(ns(name="run1", force=True))
    assert "run1" not in index_jobs()
    start("run2", "bash", "-c", "exit 0")
    wait_terminal("run2")  # the reconcile that sees the exit records it in the index
    assert "run2" not in index_jobs()


def test_scheduled_job_is_indexed_with_its_time_and_leaves_when_cancelled(clock):
    start("sch", "true", at=iso(T0 + 600))
    assert index_jobs()["sch"] == {"status": core.SCHEDULED, "scheduled_ts": T0 + 600}
    core.cmd_reschedule(ns(name="sch", at=iso(T0 + 900)))
    assert index_jobs()["sch"]["scheduled_ts"] == T0 + 900
    core.cmd_kill(ns(name="sch", force=False))
    assert "sch" not in index_jobs()


def test_rm_drops_the_index_entry():
    start("gone", "bash", "-c", "sleep 30")
    core.cmd_rm(ns(name=["gone"], force=True))
    assert "gone" not in index_jobs()


def test_a_job_going_live_is_indexed_before_its_record_and_leaving_live_after(monkeypatch):
    """Crash safety of the pair of writes: a crash between them may leave a finished job
    listed as live, never a live job the index does not know."""
    order: list[str] = []
    real_set, real_replace = core._index_set, os.replace

    def set_spy(name, entry):
        order.append(f"index:{'live' if entry else 'out'}")
        real_set(name, entry)

    def replace_spy(src, dst):
        if str(dst).endswith(".json"):
            order.append("record")
        real_replace(src, dst)

    monkeypatch.setattr(core, "_index_set", set_spy)
    monkeypatch.setattr(core.os, "replace", replace_spy)
    live = {"name": "o", "status": core.RUNNING, "pid": 1, "pid_starttime": 1}
    core._save(live)
    core._save(dict(live, status=core.EXITED))
    assert order == ["index:live", "record", "record", "index:out"]


def test_a_missing_or_corrupt_index_is_rebuilt_from_the_records(monkeypatch):
    monkeypatch.setenv(core.ENV_OWNER_SESSION, "sess-b")
    start("keep", "bash", "-c", "sleep 30")
    finished_job("done")
    idx = core.BASE / ".live-index"
    idx.unlink()
    assert core._live_index().keys() == {"keep"}
    idx.write_text("{not json")
    assert core._live_index().keys() == {"keep"}
    idx.write_text(json.dumps({"version": 999, "jobs": {"x": {}}}))
    assert core._live_index().keys() == {"keep"}
    core.cmd_kill(ns(name="keep", force=True))


def test_reindex_rebuilds_index_and_session_lists(capsys, monkeypatch):
    monkeypatch.setenv(core.ENV_OWNER_SESSION, "sess-c")
    start("r1", "true")
    wait_terminal("r1")
    (core.BASE / "sessions" / "sess-c").unlink()
    assert cli.main(["reindex"]) == 0
    assert "reindexed" in capsys.readouterr().out
    assert core._session_names("sess-c") == ["r1"]


def test_index_file_is_never_taken_for_a_record():
    start("a", "bash", "-c", "sleep 30")
    assert [p.name for p in core.BASE.glob("*.json")] == ["a.json"]
    core.cmd_kill(ns(name="a", force=True))


def test_session_list_has_every_job_the_session_started(monkeypatch):
    monkeypatch.setenv(core.ENV_OWNER_SESSION, "s/../1")
    start("one", "true")
    start("two", "bash", "-c", "sleep 30")
    monkeypatch.setenv(core.ENV_OWNER_SESSION, "other")
    start("three", "true")
    assert core._session_names("s/../1") == ["one", "two"]
    assert all("/" not in p.name for p in (core.BASE / "sessions").iterdir())
    core.cmd_kill(ns(name="two", force=True))


# --- launch-due reads only the live set --------------------------------------- #


def test_launch_due_with_nothing_due_opens_no_job_record(clock, opened):
    finished_job("ended")
    start("later", "true", at=iso(T0 + 3600))
    start("alive", "bash", "-c", "sleep 30")
    opened.clear()
    assert core.cmd_launch_due(ns()) == 0
    assert not [n for n in opened if n.endswith(".json")]
    assert ".live-index" in opened
    core.cmd_kill(ns(name="alive", force=True))


def test_launch_due_opens_only_the_due_record(clock, opened):
    finished_job("ended")
    start("later", "true", at=iso(T0 + 3600))
    start("now", "true", at=iso(T0 + 10))
    clock[0] = T0 + 11
    opened.clear()
    core.cmd_launch_due(ns())
    assert {n for n in opened if n.endswith(".json")} == {"now.json"}
    wait_terminal("now")


def test_launch_due_takes_the_lock_only_around_flips(clock, monkeypatch):
    start("l1", "true", at=iso(T0 + 3600))
    locks: list[int] = []
    real = core._with_lock
    monkeypatch.setattr(core, "_with_lock", lambda fn: (locks.append(1), real(fn))[1])
    core.cmd_launch_due(ns())
    assert locks == []  # nothing due, nothing dead: no lock at all
    start("l2", "true", at=iso(T0 + 5))
    locks.clear()
    clock[0] = T0 + 6
    core.cmd_launch_due(ns())
    assert len(locks) == 1  # one flip, one lock section
    wait_terminal("l2")


def test_launch_due_finalises_a_job_that_died_and_removes_it_from_the_index(clock):
    start("died", "bash", "-c", "exit 0")
    m = core._load("died")
    end = time.time() + 5
    while core._alive(m["pid"], m["pid_starttime"]) and time.time() < end:
        time.sleep(0.02)
    assert core._load("died")["status"] == core.RUNNING  # nobody reconciled it yet
    assert "died" in index_jobs()
    core.cmd_launch_due(ns())
    assert core._load("died")["status"] == core.EXITED
    assert "died" not in index_jobs()


def test_launch_due_rearms_a_job_that_asked_for_a_next_run_and_ended(clock, monkeypatch):
    start("loop", "bash", "-c", "sleep 30")
    monkeypatch.setenv(core.ENV_JOB_NAME, "loop")
    assert start("loop", "echo", "again", at=iso(T0 + 600)) == 0
    assert index_jobs()["loop"]["nxt"] is True
    core._signal_session(core._load("loop")["pid"], 15)
    m = core._load("loop")
    end = time.time() + 5
    while core._alive(m["pid"], m["pid_starttime"]) and time.time() < end:
        time.sleep(0.02)
    core.cmd_launch_due(ns())
    assert core._load("loop")["status"] == core.SCHEDULED
    assert index_jobs()["loop"] == {"status": core.SCHEDULED, "scheduled_ts": T0 + 600}


def test_launch_due_does_not_signal_a_reused_pid(clock, monkeypatch):
    """A running entry whose pid now belongs to another process (starttime differs) is
    finalised as ended; nothing is signalled."""
    core._save(
        {
            "name": "reused",
            "status": core.RUNNING,
            "pid": os.getpid(),
            "pid_starttime": 1,
            "log_path": str(core.BASE / "reused.log"),
        }
    )
    signals: list[tuple[int, int]] = []
    real_kill = os.kill
    monkeypatch.setattr(core.os, "kill", lambda p, s: (signals.append((p, s)), real_kill(p, s))[1])
    core.cmd_launch_due(ns())
    assert core._load("reused")["status"] == core.EXITED
    assert all(sig == 0 for _pid, sig in signals)


def test_launch_due_main_fast_path_skips_the_argument_parser(clock, monkeypatch):
    monkeypatch.setattr(cli, "build_parser", lambda: pytest.fail("parser built"))
    assert cli.main(["launch-due"]) == 0


# --- proc list ---------------------------------------------------------------- #


def names(capsys) -> list[str]:
    return [r["name"] for r in json.loads(capsys.readouterr().out)]


def test_list_defaults_to_live_jobs_and_never_opens_a_finished_record(clock, opened, capsys):
    finished_job("ended")
    start("alive", "bash", "-c", "sleep 30")
    start("sched", "true", at=iso(T0 + 3600))
    capsys.readouterr()
    opened.clear()
    assert core.cmd_list(ns(json=True)) == 0
    assert names(capsys) == ["alive", "sched"]
    assert "ended.json" not in opened
    core.cmd_kill(ns(name="alive", force=True))


def test_list_filters(clock, capsys, monkeypatch):
    monkeypatch.setenv(core.ENV_OWNER_SESSION, "sess-l")
    finished_job("ended")
    start("alive", "bash", "-c", "sleep 30")
    start("sched", "true", at=iso(T0 + 3600))
    monkeypatch.setenv(core.ENV_OWNER_SESSION, "sess-m")
    start("other", "true", at=iso(T0 + 7200))
    capsys.readouterr()
    core.cmd_list(ns(json=True, running=True))
    assert names(capsys) == ["alive"]
    core.cmd_list(ns(json=True, scheduled=True))
    assert names(capsys) == ["sched", "other"]
    core.cmd_list(ns(json=True, running=True, scheduled=True))
    assert names(capsys) == ["alive", "sched", "other"]
    core.cmd_list(ns(json=True, session="sess-l"))  # any state
    assert sorted(names(capsys)) == ["alive", "ended", "sched"]
    core.cmd_list(ns(json=True, session="sess-l", running=True))
    assert names(capsys) == ["alive"]
    core.cmd_list(ns(json=True, all=True))
    assert sorted(names(capsys)) == ["alive", "ended", "other", "sched"]
    core.cmd_list(ns(json=True, session="nobody"))
    assert names(capsys) == []
    core.cmd_kill(ns(name="alive", force=True))


def test_list_finalises_a_dead_running_job_and_hides_it(capsys):
    start("quick", "bash", "-c", "exit 0")
    m = core._load("quick")
    end = time.time() + 5
    while core._alive(m["pid"], m["pid_starttime"]) and time.time() < end:
        time.sleep(0.02)
    capsys.readouterr()
    core.cmd_list(ns(json=True))
    assert names(capsys) == []
    assert core._load("quick")["status"] == core.EXITED


def records_opened(opened) -> list[str]:
    return [n for n in opened if n.endswith(".json")]


def test_list_running_with_a_session_reads_only_the_index(clock, monkeypatch, opened, capsys):
    monkeypatch.setenv(core.ENV_OWNER_SESSION, "sess-a")
    finished_job("ended")
    start("alive", "bash", "-c", "sleep 30")
    start("sched", "true", at=iso(T0 + 3600))
    monkeypatch.setenv(core.ENV_OWNER_SESSION, "sess-b")
    start("other", "bash", "-c", "sleep 30")
    capsys.readouterr()
    for kw in (
        {"running": True},
        {"running": True, "session": "sess-a"},
        {"scheduled": True, "session": "sess-a"},
        {"running": True, "scheduled": True, "session": "sess-b"},
        {},
    ):
        opened.clear()
        assert core.cmd_list(ns(json=True, **kw)) == 0
        rows = json.loads(capsys.readouterr().out)
        assert rows
        assert records_opened(opened) == [], kw
    opened.clear()
    core.cmd_list(ns(json=True, running=True, session="sess-a"))
    assert names(capsys) == ["alive"]
    core.cmd_kill(ns(name="alive", force=True))
    core.cmd_kill(ns(name="other", force=True))


def test_list_json_carries_only_what_a_caller_filters_and_shows(clock, monkeypatch, capsys):
    monkeypatch.setenv(core.ENV_OWNER_SESSION, "sess-a")
    start("alive", "bash", "-c", "sleep 30", env=["SECRET_KEY=hunter2"])
    start("sched", "true", at=iso(T0 + 3600))
    capsys.readouterr()
    core.cmd_list(ns(json=True))
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    keys = {"name", "status", "alive", "exit_code", "pid", "pid_starttime", "owner"}
    assert set(rows["alive"]) == keys | {"start_ts", "elapsed_s"}
    assert set(rows["sched"]) == keys | {"start_ts", "elapsed_s", "scheduled_ts"}
    assert rows["alive"]["owner"] == {"session": "sess-a"}
    assert rows["alive"]["pid"] == core._load("alive")["pid"]
    assert rows["alive"]["start_ts"] == core._load("alive")["start_ts"]
    assert rows["sched"]["scheduled_ts"] == T0 + 3600
    core.cmd_list(ns(json=True, full=True))
    full = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert full["alive"]["cmd"] and "env" in full["alive"] and "log_path" in full["alive"]
    core.cmd_kill(ns(name="alive", force=True))


def test_list_full_and_table_open_only_the_live_records(clock, opened, capsys):
    finished_job("ended")
    start("alive", "bash", "-c", "sleep 30")
    capsys.readouterr()
    for kw in ({"json": True, "full": True}, {"json": False}):
        opened.clear()
        core.cmd_list(ns(running=True, **kw))
        assert records_opened(opened) == ["alive.json"]
        assert "alive" in capsys.readouterr().out
    core.cmd_kill(ns(name="alive", force=True))


def test_list_main_fast_path_skips_the_argument_parser(clock, monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_parser", lambda: pytest.fail("parser built"))
    assert cli.main(["list", "--running", "--session", "s", "--json"]) == 0
    assert cli.main(["ls", "--session=s", "--scheduled"]) == 0
    assert cli._list_args(["--json", "--full"]).full is True


def test_list_fast_path_leaves_anything_unusual_to_the_parser():
    for rest in (
        ["--help"],
        ["--run"],
        ["--session"],
        ["--session", "--json"],
        ["x"],
        ["--session="],
    ):
        assert cli._list_args(rest) is None


def test_the_index_entry_carries_the_owner_and_start(monkeypatch):
    monkeypatch.setenv(core.ENV_OWNER_SESSION, "sess-a")
    start("alive", "bash", "-c", "sleep 30")
    m = core._load("alive")
    e = index_jobs()["alive"]
    assert e["owner"] == m["owner"] and e["start"] == m["start_ts"]
    core.cmd_kill(ns(name="alive", force=True))


def test_list_cli_flags_parse():
    a = cli.build_parser().parse_args(
        ["list", "--running", "--scheduled", "--session", "x", "--all"]
    )
    assert (a.running, a.scheduled, a.session, a.all) == (True, True, "x", True)


# --- wait / status read one record without the registry lock ------------------ #


def test_wait_and_status_take_no_lock_while_the_job_is_simply_running(monkeypatch, capsys):
    start("w", "bash", "-c", "sleep 30")
    real_lock = core._with_lock
    monkeypatch.setattr(core, "_with_lock", lambda fn: pytest.fail("registry lock taken"))
    assert core.cmd_wait(ns(name="w", timeout=0)) == core.EXIT_WAIT_TIMEOUT
    capsys.readouterr()  # drop what start and wait printed
    assert core.cmd_status(ns(name="w", json=True)) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "running"
    monkeypatch.setattr(core, "_with_lock", real_lock)
    core.cmd_kill(ns(name="w", force=True))


def test_wait_and_status_on_a_finished_job_take_no_lock(monkeypatch):
    finished_job("fin")
    monkeypatch.setattr(core, "_with_lock", lambda fn: pytest.fail("registry lock taken"))
    assert core.cmd_wait(ns(name="fin", timeout=5)) == 0
    assert core.cmd_status(ns(name="fin")) == 0
    assert core.cmd_status(ns(name="ghost")) == core.EXIT_NOTFOUND


def test_wait_finalises_a_job_that_ended_while_recorded_running():
    start("end", "bash", "-c", "exit 3")
    assert core.cmd_wait(ns(name="end", timeout=10)) == core.EXIT_JOB_FAILED
    assert core._load("end")["exit_code"] == 3


# --- the wrapper records the job's cost in .rc, status shows it ---------------- #


def test_parse_rc_reads_code_times_and_io():
    text = (
        "7\n0m0.003s 0m0.001s\n1m2.500s 0m0.250s\nio rchar: 100\nio wchar: 20\n"
        "io syscr: 3\nio read_bytes: 4096\nio write_bytes: 0\nio \nio cancelled_write_bytes: 0\n"
    )
    rc, c = core._parse_rc(text)
    assert rc == 7
    assert c == {
        "cpu_user_s": 62.5,
        "cpu_sys_s": 0.25,
        "rchar": 100,
        "wchar": 20,
        "syscr": 3,
        "read_bytes": 4096,
        "write_bytes": 0,
    }


def test_parse_rc_of_an_old_sidecar_is_the_code_alone():
    assert core._parse_rc("5\n") == (5, None)
    assert core._parse_rc("") == (None, None)
    assert core._parse_rc("x\n") == (None, None)


def test_a_finished_job_record_carries_its_cost(capsys):
    start("cst", "bash", "-c", "python3 -c 'sum(range(2000000))'; echo done")
    m = wait_terminal("cst")
    assert m["exit_code"] == 0
    c = m["cost"]
    assert c["cpu_user_s"] > 0 and c["rchar"] > 0 and "write_bytes" in c
    capsys.readouterr()
    core.cmd_status(ns(name="cst"))
    assert any(ln.startswith("  cost: cpu user ") for ln in capsys.readouterr().out.splitlines())
    assert core._read_rc({"name": "cst"}) == 0


def test_cost_of_a_job_that_reads_a_file_includes_the_file(tmp_path):
    big = tmp_path / "big"
    big.write_bytes(b"x" * 3_000_000)
    start("rd", "bash", "-c", f"dd if={big} of=/dev/null bs=65536 status=none")
    assert wait_terminal("rd")["cost"]["rchar"] >= 3_000_000


# --- own scope for launched jobs, unit accounting ------------------------------ #


def test_scope_prefix_only_inside_a_systemd_unit(monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    assert core._scope_prefix("j") == []
    monkeypatch.setenv("INVOCATION_ID", "abc")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(core, "_SCOPE_OK", True)
    prefix = core._scope_prefix("we ird/name")
    assert prefix[:4] == ["systemd-run", "--user", "--scope", "--quiet"]
    assert prefix[-1] == "--" and "--collect" in prefix
    unit = next(p for p in prefix if p.startswith("--unit="))
    assert unit.startswith("--unit=proc-job-we_ird_name-") and "/" not in unit
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert core._scope_prefix("j") == []


def test_no_scope_when_systemd_run_cannot_start_one(monkeypatch):
    """A launcher where the user manager is unreachable starts jobs unscoped."""
    monkeypatch.setenv("INVOCATION_ID", "abc")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(core, "_SCOPE_OK", None)
    calls = []

    def failing_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, b"", b"Failed to connect to bus")

    monkeypatch.setattr("subprocess.run", failing_run)
    assert core._scope_prefix("j") == []
    assert core._scope_prefix("k") == []
    assert len(calls) == 1  # probed once per process
    assert calls[0][:3] == ["systemd-run", "--user", "--scope"]


def test_stale_index_entries_are_dropped_by_reconcile_and_by_a_launch_pass(clock):
    """A crash between the record write and the index removal leaves a finished job listed
    as live; the next reconcile removes it. A scheduled entry the record no longer
    matches is dropped by the pass that finds it due."""
    finished_job("ghost")
    core._with_lock(lambda: core._index_set("ghost", {"status": core.RUNNING, "pid": 1, "st": 1}))
    assert "ghost" in index_jobs()
    core._with_lock(lambda: core._reconcile_and_save("ghost"))
    assert "ghost" not in index_jobs()
    core._with_lock(
        lambda: core._index_set("nothere", {"status": core.SCHEDULED, "scheduled_ts": T0})
    )
    core.cmd_launch_due(ns())
    assert "nothere" not in index_jobs()


def test_alert_stamp_is_freed_when_the_command_cannot_start(tmp_path):
    (tmp_path / cost.ALERT_CONFIG).write_text("/nonexistent/alert-command\n")
    assert cost.raise_alert(tmp_path, "launch-due", {}, {}) is False
    assert not list((tmp_path / "cost-alert").glob("*/proc.launch-due"))
    marker = tmp_path / "ok"
    (tmp_path / cost.ALERT_CONFIG).write_text(f"touch {marker}\n")
    assert cost.raise_alert(tmp_path, "launch-due", {}, {}) is True


def test_launcher_unit_turns_io_accounting_on():
    assert "IOAccounting=yes" in launcher._service_text(core.BASE)


# --- cost line and alert of a launch-due pass ----------------------------------- #


def cost_lines() -> list[dict]:
    files = sorted((core.BASE / "cost").glob("*.jsonl"))
    return [json.loads(ln) for f in files for ln in f.read_text().splitlines()]


def test_launch_due_appends_one_cost_line_per_pass(clock):
    core.cmd_launch_due(ns())
    core.cmd_launch_due(ns())
    lines = cost_lines()
    assert len(lines) == 2
    rec = lines[0]
    assert rec["tool"] == "proc" and rec["path"] == "proc launch-due"
    for key in ("ts", "wall_s", "cpu_s", "rss_mb", "rchar_mb", "read_mb", "write_mb", "n"):
        assert key in rec
    assert rec["launched"] == 0
    assert len(json.dumps(rec)) < 512


def test_a_pass_over_its_bound_is_marked_and_alerts_once_a_day(clock, monkeypatch, tmp_path):
    marker = tmp_path / "posted"
    (core.BASE / cost.ALERT_CONFIG).write_text(f"sh -c 'echo \"$COST_ALERT_KEY\" >> {marker}'\n")
    monkeypatch.setattr(cost, "LAUNCH_DUE_BOUNDS", {"cpu_s": -1.0})
    core.cmd_launch_due(ns())
    core.cmd_launch_due(ns())
    lines = cost_lines()
    assert [ln["over"] for ln in lines] == [["cpu_s"], ["cpu_s"]]  # every excess is recorded
    end = time.time() + 5
    while not marker.exists() and time.time() < end:
        time.sleep(0.05)
    time.sleep(0.2)
    assert marker.read_text().splitlines() == ["cost:proc:launch-due"]  # one post
    stamps = list((core.BASE / "cost-alert").glob("*/proc.launch-due"))
    assert len(stamps) == 1


def test_no_alert_command_means_no_stamp_and_no_post(clock, monkeypatch, tmp_path):
    monkeypatch.delenv(cost.ALERT_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(cost, "LAUNCH_DUE_BOUNDS", {"cpu_s": -1.0})
    core.cmd_launch_due(ns())
    assert cost_lines()[0]["over"] == ["cpu_s"]
    assert not (core.BASE / "cost-alert").exists()


def test_a_scratch_home_posts_nothing_whatever_the_user_configures(clock, monkeypatch, tmp_path):
    """The user's alert configuration (environment variable and ~/.config/proc) belongs to
    the default registry; a pass on any other home records its line and posts nothing."""
    marker = tmp_path / "posted"
    cmd = f"sh -c 'echo x >> {marker}'"
    monkeypatch.setenv(cost.ALERT_ENV, cmd)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg" / "proc").mkdir(parents=True)
    (tmp_path / "cfg" / "proc" / cost.ALERT_CONFIG).write_text(cmd + "\n")
    monkeypatch.setattr(cost, "LAUNCH_DUE_BOUNDS", {"cpu_s": -1.0})
    core.cmd_launch_due(ns())
    assert cost_lines()[0]["over"] == ["cpu_s"]  # recorded
    assert cost.alert_command(core.BASE) is None
    assert not (core.BASE / "cost-alert").exists()
    time.sleep(0.3)
    assert not marker.exists()


def test_the_default_registry_reads_the_user_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(cost.ALERT_ENV, "my-alert")
    assert cost.alert_command(tmp_path / "home" / ".local" / "state" / "proc") == ["my-alert"]
    assert cost.alert_command() == ["my-alert"]


def test_alert_command_may_come_from_the_config_file(monkeypatch, tmp_path):
    monkeypatch.delenv(cost.ALERT_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    cfg = tmp_path / "cfg" / "proc"
    cfg.mkdir(parents=True)
    (cfg / cost.ALERT_CONFIG).write_text("# comment\n\nmy-alert --flag 'a b'\n")
    assert cost.alert_command() == ["my-alert", "--flag", "a b"]


def test_a_pass_that_started_a_job_is_recorded_without_a_bound(clock, monkeypatch):
    monkeypatch.setattr(cost, "LAUNCH_DUE_BOUNDS", {"cpu_s": -1.0})
    start("go", "true", at=iso(T0 + 1))
    clock[0] = T0 + 2
    core.cmd_launch_due(ns())
    rec = cost_lines()[-1]
    assert rec["launched"] == 1 and "over" not in rec
    wait_terminal("go")


def test_cost_files_and_stamps_older_than_two_weeks_go_when_a_new_day_starts(tmp_path):
    base = tmp_path / "b"
    (base / "cost").mkdir(parents=True)
    (base / "cost" / "2020-01-01.jsonl").write_text("{}\n")
    (base / "cost-alert" / "2020-01-01").mkdir(parents=True)
    today = datetime(2026, 11, 12, tzinfo=UTC)
    (base / "cost" / "2026-11-10.jsonl").write_text("{}\n")
    cost.append_line(base, {"a": 1}, today)
    assert not (base / "cost" / "2020-01-01.jsonl").exists()
    assert not (base / "cost-alert" / "2020-01-01").exists()
    assert (base / "cost" / "2026-11-10.jsonl").exists()
    assert (base / "cost" / "2026-11-12.jsonl").read_text() == '{"a":1}\n'


# --- clean ------------------------------------------------------------------------ #


def test_clean_seven_days_keeps_recent_and_removes_old_without_the_whole_scan_lock(
    monkeypatch,
):
    finished_job("six")
    finished_job("eight")
    finished_job("fresh")
    for name, days in (("six", 6), ("eight", 8)):
        m = core._load(name)
        m["end_ts"] = time.time() - days * 86400
        core._save(m)
    held: list[int] = []
    real = core._with_lock
    monkeypatch.setattr(core, "_with_lock", lambda fn: (held.append(1), real(fn))[1])
    assert core.cmd_clean(ns(older_than=168)) == 0
    assert core._load("eight") is None
    assert core._load("six") is not None and core._load("fresh") is not None
    assert len(held) == 1  # one lock section for the one removal, none for the scan


def test_clean_leaves_a_job_still_marked_running(monkeypatch):
    start("busy", "bash", "-c", "sleep 30")
    m = core._load("busy")
    m["start_ts"] = time.time() - 30 * 86400
    core._save(m)
    core.cmd_clean(ns(older_than=1))
    assert core._load("busy") is not None
    core.cmd_kill(ns(name="busy", force=True))
