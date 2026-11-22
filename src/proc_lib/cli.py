# spec: specs/proc_lib/cli.spec.md
"""proc — argparse front end. Entry point: ``proc = proc_lib.cli:main``.

Thin layer over :mod:`proc_lib.core`; all behaviour lives in the engine.
"""

from __future__ import annotations

import contextlib
import sys

from proc_lib import core

__all__ = ["build_parser", "main"]


def build_parser():
    import argparse  # imported here: the launch-due fast path in main() never builds a parser

    p = argparse.ArgumentParser(
        prog="proc",
        description="Managed background-process CLI. Launch, monitor, log, wait "
        "on, and kill long-running processes through a logged, registry-backed "
        "interface. State in $PROC_HOME (default ~/.local/state/proc/).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser(
        "start",
        help="launch a detached background job",
        description="Launch CMD detached in the background under NAME. "
        "Everything after '--' is the command and its args.",
    )
    sp.add_argument("--name", required=True, help="unique job name")
    sp.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="OS-enforced timeout in seconds (TERM, then KILL after 10s grace)",
    )
    sp.add_argument("--cwd", default=None, help="working directory (default: cwd)")
    sp.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VAL",
        help="set an environment variable for the job (repeatable); recorded in "
        "the registry, values of keys containing TOKEN/PASSWORD/SECRET/KEY masked",
    )
    sp.add_argument(
        "--at",
        default=None,
        metavar="ISO_TIME",
        help="do not start now: register the job to start at this ISO 8601 time "
        "(zone required, e.g. 2026-10-23T13:19:00Z). No process exists until then. "
        "From inside the running job of the same --name it sets that job's next run",
    )
    sp.add_argument(
        "--heavy",
        action="store_true",
        help="register as a heavy job (RAM-intensive); refuses to start if "
        "another heavy job is already running",
    )
    sp.add_argument(
        "--force-concurrent",
        action="store_true",
        help="bypass the heavy-job lock check (use only with explicit operator approval)",
    )
    sp.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="-- CMD [ARGS...] (the command to run)",
    )
    sp.set_defaults(func=core.cmd_start)

    rsp = sub.add_parser(
        "reschedule",
        help="move a scheduled job's start, or set a running job's own next run",
        description="On a scheduled job: change its start time. Called from inside the "
        "running job itself: run the same command again at that time once this run ends.",
    )
    rsp.add_argument("name")
    rsp.add_argument("--at", required=True, metavar="ISO_TIME", help="ISO 8601 time with a zone")
    rsp.set_defaults(func=core.cmd_reschedule)

    ldp = sub.add_parser(
        "launch-due",
        help="start every scheduled job whose time has come (run by the launcher)",
        description="Run once a minute by the launcher backend; starts each due entry "
        "exactly once. Safe to run by hand.",
    )
    ldp.set_defaults(func=core.cmd_launch_due)

    lcp = sub.add_parser(
        "launcher",
        help="install, check or remove the per-minute launcher of scheduled jobs",
        description="The launcher is a systemd user timer (cron when user systemd is "
        "unavailable) that runs `proc launch-due` every minute. `start --at` installs "
        "it when missing; `status` exits 6 when it cannot fire jobs.",
    )
    lcp.add_argument("action", choices=["status", "install", "uninstall"])
    lcp.set_defaults(func=core.cmd_launcher)

    lp = sub.add_parser(
        "list",
        aliases=["ls"],
        help="list live jobs (running and scheduled); --all for history",
        description="Without a filter: the running and scheduled jobs, read from the live "
        "index without opening any finished job's record. `--running` / `--scheduled` "
        "narrow it; `--session ID` lists the jobs a Claude session started, in any state; "
        "`--all` reads the whole registry, finished jobs included (live folder and history); "
        "`--exited --since HOURS` lists the jobs that ended in that window from the live "
        "folder and the history folders it covers.",
    )
    lp.add_argument("--running", action="store_true", help="only running jobs")
    lp.add_argument("--scheduled", action="store_true", help="only scheduled jobs")
    lp.add_argument(
        "--session",
        default=None,
        metavar="ID",
        help="the jobs started by this Claude session (any state; --running/--scheduled narrow)",
    )
    lp.add_argument(
        "--all", action="store_true", help="every job in the registry, finished ones included"
    )
    lp.add_argument(
        "--exited",
        action="store_true",
        help="the jobs that ended in the last --since HOURS, read from the live folder and "
        "the history day folders that window covers",
    )
    lp.add_argument(
        "--since",
        type=float,
        default=None,
        metavar="HOURS",
        help="with --exited: the window, in hours back from now",
    )
    lp.add_argument(
        "--json",
        action="store_true",
        help="print a JSON array of per-job objects: name, status, alive, exit_code, pid, "
        "pid_starttime, owner, start_ts, elapsed_s (scheduled_ts for a scheduled job)",
    )
    lp.add_argument(
        "--full",
        action="store_true",
        help="with --json: the whole record of each job (command, cwd, env, log path; "
        "same shape as `status --json`); reads each listed job's record",
    )
    lp.set_defaults(func=core.cmd_list)

    rip = sub.add_parser(
        "reindex",
        help="rebuild the live index and per-session lists from every record",
        description="The index is kept by every state change and rebuilt automatically "
        "when missing; run this after records were edited or deleted by hand.",
    )
    rip.set_defaults(func=core.cmd_reindex)

    stp = sub.add_parser(
        "status",
        help="print a job's verdict line, then its details",
        description="First line is the verdict (`NAME: running pid=N for 2h13m`, "
        "`NAME: exited rc=0 after 41m`, `NAME: killed (timeout) after 1h0m`), "
        "then the log path and the rest of the metadata.",
    )
    stp.add_argument("name")
    stp.add_argument(
        "--json",
        action="store_true",
        help="print metadata JSON with status/alive/exit_code first",
    )
    stp.set_defaults(func=core.cmd_status)

    lgp = sub.add_parser("logs", help="print or follow a job's combined log")
    lgp.add_argument("name")
    lgp.add_argument("--tail", type=int, default=None, help="show last N lines")
    lgp.add_argument(
        "--since",
        metavar="TS",
        default=None,
        help="only lines after TS (ISO 8601 with a zone); a line is timed by its own "
        "leading time, else by the line above; a log with no timed line is judged by its mtime",
    )
    lgp.add_argument(
        "--follow",
        action="store_true",
        help="tail -f with auto-stop on terminal status or 600s cap",
    )
    lgp.set_defaults(func=core.cmd_logs)

    wp = sub.add_parser(
        "wait",
        help="block until a job reaches terminal status",
        description="Block until NAME is terminal, then print its verdict line. "
        "Exit 0 if it exited rc 0, 5 if it ended any other way (non-zero or "
        "unknown rc, killed, timeout), 3 if still running at the cap, 4 if unknown.",
    )
    wp.add_argument("name")
    wp.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="max seconds to wait (default 3600); exit 3 if still running",
    )
    wp.set_defaults(func=core.cmd_wait)

    kp = sub.add_parser("kill", help="terminate a job (registry-only, PID-guarded)")
    kp.add_argument("name")
    kp.add_argument(
        "--force",
        action="store_true",
        help="send SIGKILL immediately instead of SIGTERM-then-KILL",
    )
    kp.set_defaults(func=core.cmd_kill)

    rp = sub.add_parser("rm", help="delete metadata + log for terminal jobs")
    rp.add_argument("name", nargs="+", help="one or more job names")
    rp.add_argument(
        "--force",
        action="store_true",
        help="also remove (after killing) a running job",
    )
    rp.set_defaults(func=core.cmd_rm)

    arp = sub.add_parser(
        "archive",
        help="move finished records still in the live folder into the history folders",
        description="The first proc call of a UTC day moves finished jobs older than a day to "
        "history/<UTC day of its end>/; this moves every finished record still in the live "
        "folder at once, whatever its age (the one-time move of an old registry). Records "
        "past the retention bound are deleted. Safe to rerun.",
    )
    arp.set_defaults(func=core.cmd_archive)

    cp = sub.add_parser("clean", help="remove terminal jobs (optionally by age)")
    cp.add_argument(
        "--older-than",
        type=float,
        default=None,
        metavar="HOURS",
        help="only remove jobs whose end_ts is older than HOURS (default 24)",
    )
    cp.set_defaults(func=core.cmd_clean)

    hkp = sub.add_parser(
        "hook",
        help="install, check or remove proc's Claude Code hook",
        description="The hook makes a project's own Claude Code Bash checks judge the command "
        "behind `proc start ... -- CMD` as if CMD had been the call. `install` registers it "
        "in the user's Claude settings (idempotent; `start` does it once when missing, "
        "unless `uninstall` was run); `status` exits 7 when it is not registered; `unwrap` is "
        "the hook itself (reads the tool call on stdin).",
    )
    hkp.add_argument("action", choices=["status", "install", "uninstall", "unwrap"])
    hkp.add_argument(
        "--config-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="Claude config dir to edit (repeatable; default $CLAUDE_CONFIG_DIR or ~/.claude)",
    )
    hkp.set_defaults(func=core.cmd_hook)

    return p


def _list_args(rest):
    """The `list` arguments as a namespace, or None when anything is not a plain flag
    (`--help`, an abbreviation, an unknown or malformed one): the full parser then
    handles the call and gives its usual message."""
    flags = {"--running", "--scheduled", "--all", "--json", "--full", "--exited"}
    out = {f[2:]: False for f in flags} | {"session": None, "since": None}
    it = iter(rest)
    for a in it:
        if a in flags:
            out[a[2:]] = True
        elif a == "--session":
            out["session"] = next(it, None)
            if not out["session"] or out["session"].startswith("-"):
                return None
        elif a.startswith("--session=") and len(a) > len("--session="):
            out["session"] = a.split("=", 1)[1]
        elif a == "--since" or a.startswith("--since="):
            raw = a.split("=", 1)[1] if "=" in a else next(it, "")
            try:
                out["since"] = float(raw)
            except ValueError:
                return None
        else:
            return None
    import types

    return types.SimpleNamespace(**out)


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    try:
        if not argv or argv[0] != "hook":  # the hook's own path stays free of any registry read
            core.housekeeping()
        if list(argv) == ["launch-due"]:
            # the per-minute timer's call: skip argparse (about 130 KB of bytecode read
            # on every pass); the full parser still accepts and documents the command
            return core.cmd_launch_due(None)
        if argv and argv[0] in ("list", "ls") and (fast := _list_args(argv[1:])) is not None:
            return core.cmd_list(fast)  # no argparse (about 0.3 MB of bytecode) for a plain list
        args = build_parser().parse_args(argv)
        return args.func(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # downstream pipe (e.g. `| head`) closed; exit quietly
        with contextlib.suppress(OSError):
            sys.stdout.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
