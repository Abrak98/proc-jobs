"""`proc logs --since TS`: filter a log to the lines after a time."""

import os

from proc_lib import core

from .conftest import ns, start, wait_terminal

LOG = (
    "orphan line before any time\n"
    "2026-10-31T10:00:00Z first\n"
    "  first continuation\n"
    "2026-10-31T11:00:00Z second\n"
    "  second continuation\n"
    "2026-10-31T12:00:00+00:00 third\n"
)


def _job(name, text):
    start(name, "true")
    wait_terminal(name)
    core._log_path(name).write_text(text)


def _logs(name, since, capsys, **kw):
    capsys.readouterr()
    rc = core.cmd_logs(ns(name=name, tail=kw.get("tail"), follow=False, since=since))
    return rc, capsys.readouterr()


def test_since_between_lines(capsys):
    _job("s1", LOG)
    rc, cap = _logs("s1", "2026-10-31T10:30:00Z", capsys)
    assert rc == 0
    assert cap.out == (
        "2026-10-31T11:00:00Z second\n  second continuation\n2026-10-31T12:00:00+00:00 third\n"
    )


def test_since_zone_offset_is_honoured(capsys):
    _job("s2", LOG)
    # 13:30+03:00 is 10:30Z
    rc, cap = _logs("s2", "2026-10-31T13:30:00+03:00", capsys)
    assert rc == 0
    assert cap.out.startswith("2026-10-31T11:00:00Z second")


def test_since_after_last_line_prints_nothing(capsys):
    _job("s3", LOG)
    rc, cap = _logs("s3", "2026-10-31T12:00:01Z", capsys)
    assert rc == 0
    assert cap.out == ""


def test_since_with_tail(capsys):
    _job("s4", LOG)
    rc, cap = _logs("s4", "2026-10-31T10:30:00Z", capsys, tail=1)
    assert rc == 0
    assert cap.out == "2026-10-31T12:00:00+00:00 third\n"


def test_since_unparseable_is_usage_error(capsys):
    _job("s5", LOG)
    rc, cap = _logs("s5", "yesterday", capsys)
    assert rc == core.EXIT_USAGE
    assert cap.out == ""
    assert "--since expects an ISO 8601 time with a zone" in cap.err


def test_since_naive_time_is_usage_error(capsys):
    _job("s6", LOG)
    rc, cap = _logs("s6", "2026-10-31T10:30:00", capsys)
    assert rc == core.EXIT_USAGE
    assert cap.out == ""


def test_since_untimed_log_uses_mtime(capsys):
    _job("s7", "plain\nlines\n")
    os.utime(core._log_path("s7"), (1_000_000_000, 1_000_000_000))  # 2001-09-09T01:46:40Z
    rc, cap = _logs("s7", "2001-01-01T00:00:00Z", capsys)
    assert (rc, cap.out) == (0, "plain\nlines\n")
    rc, cap = _logs("s7", "2002-01-01T00:00:00Z", capsys)
    assert (rc, cap.out) == (0, "")
