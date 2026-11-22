"""Peak RSS in a job's `.rc`: the wrapper runs the job under GNU time, folds its figure into
the sidecar, and `status` shows it, or says why it is absent."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from proc_lib import core

from .conftest import ns, start, wait_terminal

needs_gnu_time = pytest.mark.skipif(core._gnu_time() is None, reason="GNU time not installed")

# Touches every page of ~64 MB, so the job's peak resident size is well above 50 MB.
ALLOC = "b = bytearray(64 * 1024 * 1024); b[::4096] = b'1' * len(b[::4096])"


@needs_gnu_time
def test_a_finished_job_records_its_peak_rss():
    start("big", "python3", "-c", ALLOC)
    m = wait_terminal("big")
    assert m["exit_code"] == 0
    assert 50_000 <= m["cost"]["maxrss_kb"] < 400_000
    assert "maxrss_note" not in m["cost"]


@needs_gnu_time
def test_peak_rss_is_that_of_the_largest_descendant_not_the_wrapper():
    start("kid", "bash", "-c", f'python3 -c "{ALLOC}"; echo done')
    assert wait_terminal("kid")["cost"]["maxrss_kb"] >= 50_000


@needs_gnu_time
def test_exit_code_and_log_are_unchanged_by_the_measurement(capsys):
    start("ec", "bash", "-c", "echo out; echo err >&2; exit 7")
    m = wait_terminal("ec")
    assert m["status"] == core.EXITED and m["exit_code"] == 7
    assert (core.BASE / "ec.log").read_text().splitlines() == ["out", "err"]
    assert core._read_rc({"name": "ec"}) == 7


@needs_gnu_time
def test_a_job_killed_by_a_signal_keeps_the_shell_code():
    start("sg", "bash", "-c", "kill -9 $$")
    m = wait_terminal("sg")
    assert m["exit_code"] == 137  # 128 + 9, as bash alone reports it
    assert (core.BASE / "sg.log").read_text() == ""
    assert isinstance(m["cost"]["maxrss_kb"], int)


@needs_gnu_time
def test_a_timeout_still_ends_as_timeout_with_its_peak_rss():
    start("to", "sleep", "30", timeout=1)
    m = wait_terminal("to", deadline_s=20)
    assert m["status"] == core.TIMEOUT and m["exit_code"] == 124
    assert m["cost"]["maxrss_kb"] > 0


@needs_gnu_time
def test_the_side_file_is_gone_once_the_job_is_folded_into_rc():
    start("sf", "true")
    wait_terminal("sf")
    assert not core._rss_path("sf").exists()
    assert "maxrss_kb " in (core.BASE / "sf.rc").read_text()


@needs_gnu_time
def test_status_and_json_show_the_field(capsys):
    start("st", "python3", "-c", ALLOC)
    wait_terminal("st")
    capsys.readouterr()
    core.cmd_status(ns(name="st"))
    assert " peak RSS " in capsys.readouterr().out
    core.cmd_status(ns(name="st", json=True))
    assert json.loads(capsys.readouterr().out)["cost"]["maxrss_kb"] >= 50_000


def test_without_gnu_time_the_field_is_absent_and_status_says_why(monkeypatch, capsys):
    monkeypatch.setattr(core, "_gnu_time", lambda: None)
    start("nt", "bash", "-c", "echo hi; exit 3")
    m = wait_terminal("nt")
    assert m["exit_code"] == 3
    assert "maxrss_kb" not in m["cost"]
    assert m["cost"]["maxrss_note"] == core._RSS_NOTE_NO_TIME
    assert (core.BASE / "nt.log").read_text() == "hi\n"
    assert not core._rss_path("nt").exists()
    capsys.readouterr()
    core.cmd_status(ns(name="nt"))
    assert "peak RSS not recorded (GNU time is not installed)" in capsys.readouterr().out


def test_gnu_time_that_left_no_figure_is_reported_as_such():
    """`time` killed before it wrote: the wrapper (here fed a time binary that writes
    nothing) records the reason instead of a figure."""
    script = core._wrapper_script("true", core.BASE / "x.rc", "/bin/true", core.BASE / "x.rss")
    subprocess.run(["bash", "-c", script], check=True)
    rc, cost = core._read_rc_file({"name": "x"})
    assert rc == 0
    assert cost["maxrss_note"] == core._RSS_NOTE_NO_FIGURE
    assert "maxrss_kb" not in cost


def test_parse_rc_reads_the_peak_rss_and_the_note():
    assert core._parse_rc("0\nmaxrss_kb 1234\n") == (0, {"maxrss_kb": 1234})
    assert core._parse_rc("0\nmaxrss_kb x\n") == (0, None)
    assert core._parse_rc("0\nmaxrss_note GNU time is not installed\n") == (
        0,
        {"maxrss_note": "GNU time is not installed"},
    )


def test_cost_text_formats_the_peak_and_each_absent_case():
    assert "peak RSS 2.0 MB" in core._cost_text({"cpu_user_s": 1.0, "maxrss_kb": 2048})
    assert core._cost_text({"cpu_user_s": 1.0, "maxrss_note": "why"}).endswith(
        "peak RSS not recorded (why)"
    )
    # a sidecar written before this field existed
    assert core._cost_text({"cpu_user_s": 1.0}).endswith(
        f"peak RSS not recorded ({core._RSS_NOTE_OLD})"
    )


def test_gnu_time_probe_rejects_a_time_without_gnu_flags(monkeypatch, tmp_path):
    fake = tmp_path / "time"
    fake.write_text("#!/bin/sh\necho 'BusyBox v1'\n")
    fake.chmod(0o755)
    core._gnu_time.cache_clear()
    monkeypatch.setattr("os.access", lambda p, mode: False)
    monkeypatch.setattr(shutil, "which", lambda name: str(fake))
    try:
        assert core._gnu_time() is None
    finally:
        core._gnu_time.cache_clear()


def test_removing_a_job_drops_its_side_file():
    start("rm1", "true")
    wait_terminal("rm1")
    core._rss_path("rm1").write_text("maxrss 1\n")
    core.cmd_rm(ns(name="rm1", force=False))
    assert not core._rss_path("rm1").exists()
