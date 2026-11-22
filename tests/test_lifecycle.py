"""Lifecycle: start, list, status, logs, wait, and their exit codes."""

from __future__ import annotations

from proc_lib import core

from .conftest import ns, start, wait_terminal


def test_start_creates_registry_entry(isolated_base):
    rc = start("job1", "bash", "-c", "exit 0")
    assert rc == 0
    m = core._load("job1")
    assert m is not None
    assert m["name"] == "job1"
    assert m["cmd"] == ["bash", "-c", "exit 0"]
    assert (isolated_base / "job1.json").exists()
    assert (isolated_base / "job1.log").exists()


def test_start_missing_command_is_usage_error():
    rc = core.cmd_start(ns(name="x", timeout=None, cwd="/tmp", cmd=[]))
    assert rc == core.EXIT_USAGE


def test_start_bad_cwd_is_usage_error():
    rc = core.cmd_start(ns(name="x", timeout=None, cwd="/no/such/dir", cmd=["--", "true"]))
    assert rc == core.EXIT_USAGE


def test_start_strips_leading_double_dash():
    # cmd arrives from REMAINDER with a leading '--'; it must be stripped.
    start("j", "echo", "hi")
    assert core._load("j")["cmd"] == ["echo", "hi"]


def test_duplicate_running_name_rejected():
    start("dup", "bash", "-c", "sleep 2")
    rc = start("dup", "bash", "-c", "sleep 2")
    assert rc == core.EXIT_USAGE
    core.cmd_kill(ns(name="dup", force=True))


def test_terminal_name_requires_rm_before_reuse():
    start("re", "bash", "-c", "exit 0")
    wait_terminal("re")
    rc = start("re", "bash", "-c", "exit 0")
    assert rc == core.EXIT_USAGE  # must rm first; won't clobber the log


def test_status_unknown_job_is_notfound():
    assert core.cmd_status(ns(name="ghost")) == core.EXIT_NOTFOUND


def test_status_reports_exited(capsys):
    start("s", "bash", "-c", "exit 3")
    wait_terminal("s")
    capsys.readouterr()
    rc = core.cmd_status(ns(name="s"))
    assert rc == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("s: exited rc=3 after ")
    assert "exit_code: 3" in out


def test_logs_capture_stdout(capsys):
    start("lg", "bash", "-c", "echo hello-proc")
    wait_terminal("lg")
    rc = core.cmd_logs(ns(name="lg", tail=None, follow=False))
    assert rc == 0
    assert "hello-proc" in capsys.readouterr().out


def test_logs_tail(capsys):
    start("lt", "bash", "-c", "printf 'a\\nb\\nc\\nd\\n'")
    wait_terminal("lt")
    capsys.readouterr()  # flush the start() banner so we only see log output
    core.cmd_logs(ns(name="lt", tail=2, follow=False))
    out = capsys.readouterr().out
    assert "c" in out and "d" in out and "a" not in out


def test_logs_unknown_job_is_notfound():
    assert core.cmd_logs(ns(name="nope", tail=None, follow=False)) == core.EXIT_NOTFOUND


def test_wait_returns_zero_on_terminal(capsys):
    start("w", "bash", "-c", "exit 0")
    rc = core.cmd_wait(ns(name="w", timeout=10))
    assert rc == 0
    assert "exited" in capsys.readouterr().out


def test_wait_timeout_returns_three(capsys):
    start("wt", "bash", "-c", "sleep 5")
    rc = core.cmd_wait(ns(name="wt", timeout=0))  # 0s budget -> still running
    assert rc == core.EXIT_WAIT_TIMEOUT
    core.cmd_kill(ns(name="wt", force=True))


def test_list_empty(capsys):
    rc = core.cmd_list(ns())
    assert rc == 0
    assert "no jobs" in capsys.readouterr().out


def test_list_shows_jobs(capsys):
    start("a", "bash", "-c", "exit 0")
    start("b", "bash", "-c", "sleep 2")
    wait_terminal("a")
    capsys.readouterr()  # drop what start printed
    core.cmd_list(ns(all=True))
    out = capsys.readouterr().out
    assert "NAME" in out
    assert [ln.split()[0] for ln in out.splitlines()[1:]] == ["b", "a"]  # running first
    core.cmd_kill(ns(name="b", force=True))


def test_rm_single_name_still_works(capsys):
    start("solo", "true")
    wait_terminal("solo")
    rc = core.cmd_rm(ns(name=["solo"], force=False))
    assert rc == 0
    assert "removed 'solo'" in capsys.readouterr().out
    assert core.cmd_status(ns(name="solo")) == core.EXIT_NOTFOUND


def test_rm_multiple_names(capsys):
    for n in ("r1", "r2", "r3"):
        start(n, "true")
        wait_terminal(n)
    rc = core.cmd_rm(ns(name=["r1", "r2", "r3"], force=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("removed '") == 3
    for n in ("r1", "r2", "r3"):
        assert core.cmd_status(ns(name=n)) == core.EXIT_NOTFOUND


def test_rm_multiple_continues_past_missing(capsys):
    start("present", "true")
    wait_terminal("present")
    # one real job + one missing name: the real one is still removed,
    # and the worst-case exit code (NOTFOUND) is surfaced.
    rc = core.cmd_rm(ns(name=["present", "ghost"], force=False))
    assert rc == core.EXIT_NOTFOUND
    assert core.cmd_status(ns(name="present")) == core.EXIT_NOTFOUND


def test_rm_clears_heavy_lock():
    from proc_lib import heavy_job_lock

    start("hv", "true")
    wait_terminal("hv")
    # simulate a heavy-job lockfile left under this job's name
    heavy_job_lock.claim("hv", 999999, "true")
    lock = core.BASE / "heavy_job" / "hv.lock"
    assert lock.exists()
    core.cmd_rm(ns(name=["hv"], force=False))
    assert not lock.exists()
