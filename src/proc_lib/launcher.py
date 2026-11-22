# spec: specs/proc_lib/launcher.spec.md
"""The per-minute launcher that starts scheduled jobs.

`proc launch-due` must run every minute for `proc start --at` to mean anything. This
module installs, checks and removes whatever fires it: a systemd user timer when the
user manager is reachable, otherwise a cron line. It knows nothing about the registry;
the base dir is passed in so the unit can pin ``PROC_HOME``.

stdlib-only, like the rest of the package.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CRON_MARKER",
    "LauncherError",
    "RUN_TIMEOUT_SECONDS",
    "SERVICE",
    "Status",
    "TIMER",
    "TIMER_TEXT",
    "ensure",
    "install",
    "status",
    "uninstall",
]

SERVICE = "proc-launch.service"
TIMER = "proc-launch.timer"
CRON_MARKER = "# proc-launch"
RUN_TIMEOUT_SECONDS = 30


class LauncherError(Exception):
    """No launcher backend could be installed or verified."""


@dataclass(frozen=True)
class Status:
    backend: str | None  # "systemd" | "cron" | None
    ok: bool
    detail: str

    def line(self) -> str:
        state = "ok" if self.ok else "NOT WORKING"
        return f"launcher: {self.backend or 'none'} {state} - {self.detail}"


def _run(argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run a helper command; a missing binary or a hang is a failed result, not a raise."""
    env = None
    if argv[:2] == ["systemctl", "--user"]:
        env = os.environ.copy()
        runtime_dir = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        if Path(runtime_dir).is_dir():
            env["XDG_RUNTIME_DIR"] = runtime_dir
            if not env.get("DBUS_SESSION_BUS_ADDRESS"):
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime_dir}/bus"
    try:
        return subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"{argv[0]}: timed out")


def _unit_dir() -> Path:
    return Path(os.path.expanduser("~/.config/systemd/user"))


def _launch_argv() -> list[str]:
    return [sys.executable, "-I", "-m", "proc_lib.cli", "launch-due"]


def _service_text(base: Path) -> str:
    # Each job the pass starts goes into its own systemd scope (`systemd-run --scope`),
    # so nothing of it stays in this unit's cgroup. KillMode=process is the fallback for
    # a job that could not get a scope: the default would kill it the moment the
    # oneshot pass ends. IOAccounting makes systemd keep the unit's disk read/write.
    return (
        "[Unit]\n"
        "Description=proc: start due scheduled jobs\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "KillMode=process\n"
        "IOAccounting=yes\n"
        f"Environment=PROC_HOME={shlex.quote(str(base))}\n"
        f"ExecStart={shlex.join(_launch_argv())}\n"
    )


TIMER_TEXT = (
    "[Unit]\n"
    "Description=proc: every minute, start due scheduled jobs\n\n"
    "[Timer]\n"
    "OnCalendar=minutely\n"
    "AccuracySec=1s\n"
    "Persistent=true\n\n"
    "[Install]\n"
    "WantedBy=timers.target\n"
)


def _systemd_unavailable() -> str | None:
    r = _run(["systemctl", "--user", "show-environment"])
    if r.returncode == 0:
        return None
    return f"user systemd unreachable ({(r.stderr or r.stdout).strip() or f'rc {r.returncode}'})"


def _cron_line(base: Path) -> str:
    env = f"PROC_HOME={shlex.quote(str(base))}"
    return f"* * * * * {env} {shlex.join(_launch_argv())} >/dev/null 2>&1 {CRON_MARKER}"


def _crontab() -> str | None:
    """Current crontab text ('' when empty), None when crontab itself is unusable."""
    r = _run(["crontab", "-l"])
    if r.returncode == 0:
        return r.stdout
    if r.returncode == 127:
        return None
    if "no crontab" in (r.stderr + r.stdout).lower():
        return ""
    return None


def _cron_daemon_running() -> bool:
    return any(_run(["pgrep", "-x", name]).returncode == 0 for name in ("cron", "crond"))


def _systemd_status() -> Status | None:
    """Status of the systemd backend, or None when it is not installed."""
    if not (_unit_dir() / TIMER).exists():
        return None
    enabled = _run(["systemctl", "--user", "is-enabled", TIMER])
    active = _run(["systemctl", "--user", "is-active", TIMER])
    if enabled.returncode == 0 and active.returncode == 0:
        return Status("systemd", True, f"{TIMER} enabled and active")
    return Status(
        "systemd",
        False,
        f"{TIMER} is-enabled={enabled.stdout.strip() or enabled.stderr.strip()} "
        f"is-active={active.stdout.strip() or active.stderr.strip()}",
    )


def _upgrade_unit() -> bool:
    """Add settings a newer version writes to the installed service unit in place, keeping
    its ExecStart and PROC_HOME (they belong to whoever installed it; a throwaway
    PROC_HOME or a dev checkout must not repoint the real timer). True when it changed."""
    path = _unit_dir() / SERVICE
    try:
        text = path.read_text()
    except OSError:
        return False
    if "IOAccounting=" in text or "Type=oneshot\n" not in text:
        return False
    path.write_text(text.replace("Type=oneshot\n", "Type=oneshot\nIOAccounting=yes\n", 1))
    r = _run(["systemctl", "--user", "daemon-reload"])
    if r.returncode != 0:
        raise LauncherError(
            f"systemctl --user daemon-reload failed: {(r.stderr or r.stdout).strip()}"
        )
    return True


def _cron_status() -> Status | None:
    text = _crontab()
    if text is None or CRON_MARKER not in text:
        return None
    if not _cron_daemon_running():
        return Status("cron", False, "crontab line present but no cron daemon is running")
    return Status("cron", True, "crontab line present, cron daemon running")


def _cron_lines() -> list[str]:
    """The marked crontab lines (a launcher fallback), empty when there are none."""
    return [ln for ln in (_crontab() or "").splitlines() if CRON_MARKER in ln]


def status() -> Status:
    """Report the installed backend. A systemd timer and a marked crontab line together
    are a fault: `launch-due` would run twice a minute."""
    sd = _systemd_status()
    if sd is not None:
        lines = _cron_lines() if sd.ok else []  # a dead timer plus a cron line is no duplicate
        if lines:
            return Status(
                "systemd+cron",
                False,
                f"both launchers are present, launch-due runs twice a minute; remove the "
                f"crontab line `{lines[0]}` (`proc launcher install` does)",
            )
        return sd
    st = _cron_status()
    if st is not None:
        return st
    return Status(None, False, "not installed; run `proc launcher install`")


def install(base: Path) -> Status:
    """Install the systemd user timer, or the cron line when user systemd is
    unavailable. Idempotent; verifies the result and raises LauncherError otherwise."""
    why_not_systemd = _systemd_unavailable()
    if why_not_systemd is None:
        _install_systemd(base)
        _remove_cron_lines()
        st = _systemd_status()
    else:
        st = _install_cron(base, why_not_systemd)
    if st is None or not st.ok:
        raise LauncherError(f"installed but not working: {st.line() if st else 'no status'}")
    return st


def _install_systemd(base: Path) -> None:
    udir = _unit_dir()
    udir.mkdir(parents=True, exist_ok=True)
    (udir / SERVICE).write_text(_service_text(base))
    (udir / TIMER).write_text(TIMER_TEXT)
    for argv in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", TIMER],
    ):
        r = _run(argv)
        if r.returncode != 0:
            raise LauncherError(f"{' '.join(argv)} failed: {(r.stderr or r.stdout).strip()}")


def _remove_cron_lines() -> None:
    """Drop the marked crontab line(s): the timer is the launcher, a second one doubles
    every pass."""
    text = _crontab()
    if not text or CRON_MARKER not in text:
        return
    kept = [ln for ln in text.splitlines() if CRON_MARKER not in ln]
    r = _run(["crontab", "-"], stdin="\n".join(kept) + ("\n" if kept else ""))
    if r.returncode != 0:
        raise LauncherError(f"crontab - failed: {(r.stderr or r.stdout).strip()}")


def _install_cron(base: Path, why_not_systemd: str) -> Status | None:
    current = _crontab()
    if current is None:
        raise LauncherError(f"no launcher backend: {why_not_systemd}; crontab is unusable")
    if not _cron_daemon_running():
        raise LauncherError(f"no launcher backend: {why_not_systemd}; no cron daemon is running")
    kept = [ln for ln in current.splitlines() if CRON_MARKER not in ln]
    r = _run(["crontab", "-"], stdin="\n".join([*kept, _cron_line(base)]) + "\n")
    if r.returncode != 0:
        raise LauncherError(f"crontab - failed: {(r.stderr or r.stdout).strip()}")
    return _cron_status()


def ensure(base: Path) -> Status:
    """Return the working launcher status, installing the launcher first when it is
    missing or broken. Raises LauncherError when no backend works."""
    st = status()
    if st.ok and st.backend == "systemd":
        _upgrade_unit()
    return st if st.ok else install(base)


def uninstall() -> Status:
    """Remove the systemd timer and the cron line, whichever exist."""
    if (_unit_dir() / TIMER).exists():
        r = _run(["systemctl", "--user", "disable", "--now", TIMER])
        if r.returncode != 0:
            raise LauncherError(
                f"systemctl --user disable failed: {(r.stderr or r.stdout).strip()}"
            )
        for unit in (SERVICE, TIMER):
            (_unit_dir() / unit).unlink(missing_ok=True)
        r = _run(["systemctl", "--user", "daemon-reload"])
        if r.returncode != 0:
            raise LauncherError(
                f"systemctl --user daemon-reload failed: {(r.stderr or r.stdout).strip()}"
            )
    _remove_cron_lines()
    return Status(None, False, "removed")
