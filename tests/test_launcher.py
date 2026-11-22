"""Launcher backends, driven through a fake command runner and a tmp HOME."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from proc_lib import core, launcher

from .conftest import REAL_ENSURE


class Fake:
    """Scripted stand-in for launcher._run; records every call."""

    def __init__(self, systemd=True, crontab="", cron_daemon=True, crontab_bin=True):
        self.systemd = systemd
        self.crontab = crontab
        self.cron_daemon = cron_daemon
        self.crontab_bin = crontab_bin
        self.calls: list[list[str]] = []
        self.enabled = False

    def __call__(self, argv, stdin=None):
        self.calls.append(argv)
        rc, out, err = 0, "", ""
        if argv[:3] == ["systemctl", "--user", "show-environment"]:
            rc = 0 if self.systemd else 1
            err = "" if self.systemd else "Failed to connect to bus"
        elif argv[:3] == ["systemctl", "--user", "enable"]:
            self.enabled = True
        elif argv[:3] == ["systemctl", "--user", "disable"]:
            self.enabled = False
        elif argv[:3] in (
            ["systemctl", "--user", "is-enabled"],
            ["systemctl", "--user", "is-active"],
        ):
            rc = 0 if self.enabled else 1
            out = "enabled" if self.enabled else "disabled"
        elif argv[0] == "crontab" and not self.crontab_bin:
            rc, err = 127, "crontab: not found"
        elif argv == ["crontab", "-l"]:
            rc, out = (0, self.crontab) if self.crontab else (1, "")
            err = "" if self.crontab else "no crontab for user"
        elif argv == ["crontab", "-"]:
            self.crontab = stdin
        elif argv[0] == "pgrep":
            rc = 0 if self.cron_daemon and argv[2] == "cron" else 1
        return subprocess.CompletedProcess(argv, rc, out, err)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def use(monkeypatch, fake):
    monkeypatch.setattr(launcher, "_run", fake)
    return fake


@pytest.mark.parametrize("operation", ["status", "install", "uninstall"])
def test_systemd_works_without_bus_environment(home, monkeypatch, operation):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(launcher.os, "getuid", lambda: 12345)
    is_dir = Path.is_dir
    monkeypatch.setattr(
        Path, "is_dir", lambda path: path == Path("/run/user/12345") or is_dir(path)
    )
    fake = Fake()
    fake.enabled = True
    unit_dir = home / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    (unit_dir / launcher.TIMER).touch()

    def run(argv, **kwargs):
        if argv[:2] == ["systemctl", "--user"]:
            assert kwargs["env"]["XDG_RUNTIME_DIR"] == "/run/user/12345"
            assert kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/12345/bus"
        return fake(argv, kwargs.get("input"))

    monkeypatch.setattr(launcher.subprocess, "run", run)
    st = launcher.install(core.BASE) if operation == "install" else getattr(launcher, operation)()
    assert st.ok if operation != "uninstall" else st.detail == "removed"
    assert any(argv[:2] == ["systemctl", "--user"] for argv in fake.calls)
    assert "XDG_RUNTIME_DIR" not in launcher.os.environ
    assert "DBUS_SESSION_BUS_ADDRESS" not in launcher.os.environ


@pytest.mark.parametrize(
    ("runtime", "bus", "directory_exists", "expected_runtime", "expected_bus"),
    [
        (None, None, False, None, None),
        ("", "", True, "/run/user/12345", "unix:path=/run/user/12345/bus"),
        ("/custom/runtime", None, True, "/custom/runtime", "unix:path=/custom/runtime/bus"),
        (None, "unix:path=/custom/bus", True, "/run/user/12345", "unix:path=/custom/bus"),
        (
            "/custom/runtime",
            "unix:path=/custom/bus",
            True,
            "/custom/runtime",
            "unix:path=/custom/bus",
        ),
        ("/missing/runtime", None, False, "/missing/runtime", None),
    ],
)
def test_systemd_preserves_environment_and_bus_failure(
    home, monkeypatch, runtime, bus, directory_exists, expected_runtime, expected_bus
):
    for key, value in (("XDG_RUNTIME_DIR", runtime), ("DBUS_SESSION_BUS_ADDRESS", bus)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(launcher.os, "getuid", lambda: 12345)
    monkeypatch.setattr(Path, "is_dir", lambda path: directory_exists)
    original_env = launcher.os.environ.copy()
    unit_dir = home / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    (unit_dir / launcher.TIMER).touch()

    def run(argv, **kwargs):
        assert kwargs["env"].get("XDG_RUNTIME_DIR") == expected_runtime
        assert kwargs["env"].get("DBUS_SESSION_BUS_ADDRESS") == expected_bus
        assert kwargs["timeout"] == launcher.RUN_TIMEOUT_SECONDS
        return subprocess.CompletedProcess(
            argv, 1, "", "Failed to connect to bus: Connection refused"
        )

    monkeypatch.setattr(launcher.subprocess, "run", run)
    st = launcher.status()
    assert not st.ok and st.backend == "systemd"
    assert "Failed to connect to bus: Connection refused" in st.detail
    assert launcher.os.environ == original_env


def test_other_helpers_inherit_environment(monkeypatch):
    def run(argv, **kwargs):
        assert kwargs["env"] is None
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher._run(["crontab", "-l"]).returncode == 0


@pytest.mark.parametrize("failed_command", ["disable", "daemon-reload"])
def test_uninstall_reports_systemd_failure(home, monkeypatch, failed_command):
    fake = Fake()
    use(monkeypatch, fake)
    launcher.install(core.BASE)

    def run(argv, stdin=None):
        if argv[:3] == ["systemctl", "--user", failed_command]:
            return subprocess.CompletedProcess(argv, 1, "", "Failed to connect to bus: refused")
        return fake(argv, stdin)

    monkeypatch.setattr(launcher, "_run", run)
    with pytest.raises(launcher.LauncherError, match="Failed to connect to bus: refused"):
        launcher.uninstall()
    assert (home / ".config/systemd/user" / launcher.TIMER).exists() == (
        failed_command == "disable"
    )


def test_install_writes_units_and_enables_timer(home, monkeypatch):
    fake = use(monkeypatch, Fake())
    st = launcher.install(core.BASE)
    assert st.backend == "systemd" and st.ok
    unit = (home / ".config/systemd/user/proc-launch.service").read_text()
    assert f"PROC_HOME={core.BASE}" in unit
    assert "KillMode=process" in unit
    assert "IOAccounting=yes" in unit
    assert "proc_lib.cli launch-due" in unit
    assert "OnCalendar=minutely" in (home / ".config/systemd/user/proc-launch.timer").read_text()
    assert ["systemctl", "--user", "enable", "--now", launcher.TIMER] in fake.calls


def test_ensure_is_a_no_op_when_launcher_already_works(home, monkeypatch):
    fake = use(monkeypatch, Fake())
    launcher.install(core.BASE)
    fake.calls.clear()
    st = REAL_ENSURE(core.BASE)
    assert st.ok
    assert not any(c[:3] == ["systemctl", "--user", "enable"] for c in fake.calls)


def test_ensure_adds_io_accounting_to_an_older_unit_and_keeps_its_exec_and_home(home, monkeypatch):
    fake = use(monkeypatch, Fake())
    launcher.install(core.BASE)
    unit_path = home / ".config/systemd/user/proc-launch.service"
    old = unit_path.read_text().replace("IOAccounting=yes\n", "")
    old = old.replace(f"PROC_HOME={core.BASE}", "PROC_HOME=/elsewhere")
    unit_path.write_text(old)
    fake.calls.clear()
    assert REAL_ENSURE(core.BASE).ok
    new = unit_path.read_text()
    assert "IOAccounting=yes" in new and "PROC_HOME=/elsewhere" in new
    assert new.replace("IOAccounting=yes\n", "") == old
    assert ["systemctl", "--user", "daemon-reload"] in fake.calls


def test_ensure_installs_when_missing(home, monkeypatch):
    use(monkeypatch, Fake())
    assert REAL_ENSURE(core.BASE).ok


def test_falls_back_to_cron_when_user_systemd_is_unavailable(home, monkeypatch):
    fake = use(monkeypatch, Fake(systemd=False, crontab="0 3 * * * other\n"))
    st = launcher.install(core.BASE)
    assert st.backend == "cron" and st.ok
    assert "0 3 * * * other" in fake.crontab
    assert fake.crontab.count(launcher.CRON_MARKER) == 1
    launcher.install(core.BASE)
    assert fake.crontab.count(launcher.CRON_MARKER) == 1


def test_no_backend_raises_naming_both_reasons(home, monkeypatch):
    use(monkeypatch, Fake(systemd=False, cron_daemon=False))
    with pytest.raises(launcher.LauncherError, match="user systemd unreachable.*no cron daemon"):
        launcher.install(core.BASE)
    use(monkeypatch, Fake(systemd=False, crontab_bin=False))
    with pytest.raises(launcher.LauncherError, match="crontab is unusable"):
        launcher.install(core.BASE)


def test_status_reports_not_installed_and_broken_timer(home, monkeypatch):
    use(monkeypatch, Fake())
    assert not launcher.status().ok and launcher.status().backend is None
    launcher.install(core.BASE)
    use(monkeypatch, Fake())  # timer files exist but systemd says disabled
    st = launcher.status()
    assert st.backend == "systemd" and not st.ok


def test_uninstall_removes_units_and_cron_line(home, monkeypatch):
    fake = use(monkeypatch, Fake())
    launcher.install(core.BASE)
    launcher.uninstall()
    assert not (home / ".config/systemd/user/proc-launch.timer").exists()
    fake = use(monkeypatch, Fake(systemd=False, crontab="x\n"))
    launcher.install(core.BASE)
    launcher.uninstall()
    assert launcher.CRON_MARKER not in fake.crontab and "x" in fake.crontab


def test_start_at_fails_loudly_when_no_launcher_works(monkeypatch, capsys):
    def broken(base):
        raise launcher.LauncherError("nothing")

    monkeypatch.setattr(launcher, "ensure", broken)
    from .conftest import start

    assert start("nol", "true", at="2026-10-23T13:19:00Z") == core.EXIT_LAUNCHER
    assert core._load("nol") is None
    assert "nothing" in capsys.readouterr().err


def test_install_with_timer_removes_a_leftover_cron_line(home, monkeypatch):
    fake = use(monkeypatch, Fake(crontab=f"0 3 * * * other\n* * * * * x {launcher.CRON_MARKER}\n"))
    st = launcher.install(core.BASE)
    assert st.backend == "systemd" and st.ok
    assert fake.crontab == "0 3 * * * other\n"
    assert launcher.status().ok


def test_status_reports_both_launchers_as_a_fault_naming_the_line(home, monkeypatch, capsys):
    fake = use(monkeypatch, Fake())
    launcher.install(core.BASE)
    line = f"* * * * * leftover-cmd {launcher.CRON_MARKER}"
    fake.crontab = f"{line}\n"
    st = launcher.status()
    assert not st.ok and "leftover-cmd" in st.detail
    assert core.cmd_launcher(type("A", (), {"action": "status"})()) == core.EXIT_LAUNCHER
    assert "leftover-cmd" in capsys.readouterr().out
    # ensure repairs it
    assert REAL_ENSURE(core.BASE).ok and launcher.CRON_MARKER not in fake.crontab
