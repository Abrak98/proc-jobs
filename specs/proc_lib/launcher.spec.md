# launcher — spec

> Permanent contract and source of truth. Edit THIS file directly on any change.
> Every source `.py` implementing it carries `# spec: <path-to-this-file>`.

## Intent
Install, check and remove whatever runs `proc launch-due` once a minute, so that jobs
registered with `proc start --at` actually start. Without a working launcher a scheduled
entry never fires; `start --at` therefore refuses to register one when none can be made to
work.

## Rationale / Key decisions
- **systemd user timer first, cron as the fallback.** A user timer needs no daemon of our
  own and survives reboots (`Persistent=true`). WSL often has no reachable user manager, so a
  `crontab` line (with a running cron daemon) is the one fallback. No third backend.
- **`KillMode=process` on the service, jobs in their own scope.** A launch pass is a oneshot
  unit; a job started in its cgroup would be killed by the default `control-group` mode the
  moment the pass ends, and with `KillMode=process` survives but is logged as a left-over
  process on every unit end. The engine therefore starts each job through `systemd-run --user
  --scope` (see `core.spec.md`), and `KillMode=process` stays only as the fallback for a job
  that could not get a scope.
- **`IOAccounting=yes` on the service.** Without it systemd keeps no disk read/write for the
  unit, and the per-minute pass's disk cost could not be seen.
- **The unit pins `PROC_HOME` and the interpreter that installed it.** The pass must read
  the same registry the installing `proc` used; the command is `<python> -I -m proc_lib.cli
  launch-due`, which needs no `proc` on the timer's PATH.
- **`ensure` checks state, never repoints the unit.** A working launcher is left untouched, so a
  `proc` run from a dev checkout does not repoint a timer that an installed `proc` set up.
  The one edit it makes to a working systemd unit is adding a setting a newer version writes
  (`IOAccounting=yes`) in place, keeping its `ExecStart` and `PROC_HOME`. `install` is the
  explicit, idempotent rewrite.
- **Verify after install.** `install` re-reads the status and raises when the result is not
  `ok`; a timer that is written but not active is a failure, not a success.
- **Helper commands never raise on absence.** `_run` turns a missing binary or a hang
  (30 s cap) into a failed result, so a missing `crontab` or `systemctl` becomes a named
  reason in `LauncherError` instead of a traceback.

## Rejected alternatives
- **A resident scheduler daemon.** Rejected: a second long-lived process to supervise is
  the problem this feature removes.
- **`sleep` inside the job.** Rejected: the original defect.
- **`at`/`atd`.** Rejected: one more daemon, no portable status check.

## Acceptance criteria
- GIVEN user systemd is reachable WHEN `install` runs THEN the service and timer units exist
  under `~/.config/systemd/user`, the timer is enabled and active, and `status()` is `ok`.
- GIVEN user systemd is unreachable and cron is available WHEN `install` runs THEN exactly
  one marked crontab line exists, however often `install` runs, and other lines are kept.
- GIVEN neither backend works WHEN `install` or `ensure` runs THEN `LauncherError` names the
  reason for each backend.
- GIVEN user systemd is reachable and a marked crontab line exists WHEN `install` runs THEN
  the line is removed and other lines are kept; WHEN `status` runs while both are present
  THEN it is not `ok`, names the line and `proc launcher status` exits non-zero.
- GIVEN a working launcher WHEN `ensure` runs THEN nothing is rewritten or restarted, except
  that a service unit without `IOAccounting=` gets that line added in place (daemon-reload),
  its `ExecStart` and `PROC_HOME` untouched.

## State
- `~/.config/systemd/user/proc-launch.service` and `proc-launch.timer` (systemd backend).
- One crontab line ending in `# proc-launch` (cron backend).

## Methods
- `status() -> Status` — which backend is installed and whether it works.
- `install(base) -> Status` — install the available backend and verify it.
- `ensure(base) -> Status` — `status()` if ok (upgrading an older systemd unit in place), else
  `install(base)`.
- `uninstall() -> Status` — remove both backends' artefacts.

## Invariants
- No import from `proc_lib.core` (the engine imports this module, not the reverse).
- Every external command runs through `_run` with a hard timeout.
- Every `systemctl --user` command receives a copy of the caller's environment.
  If `XDG_RUNTIME_DIR` is missing or empty and `/run/user/<uid>` is a directory, use it.
  If `DBUS_SESSION_BUS_ADDRESS` is missing or empty and the runtime directory exists,
  use `unix:path=<runtime directory>/bus`. Preserve supplied nonempty values and the
  caller's environment; a missing directory is not created and a failed connection
  retains the command's diagnostic.
- `uninstall` raises `LauncherError` when disabling or reloading systemd fails,
  preserving the command's diagnostic; a failed disable leaves the unit files intact.

## Formulas
n/a

## I/O contracts
`Status(backend: "systemd" | "cron" | None, ok: bool, detail: str)`; `Status.line()` is the
one-line text `proc launcher` prints.

## Names
| canonical | kind | forbidden-aliases |
|-----------|------|-------------------|
| LauncherError | class | |
| Status | class | |
| ensure | function | |

## Dependencies
- Python standard library only; `systemctl`, `crontab`, `pgrep` as external commands.

## Test cases
- GIVEN a fake runner with systemd available WHEN `install` runs THEN units and enable call
  appear (`test_launcher.py`).
- GIVEN no user systemd WHEN `install` runs twice THEN one cron line (`test_launcher.py`).
- GIVEN no backend WHEN `install` runs THEN `LauncherError` names both reasons
  (`test_launcher.py`).
- GIVEN no launcher WHEN `start --at` runs THEN exit 6 and no registry entry
  (`test_launcher.py`).
- GIVEN a unit written before `IOAccounting` existed WHEN `ensure` runs THEN the line is added
  and `ExecStart`/`PROC_HOME` are kept (`test_launcher.py`).
