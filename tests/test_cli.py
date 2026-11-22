"""CLI surface: the argparse front end wires subcommands and exit codes."""

from __future__ import annotations

import pytest

from proc_lib import cli, core

from .conftest import wait_terminal


def test_main_notfound_exit_code():
    assert cli.main(["status", "ghost"]) == core.EXIT_NOTFOUND


def test_main_start_and_ls_alias(capsys):
    rc = cli.main(["start", "--name", "cli1", "--cwd", "/tmp", "--", "bash", "-c", "exit 0"])
    assert rc == 0
    wait_terminal("cli1")
    capsys.readouterr()
    assert cli.main(["ls", "--all"]) == 0
    assert "cli1" in capsys.readouterr().out


def test_main_start_with_timeout_flag(capsys):
    argv = ["start", "--name", "cli_to", "--timeout", "1", "--cwd", "/tmp"]
    argv += ["--", "bash", "-c", "sleep 5"]
    rc = cli.main(argv)
    assert rc == 0
    m = wait_terminal("cli_to", deadline_s=8)
    assert m["status"] == "timeout"


def test_main_requires_subcommand():
    with pytest.raises(SystemExit):
        cli.main([])


def test_main_wait_timeout_exit_three():
    cli.main(["start", "--name", "cliw", "--cwd", "/tmp", "--", "bash", "-c", "sleep 5"])
    rc = cli.main(["wait", "cliw", "--timeout", "0"])
    assert rc == core.EXIT_WAIT_TIMEOUT
    cli.main(["kill", "cliw", "--force"])
