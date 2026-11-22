# spec: specs/proc_lib/core.spec.md
"""proc engine — registry, process/signal logic, and subcommand implementations.

State lives under ``$PROC_HOME`` (default ``~/.local/state/proc``). The base dir is
read once at import; tests isolate it by setting ``PROC_HOME`` before import or
by monkeypatching :data:`BASE`. Every path helper reads :data:`BASE` at call
time, so a monkeypatch takes effect immediately.

Exit codes:
  0  success / terminal reached
  2  usage error or conflict (e.g. duplicate running name, rm on running job)
  3  wait/follow timed out while job still running
  4  named job not found in registry
  5  wait: job reached a terminal status other than `exited` with rc 0
  6  no launcher backend (systemd user timer / cron) can fire scheduled jobs

stdlib-only by design: a process supervisor must not depend on anything that
could itself break the very runs it manages. Modules only some commands need
(subprocess, shutil, launcher, claude_hook, heavy_job_lock) are imported inside the
functions that use them: the minute-by-minute `launch-due` pass reads their bytecode
only when it actually starts a job.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import functools
import json
import os
import re
import shlex
import signal
import sys
import time
import types
from datetime import UTC, datetime
from pathlib import Path

from proc_lib import cost

__all__ = [
    "BASE",
    "ENV_JOB_NAME",
    "ENV_OWNER_PROJECT",
    "ENV_OWNER_SESSION",
    "EXITED",
    "EXIT_HOOK_MISSING",
    "EXIT_JOB_FAILED",
    "EXIT_KILL_SURVIVORS",
    "EXIT_LAUNCHER",
    "EXIT_NOTFOUND",
    "EXIT_USAGE",
    "EXIT_WAIT_TIMEOUT",
    "FOLLOW_HARD_CAP_SECONDS",
    "HISTORY_DAYS_CONFIG",
    "HISTORY_DAYS_DEFAULT",
    "HISTORY_DAYS_ENV",
    "HISTORY_DIR",
    "HISTORY_STAMP",
    "INDEX_VERSION",
    "KILLED",
    "KILL_DRAIN_POLLS",
    "KILL_GRACE_SECONDS",
    "LIVE",
    "LOST",
    "MASKED_VALUE",
    "OVERDUE_WARN_SECONDS",
    "RUNNING",
    "SCHEDULED",
    "SECRET_KEY_MARKERS",
    "TERMINAL",
    "TIMEOUT",
    "TIMEOUT_BIN",
    "cmd_archive",
    "cmd_clean",
    "cmd_hook",
    "cmd_kill",
    "cmd_launch_due",
    "cmd_launcher",
    "cmd_list",
    "cmd_logs",
    "cmd_reindex",
    "cmd_reschedule",
    "cmd_rm",
    "cmd_start",
    "cmd_status",
    "cmd_wait",
    "housekeeping",
]


def _default_base() -> Path:
    return Path(os.environ.get("PROC_HOME") or os.path.expanduser("~/.local/state/proc"))


# Resolved once at import. Overridable via PROC_HOME (isolation/tests) or by
# monkeypatching this module attribute; all path helpers read it at call time.
BASE = _default_base()

# status values
SCHEDULED = "scheduled"  # registered with --at, no process yet; the launcher starts it
RUNNING = "running"
EXITED = "exited"
KILLED = "killed"
TIMEOUT = "timeout"
LOST = "lost"  # legacy-read only — no longer produced (see _reconcile / cmd_kill)
TERMINAL = {EXITED, KILLED, TIMEOUT, LOST}

EXIT_USAGE = 2
EXIT_WAIT_TIMEOUT = 3
EXIT_NOTFOUND = 4
EXIT_JOB_FAILED = 5
EXIT_LAUNCHER = 6
EXIT_HOOK_MISSING = 7
EXIT_KILL_SURVIVORS = 8  # kill: session members still alive after SIGKILL

# Set in every job's environment so a job can recognise itself when it asks to be
# re-scheduled (`proc start --at` / `proc reschedule` from inside the running job).
ENV_JOB_NAME = "PROC_JOB_NAME"
ENV_OWNER_SESSION = "CLAUDE_CODE_SESSION_ID"
ENV_OWNER_PROJECT = "COORD_PROJECT"

TIMEOUT_BIN = "/usr/bin/timeout"

# Grace between SIGTERM and the escalation SIGKILL on `proc kill` (no --force).
KILL_GRACE_SECONDS = 10.0
KILL_DRAIN_POLLS = 50  # SIGKILL re-send polls of 0.1s (~5s ceiling)
# A scheduled entry this far past its time means the launcher is not firing.
OVERDUE_WARN_SECONDS = 180.0
# Hard ceiling for `proc logs --follow` so it never tails forever unattended.
FOLLOW_HARD_CAP_SECONDS = 600.0

# A finished job's record and sidecars stay at `$PROC_HOME/<job>.*` until the first proc call
# of a UTC day after the day following its end moves them into `history/<UTC day of its end>/`;
# the whole folder of a day is deleted once the day is older than the retention bound.
HISTORY_DIR = "history"
HISTORY_STAMP = ".swept"  # holds the UTC day of the last sweep; one read per proc call
HISTORY_DAYS_DEFAULT = 14
HISTORY_DAYS_ENV = "PROC_HISTORY_DAYS"
HISTORY_DAYS_CONFIG = "history_days"
_DAY_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# `--env` keys whose value is masked before it is written to the registry. The
# registry is a plain world-readable JSON dir; the job itself gets the real value
# through Popen's env, so nothing downstream needs the plaintext on disk.
SECRET_KEY_MARKERS = ("TOKEN", "PASSWORD", "SECRET", "KEY")
MASKED_VALUE = "***"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    """UTC ISO8601 from epoch seconds."""
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def _lockfile() -> Path:
    return BASE / ".lock"


def _ensure_base() -> None:
    BASE.mkdir(parents=True, exist_ok=True)


def _meta_path(name: str) -> Path:
    return BASE / f"{name}.json"


def _log_path(name: str) -> Path:
    return BASE / f"{name}.log"


def _rc_path(name: str) -> Path:
    """Sidecar where the wrapper records the job's real exit code."""
    return BASE / f"{name}.rc"


def _schedenv_path(name: str) -> Path:
    """Private (0600) sidecar holding the full environment a scheduled run starts
    with. It is never in the registry JSON: unlike `env` there is no masking, the
    launcher needs the real values."""
    return BASE / f"{name}.schedenv"


# Process states (/proc/<pid>/stat field 3) that mean the process has ended but
# may not be reaped yet. A zombie still has a /proc entry and answers
# os.kill(pid, 0), so we must treat it as NOT alive — otherwise an exited job
# whose parent hasn't reaped it (e.g. proc running in-process) reads as running
# forever. In production `proc start` exits immediately, so the wrapper is
# reparented to init and reaped; this only matters in the brief pre-reap window
# and for in-process use, but skipping Z/X is strictly more correct.
_DEAD_STATES = {"Z", "X", "x"}


def _read_stat(pid: int | None) -> tuple[int | None, str | None]:
    """Return (starttime, state) from /proc/<pid>/stat, or (None, None) if gone.

    starttime is field 22 (clock ticks since boot); state is field 3. The comm
    field (field 2) is wrapped in parentheses and may itself contain spaces or
    ')', so we split on the LAST ')' and index the remaining space-separated
    fields: state is index 0 of the tail, starttime is field 22 -> tail index 19.
    The starttime is the PID-reuse guard: a recycled PID gets a different
    boot-relative starttime, so a mismatch means our process is gone even though
    some process now owns the PID."""
    if pid is None:
        return (None, None)
    try:
        with open(f"/proc/{pid}/stat") as fh:
            data = fh.read()
    except (FileNotFoundError, ProcessLookupError, OSError):
        return (None, None)
    rparen = data.rfind(")")
    if rparen == -1:
        return (None, None)
    tail = data[rparen + 2 :].split()
    state = tail[0] if tail else None
    try:
        return (int(tail[19]), state)
    except (IndexError, ValueError):
        return (None, state)


def _read_starttime(pid: int | None) -> int | None:
    """starttime field of /proc/<pid>/stat, or None if the process is gone."""
    return _read_stat(pid)[0]


def _capture_starttime(pid: int, attempts: int = 6, delay: float = 0.01):
    """Best-effort read of a freshly launched pid's starttime.

    A job that lives more than a few ms always yields a value; we retry briefly
    so a transient /proc read race at launch doesn't leave starttime=None on a
    process that is actually alive (which would defeat the PID-reuse guard —
    see _alive). A job that exits faster than this window legitimately returns
    None and is finalized via its .rc sidecar on the next reconcile."""
    for _ in range(attempts):
        st = _read_starttime(pid)
        if st is not None:
            return st
        time.sleep(delay)
    return None


def _alive(pid: int | None, starttime: int | None) -> bool:
    """True iff `pid` is alive AND its /proc starttime matches the stored one.

    PID-reuse guard. If we never captured a starttime (`starttime is None`) we
    deliberately return False: we cannot prove the live PID is *our* process, so
    for signaling safety we treat it as not-ours and let reconcile finalize the
    job from its .rc sidecar. In practice starttime is None only for a job that
    exited within milliseconds of launch (see _capture_starttime), which is
    genuinely terminal, so this is also correct for liveness."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # exists but owned by someone else — confirm identity via starttime below
        pass
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
    cur, state = _read_stat(pid)
    if cur is None:
        return False
    if state in _DEAD_STATES:
        # zombie/dead: the process has exited, it just hasn't been reaped yet
        return False
    if starttime is None:
        # can't confirm this live PID is ours -> not-our-process (safe default)
        return False
    return cur == starttime


def _session_members(sid: int | None) -> list[int]:
    """All live PIDs belonging to the session led by `sid`, via a /proc scan.

    Why session, not process-group: we launch the job with
    start_new_session=True, so the bash wrapper becomes the session leader and
    its pid == sid. coreutils `timeout` then puts the real command into its OWN
    process group (setpgid) — so the recorded pgid (the wrapper's) goes empty
    the instant the wrapper dies, while timeout+worker live on in a different
    group that killpg(recorded_pgid) never reaches. A descendant PPID-walk
    doesn't help either: when the wrapper dies the worker is reparented to init
    and the tree breaks. The session id, by contrast, is inherited by every
    descendant, is NOT changed by reparenting, and is NOT forked off by
    `timeout`. So scanning /proc for session==sid reliably finds the whole job
    (wrapper + timeout + worker), which is what kill/escalation must signal."""
    if not sid or sid <= 0:
        return []
    members = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as fh:
                data = fh.read()
            # fields after the "(comm)" token: state ppid pgrp session ...
            after = data.rsplit(")", 1)[1].split()
            if after[0] in _DEAD_STATES:
                continue  # zombie/dead: exited, don't count as a live member
            if int(after[3]) == sid:  # after[3] == session id
                members.append(int(entry))
        except (OSError, ValueError, IndexError):
            continue
    return members


def _session_alive(sid: int | None) -> bool:
    """True if any process still belongs to the session led by `sid`."""
    return bool(_session_members(sid))


def _signal_session(sid: int | None, sig: int) -> int:
    """Send `sig` to every live member of the session led by `sid`.

    Signals each pid individually rather than killpg, because `timeout` forks
    the worker into a separate process group — one killpg can't reach every
    member. Returns the number of processes signaled."""
    n = 0
    for p in _session_members(sid):
        try:
            os.kill(p, sig)
            n += 1
        except (ProcessLookupError, PermissionError):
            pass
    return n


def _read_json(p: Path):
    try:
        with open(p) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def _load(name: str):
    """A job's record: the live folder first, else its daily history folder."""
    m = _read_json(_meta_path(name))
    if m is not None:
        return m
    hp = _history_record(name)
    return _read_json(hp) if hp is not None else None


# --------------------------------------------------------------------------- #
# history: finished jobs by UTC day of their end
# --------------------------------------------------------------------------- #
def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def _history_root() -> Path:
    return BASE / HISTORY_DIR


def _history_days() -> list[str]:
    """The day folders present, oldest first."""
    try:
        with os.scandir(_history_root()) as it:
            return sorted(e.name for e in it if _DAY_NAME.match(e.name) and e.is_dir())
    except OSError:
        return []


def _history_record(name: str) -> Path | None:
    """The newest history record of `name` (at most the retention bound of stats)."""
    for day in reversed(_history_days()):
        p = _history_root() / day / f"{name}.json"
        if p.exists():
            return p
    return None


def _history_days_bound() -> int:
    """The retention bound in days. The user's setting (`PROC_HISTORY_DAYS`, else
    `history_days` in `~/.config/proc`) applies to the default registry only; any other
    home reads `<home>/history_days` alone, like the cost alert's command."""
    if cost._is_default_home(BASE):
        text = os.environ.get(HISTORY_DAYS_ENV, "").strip()
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"))
        cfg = cfg / "proc" / HISTORY_DAYS_CONFIG
    else:
        text = ""
        cfg = BASE / HISTORY_DAYS_CONFIG
    if not text:
        try:
            text = next(
                (
                    ln.strip()
                    for ln in cfg.read_text().splitlines()
                    if ln.strip()[:1] not in ("", "#")
                ),
                "",
            )
        except OSError:
            text = ""
    try:
        days = int(text)
    except ValueError:
        return HISTORY_DAYS_DEFAULT
    return days if days >= 1 else HISTORY_DAYS_DEFAULT


def housekeeping(now: float | None = None) -> None:
    """The first proc call of a UTC day moves the finished jobs that ended before the
    previous UTC day began into their history day folders (`_archive`, the lock held per
    record) and deletes the day folders older than the retention bound, whole (no per-file
    sweep, no timer). So a finished job keeps its flat path for at least 24 h and the live
    folder holds about two days of finished jobs. The stamp is taken before the sweep, so a
    concurrent call skips it; a sweep that dies is finished by the next day's. A registry
    that does not exist yet is left untouched. Never raises: housekeeping is not a reason
    for a command to fail."""
    root = _history_root()
    try:
        if not BASE.is_dir():
            return
        t = now if now is not None else _now()
        today = _day(t)
        stamp = root / HISTORY_STAMP
        try:
            if stamp.read_text() == today:
                return
        except OSError:
            pass
        root.mkdir(exist_ok=True)
        tmp = stamp.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(today)
        os.replace(tmp, stamp)
        cutoff = _day(t - _history_days_bound() * 86400)
        import shutil

        for day in _history_days():
            if day < cutoff:
                shutil.rmtree(root / day, ignore_errors=True)
        _move_ended_before(_day(t - 86400))
    except OSError:
        return


def _move_ended_before(first_kept_day: str) -> None:
    """Move every finished flat job whose end day is earlier than `first_kept_day` into
    its history folder. Each record is re-read under the lock and moved only if it is
    still finished and still that old."""
    with os.scandir(BASE) as it:
        names = sorted(
            e.name[:-5]
            for e in it
            if e.name.endswith(".json") and not e.name.startswith(".") and e.is_file()
        )

    def one(name: str) -> None:
        m = _read_json(_meta_path(name))
        if not m or m.get("name") != name or m.get("status") not in TERMINAL:
            return
        if _day(m.get("end_ts") or m.get("start_ts") or _now()) < first_kept_day:
            _archive(m)

    for name in names:
        _with_lock(lambda name=name: one(name))


def _archive(meta: dict) -> None:
    """Move a finished job into its history day folder (the daily sweep and `proc archive`
    call this, never a job's end): its log, `.rc` and `.rss` first,
    then the record (written there with the new paths), then the live record is removed.
    A crash between the last two leaves the record in both places; the live copy wins and
    the next archive of the job (a save, or `proc archive`) finishes the move. A job whose
    end already lies beyond the retention bound is deleted instead, so the one-time
    migration of an old registry also prunes it. Caller holds the lock."""
    name = meta["name"]
    end = meta.get("end_ts") or meta.get("start_ts") or _now()
    day = _day(end)
    live = {"log_path": _log_path(name), "rc_path": _rc_path(name)}
    sidecars = [*live.values(), _rss_path(name)]
    if day < _day(_now() - _history_days_bound() * 86400):
        for f in (*sidecars, _meta_path(name), _schedenv_path(name)):
            with contextlib.suppress(FileNotFoundError):
                f.unlink()
        return
    dest = _history_root() / day
    dest.mkdir(parents=True, exist_ok=True)
    # a copy already in another day folder (the end time of a finished record was changed)
    # comes along: its files move here and the copy goes, so one job has one record
    prev = _history_record(name)
    prev_home = prev.parent if prev is not None and prev.parent != dest else None
    for home in ([prev_home] if prev_home else []) + [BASE]:
        for ext in ("log", "rc", "rss"):
            with contextlib.suppress(FileNotFoundError):
                os.replace(home / f"{name}.{ext}", dest / f"{name}.{ext}")
    for key, f in live.items():
        cur = meta.get(key)
        if cur is None or Path(cur) in (f, *([prev_home / f.name] if prev_home else [])):
            meta[key] = str(dest / f.name)  # a path set elsewhere is not ours to repoint
    p = dest / f"{name}.json"
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w") as fh:
        json.dump(meta, fh, indent=2)
    os.replace(tmp, p)
    with contextlib.suppress(FileNotFoundError):
        _meta_path(name).unlink()
    if prev_home:
        with contextlib.suppress(FileNotFoundError):
            (prev_home / f"{name}.json").unlink()


def _save(meta: dict) -> None:
    """Write a job's record and keep the live index in step with it. Caller holds the
    registry lock.

    Order is what makes the pair crash-safe: a job that becomes live (or changes
    while live) is put in the index BEFORE its record is written, a job that stops
    being live leaves the index AFTER its record is written. A crash between the two
    writes therefore leaves at worst a finished job still listed as live (the next
    reconcile removes it), never a live job the index does not know. A finished job's
    record stays in the live folder; the daily sweep moves it later (`housekeeping`)."""
    name = meta["name"]
    entry = _live_entry(meta)
    if entry is not None:
        _index_set(name, entry)
    p = _meta_path(name)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w") as fh:
        json.dump(meta, fh, indent=2)
    os.replace(tmp, p)
    if entry is None:
        _index_set(name, None)


def _all_metas(history: bool = False):
    """Every record in the live folder, plus with `history` every record of the history
    folders (a live copy wins over a history copy of the same job). Reads all of them:
    only `list --all`, `clean` and the index rebuild call it; no routine path does."""
    metas: list[dict] = []
    if not BASE.exists():
        return metas
    seen: set[str] = set()
    sources = [BASE]
    if history:
        sources += [_history_root() / d for d in reversed(_history_days())]
    for src in sources:
        for p in sorted(src.glob("*.json")):
            m = _read_json(p)
            if m is None or m.get("name") in seen:
                continue
            seen.add(m.get("name"))
            metas.append(m)
    return metas


# --------------------------------------------------------------------------- #
# live index: running and scheduled jobs (+ jobs per starting session)
# --------------------------------------------------------------------------- #
# `.live-index` (not *.json, so a reader globbing the registry for records never
# takes it for one) maps each running or scheduled job name to the few fields the
# routine paths need: status, scheduled_ts, pid + starttime (liveness without
# opening the record), whether it asked for a next run, start time and owner (what
# `list --json` shows, so a live listing never opens a record). It is
# replaced atomically (os.replace) and written only under the registry lock by
# `_save`, so it can be read without the lock. `sessions/<id>` is an append-only
# list of the job names a Claude session started, in any state.
INDEX_VERSION = 2
LIVE = (RUNNING, SCHEDULED)


def _index_path() -> Path:
    return BASE / ".live-index"


def _sessions_dir() -> Path:
    return BASE / "sessions"


def _live_entry(meta: dict) -> dict | None:
    """The index entry of a record, or None when the job is not live."""
    status = meta.get("status")
    if status not in LIVE:
        return None
    entry: dict = {"status": status}
    if status == SCHEDULED:
        entry["scheduled_ts"] = meta.get("scheduled_ts")
    else:
        entry["pid"] = meta.get("pid")
        entry["st"] = meta.get("pid_starttime")
        entry["start"] = meta.get("start_ts")
        if meta.get("next_run"):
            entry["nxt"] = True
    if meta.get("owner"):
        entry["owner"] = meta["owner"]
    return entry


def _read_index() -> dict | None:
    """The index's job map, or None when it is missing, unreadable or of another
    version (the caller then rebuilds it under the lock). Lock-free."""
    try:
        with open(_index_path()) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
        return None
    jobs = data.get("jobs")
    return jobs if isinstance(jobs, dict) else None


def _write_index(jobs: dict) -> None:
    p = _index_path()
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w") as fh:
        json.dump({"version": INDEX_VERSION, "jobs": jobs}, fh, separators=(",", ":"))
    os.replace(tmp, p)


def _safe_session(session: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session)[:200]
    return "_" + safe if safe.startswith(".") else safe


def _write_session_file(session: str, names: list[str]) -> None:
    d = _sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / _safe_session(session)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("".join(f"{n}\n" for n in names))
    os.replace(tmp, p)


def _rebuild_index() -> dict:
    """Rebuild the index (and the per-session lists) from every record: the one full
    scan, run when the index is missing or by `proc reindex`. Caller holds the lock."""
    jobs: dict = {}
    sessions: dict[str, list[str]] = {}
    for m in _all_metas(history=True):
        name = m.get("name")
        if not name:
            continue
        entry = _live_entry(m)
        if entry is not None:
            jobs[name] = entry
        session = (m.get("owner") or {}).get("session")
        if session:
            sessions.setdefault(session, []).append(name)
    _write_index(jobs)
    for session, names in sessions.items():
        _write_session_file(session, names)
    return jobs


def _index_locked() -> dict:
    """The index job map, rebuilt first when missing. Caller holds the lock."""
    jobs = _read_index()
    return jobs if jobs is not None else _rebuild_index()


def _index_set(name: str, entry: dict | None) -> None:
    """Put `name` in the index (or take it out when `entry` is None). Caller holds
    the lock."""
    jobs = _index_locked()
    if entry is None:
        if name not in jobs:
            return
        del jobs[name]
    else:
        if jobs.get(name) == entry:
            return
        jobs[name] = entry
    _write_index(jobs)


def _live_index() -> dict:
    """The live job map without taking the lock (rebuilt under it when missing)."""
    jobs = _read_index()
    return jobs if jobs is not None else _with_lock(_index_locked)


def _session_add(session: str, name: str) -> None:
    """Record that `session` started job `name`. One O_APPEND write of a short line,
    atomic without the lock."""
    d = _sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(d / _safe_session(session)), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, f"{name}\n".encode())
    finally:
        os.close(fd)


def _session_names(session: str) -> list[str]:
    try:
        text = (_sessions_dir() / _safe_session(session)).read_text()
    except OSError:
        return []
    return list(dict.fromkeys(ln for ln in text.splitlines() if ln))


# Seconds this process spent waiting for, and holding, the registry lock; the
# launch-due cost line reports them.
_LOCK_SECONDS = {"wait": 0.0, "hold": 0.0}


def _with_lock(fn):
    """Run fn() while holding an exclusive flock on the registry lockfile, so
    parallel proc invocations don't corrupt JSON. The lockfile is created on
    demand and never deleted (cheap, avoids unlink/recreate races)."""
    _ensure_base()
    lock_fd = os.open(str(_lockfile()), os.O_CREAT | os.O_RDWR, 0o644)
    asked = time.monotonic()
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        got = time.monotonic()
        _LOCK_SECONDS["wait"] += got - asked
        try:
            return fn()
        finally:
            _LOCK_SECONDS["hold"] += time.monotonic() - got
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


# `times` (a bash builtin) prints two lines of "<m>m<s>s <m>m<s>s": the shell's own
# user/system CPU, then that of its reaped children, which is the job.
_TIMES_LINE = re.compile(r"^(\d+)m([\d.,]+)s (\d+)m([\d.,]+)s$")
# /proc/<wrapper>/io counters the wrapper copies into `.rc` (the kernel folds a reaped
# child's counters into its parent's).
_IO_KEYS = ("rchar", "wchar", "syscr", "syscw", "read_bytes", "write_bytes")


# Peak resident memory of the job, which only its reaping parent can read (wait4 rusage).
# The wrapper runs the job under GNU `time -f 'maxrss %M'` (kB) when it is installed and
# folds the figure into `.rc` as `maxrss_kb <n>`; otherwise, or when `time` was killed
# before it reported, it writes `maxrss_note <why>` so `status` can say why it is absent.
_RSS_NOTE_NO_TIME = "GNU time is not installed"
_RSS_NOTE_NO_FIGURE = "GNU time left no figure, killed before it could report"
_RSS_NOTE_OLD = "job started before peak RSS was recorded"


def _rss_path(name: str) -> Path:
    """Side file GNU time writes the job's peak RSS to; the wrapper folds it into `.rc`
    and removes it (never the job's log)."""
    return BASE / f"{name}.rss"


@functools.lru_cache(maxsize=1)
def _gnu_time() -> str | None:
    """Path of GNU time (`/usr/bin/time`, 27 KB, about 1 MB resident per job) or None.
    Probed once per process: a non-GNU `time` (busybox) has no `-f`/`-o` and would put
    its usage message into the job's log."""
    import shutil
    import subprocess

    path = "/usr/bin/time" if os.access("/usr/bin/time", os.X_OK) else shutil.which("time")
    if not path:
        return None
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return path if "GNU" in (out.stdout + out.stderr) else None


def _seconds(minutes: str, seconds: str) -> float:
    return round(int(minutes) * 60 + float(seconds.replace(",", ".")), 3)


def _parse_rc(text: str) -> tuple[int | None, dict | None]:
    """Exit code (first line) and cost figures from the text of a `.rc` sidecar. An old
    one holds the code alone."""
    lines = text.splitlines()
    try:
        rc = int(lines[0].strip()) if lines and lines[0].strip() else None
    except ValueError:
        rc = None
    cost: dict = {}
    times = [m for ln in lines[1:] if (m := _TIMES_LINE.match(ln.strip()))]
    if len(times) >= 2:
        child = times[1]
        cost["cpu_user_s"] = _seconds(child.group(1), child.group(2))
        cost["cpu_sys_s"] = _seconds(child.group(3), child.group(4))
    for ln in lines[1:]:
        if ln.startswith("io "):
            key, _, val = ln[3:].partition(":")
            if key in _IO_KEYS and val.strip().isdigit():
                cost[key] = int(val)
        elif ln.startswith("maxrss_kb ") and ln[10:].strip().isdigit():
            cost["maxrss_kb"] = int(ln[10:])
        elif ln.startswith("maxrss_note "):
            cost["maxrss_note"] = ln[12:].strip()
    return rc, cost or None


def _read_rc_file(meta: dict) -> tuple[int | None, dict | None]:
    """(exit code, cost) the wrapper recorded in the .rc sidecar; (None, None) if it
    isn't there yet / unreadable."""
    rp = meta.get("rc_path") or str(_rc_path(meta.get("name", "")))
    try:
        with open(rp) as fh:
            return _parse_rc(fh.read())
    except (FileNotFoundError, OSError):
        return (None, None)


def _read_rc(meta: dict):
    """Read the exit code the wrapper recorded in the .rc sidecar, or None if
    it isn't there yet / unreadable."""
    return _read_rc_file(meta)[0]


def _reconcile(meta: dict) -> dict:
    """Bring a job's recorded status in line with reality.

    For detached processes we are not the parent across separate proc
    invocations, so we cannot reap an exit code via waitpid. Liveness is
    probed via os.kill(pid,0) + the /proc starttime guard. When a RUNNING job
    is no longer alive we consult the .rc sidecar the wrapper wrote:
      - rc 124 on a job that had a --timeout  -> status `timeout`
      - any other rc                          -> status `exited`, exit_code=rc
      - no rc file (e.g. SIGKILL beat the wrapper) -> `exited`, exit_code unknown
    Already-terminal jobs are returned untouched. This single code path also
    covers PID reuse (starttime mismatch -> not alive -> finalized here),
    recovering the real exit code instead of discarding it: that retires the
    old `lost` status, which used to be set by cmd_kill and lost the .rc code.
    """
    if meta.get("status") != RUNNING:
        return meta
    if _alive(meta.get("pid"), meta.get("pid_starttime")):
        return meta
    rc, job_cost = _read_rc_file(meta)
    if job_cost:
        meta["cost"] = job_cost
    if rc == 124 and meta.get("timeout") is not None:
        meta["status"] = TIMEOUT
        meta["exit_code"] = rc
    else:
        meta["status"] = EXITED
        if rc is not None:
            meta["exit_code"] = rc
    if meta.get("end_ts") is None:
        meta["end_ts"] = _now()
    if meta.get("next_run"):
        _apply_next_run(meta)
    return meta


def _reconcile_and_save(name: str):
    """Load + reconcile + persist if changed. Caller must hold the lock."""
    m = _load(name)
    if m is None:
        _index_set(name, None)  # the record is gone: drop a stale index entry
        return None
    before = m.get("status")
    _reconcile(m)
    if m.get("status") != before:
        _save(m)
    else:
        _index_set(name, _live_entry(m))  # also drops an entry a crash left behind
    return m


def _current(name: str):
    """A job's up-to-date record. The common read (job missing, scheduled, running and
    alive, or already finished) opens one record and takes no lock; only a record
    still marked running whose process has gone is finalised, under the lock."""
    m = _load(name)
    if (
        m is not None
        and m.get("status") == RUNNING
        and not _alive(m.get("pid"), m.get("pid_starttime"))
    ):
        return _with_lock(lambda: _reconcile_and_save(name))
    return m


def _elapsed(meta: dict) -> str:
    secs = _elapsed_seconds(meta)
    if secs is None:
        return "-"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def _cmd_str(meta: dict) -> str:
    return " ".join(meta.get("cmd") or [])


def _elapsed_seconds(meta: dict) -> int | None:
    start = meta.get("start_ts")
    if start is None:
        return None
    end = meta.get("end_ts") if meta.get("status") in TERMINAL else None
    return max(0, int((end if end else _now()) - start))


def _human_duration(secs: int) -> str:
    """Two most significant units, unpadded: 12s, 41m5s, 2h13m, 3d4h."""
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


def _verdict(meta: dict) -> str:
    """One-line state summary, always the FIRST line of human status/wait output
    so a caller that truncates (`| head -1`) still gets the fact it needed."""
    name = meta.get("name", "?")
    status = meta.get("status", "?")
    if status == SCHEDULED:
        return f"{name}: scheduled for {_iso_short(meta['scheduled_ts'])} ({_until(meta)})"
    if status == KILLED and meta.get("start_ts") is None:
        return f"{name}: cancelled before start"
    secs = _elapsed_seconds(meta)
    dur = "?" if secs is None else _human_duration(secs)
    if status == RUNNING:
        return f"{name}: running pid={meta.get('pid')} for {dur}"
    rc = meta.get("exit_code")
    rc_s = "?" if rc is None else str(rc)
    if status == EXITED:
        return f"{name}: exited rc={rc_s} after {dur}"
    if status == TIMEOUT:
        return f"{name}: killed (timeout) after {dur}"
    if status == KILLED:
        return f"{name}: killed (proc kill) after {dur}"
    return f"{name}: {status} after {dur}"


def _iso_short(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _until(meta: dict) -> str:
    """`in 9h26m` before a scheduled start, `overdue 3m5s` after it."""
    delta = int(meta["scheduled_ts"] - _now())
    return f"in {_human_duration(delta)}" if delta >= 0 else f"overdue {_human_duration(-delta)}"


def _job_succeeded(meta: dict) -> bool:
    return meta.get("status") == EXITED and meta.get("exit_code") == 0


def _status_json(meta: dict) -> dict:
    """Machine view of a reconciled job: state/rc/alive first, then every stored
    registry field unchanged. `alive` is derived — after _reconcile a job is
    RUNNING iff its PID-reuse-guarded process is still alive."""
    head = {
        "name": meta.get("name"),
        "status": meta.get("status"),
        "alive": meta.get("status") == RUNNING,
        "exit_code": meta.get("exit_code"),
        "pid": meta.get("pid"),
        "elapsed_s": _elapsed_seconds(meta),
        "log_path": meta.get("log_path"),
    }
    return head | {k: v for k, v in meta.items() if k not in head}


def _list_json(meta: dict) -> dict:
    """What `list --json` shows by default: what a caller filters on and displays, never
    the command texts, environment or paths (those are `--full`)."""
    row = {
        "name": meta.get("name"),
        "status": meta.get("status"),
        "alive": meta.get("status") == RUNNING,
        "exit_code": meta.get("exit_code"),
        "pid": meta.get("pid"),
        "pid_starttime": meta.get("pid_starttime"),
        "owner": meta.get("owner") or {},
        "start_ts": meta.get("start_ts"),
        "elapsed_s": _elapsed_seconds(meta),
    }
    if meta.get("status") == SCHEDULED:
        row["scheduled_ts"] = meta.get("scheduled_ts")
    return row


def _status_lines(meta: dict) -> list[str]:
    lines = [_verdict(meta), f"  log: {meta.get('log_path')}"]
    lines.append(f"  cmd: {shlex.join(meta.get('cmd') or [])}")
    lines.append(f"  cwd: {meta.get('cwd')}")
    if meta.get("status") == SCHEDULED and meta["scheduled_ts"] < _now() - OVERDUE_WARN_SECONDS:
        lines.append("  overdue: the launcher has not started it; check `proc launcher status`")
    if meta.get("owner"):
        lines.append("  owner: " + " ".join(f"{k}={v}" for k, v in meta["owner"].items()))
    if meta.get("start_iso"):
        lines.append(f"  started: {meta['start_iso']}")
    if meta.get("next_run"):
        lines.append(f"  next run: {_iso_short(meta['next_run']['at_ts'])}")
    if meta.get("end_ts") is not None:
        lines.append(f"  ended: {_iso(meta['end_ts'])}")
    to = meta.get("timeout")
    lines.append(f"  timeout: {'none' if to is None else f'{to}s'}")
    job_env = meta.get("env") or {}
    if job_env:
        lines.append("  env: " + " ".join(f"{k}={v}" for k, v in job_env.items()))
    lines.append(f"  status: {meta.get('status')}  exit_code: {meta.get('exit_code')}")
    if meta.get("cost"):
        lines.append("  cost: " + _cost_text(meta["cost"]))
    return lines


def _cost_text(c: dict) -> str:
    parts = []
    if "cpu_user_s" in c:
        parts.append(f"cpu user {c['cpu_user_s']}s sys {c.get('cpu_sys_s', 0)}s")
    for label, key in (
        ("rchar", "rchar"),
        ("read from disk", "read_bytes"),
        ("wchar", "wchar"),
        ("written to disk", "write_bytes"),
    ):
        if key in c:
            parts.append(f"{label} {c[key] / 1048576:.1f} MB")
    if "maxrss_kb" in c:
        parts.append(f"peak RSS {c['maxrss_kb'] / 1024:.1f} MB")
    else:
        parts.append(f"peak RSS not recorded ({c.get('maxrss_note') or _RSS_NOTE_OLD})")
    return ", ".join(parts)


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in SECRET_KEY_MARKERS)


def _parse_env(pairs: list[str]) -> dict[str, str]:
    """`KEY=VAL` strings -> ordered dict; raises ValueError naming the bad pair."""
    parsed: dict[str, str] = {}
    for pair in pairs:
        key, sep, val = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"--env expects KEY=VAL, got {pair!r}")
        if not (key[0].isalpha() or key[0] == "_") or not all(c.isalnum() or c == "_" for c in key):
            raise ValueError(f"--env key must match [A-Za-z_][A-Za-z0-9_]*, got {key!r}")
        parsed[key] = val
    return parsed


def _masked_env(job_env: dict[str, str]) -> dict[str, str]:
    return {k: (MASKED_VALUE if _is_secret_key(k) else v) for k, v in job_env.items()}


def _owner_from_env() -> dict[str, str]:
    """Who is starting a job, read from the caller's environment: the agent session
    and the project. Only variables that are set and non-empty are recorded; an
    empty dict means the caller declared no owner, and none is guessed."""
    owner = {}
    for key, var in (("session", ENV_OWNER_SESSION), ("project", ENV_OWNER_PROJECT)):
        if os.environ.get(var):
            owner[key] = os.environ[var]
    return owner


def _err(msg: str) -> None:
    print(f"proc: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #
class _LaunchError(Exception):
    """A job could not be spawned; the message is operator-facing."""


def _parse_zoned(text: str, flag: str = "--at") -> float:
    """Epoch seconds from an ISO 8601 time that carries its own zone. A naive time
    is refused: the host zone and the caller's zone can differ, and a job that
    starts hours off is worse than a refused command."""
    msg = (
        f"{flag} expects an ISO 8601 time with a zone (2026-10-23T13:19:00Z, ...+03:00), "
        f"got {text!r}"
    )
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(msg) from None
    if when.tzinfo is None:
        raise ValueError(msg)
    return when.timestamp()


def _parse_at(text: str) -> float:
    return _parse_zoned(text, "--at")


# A log line is timed only by a leading ISO 8601 time that carries a zone.
_LINE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})")


def _line_time(line: str) -> float | None:
    mt = _LINE_TIME.match(line)
    if mt is None:
        return None
    try:
        return _parse_zoned(mt.group(0))
    except ValueError:
        return None


def _since_filter(lines: list[str], since: float, mtime: float) -> list[str]:
    """Lines strictly after `since`. A line is timed by its own leading time, else by
    the nearest timed line above it; lines above the first timed line are dropped.
    A log with no timed line is judged whole by the file's mtime."""
    times = [_line_time(ln) for ln in lines]
    if all(t is None for t in times):
        return lines if mtime > since else []
    kept: list[str] = []
    cur: float | None = None
    for ln, t in zip(lines, times, strict=True):
        if t is not None:
            cur = t
        if cur is not None and cur > since:
            kept.append(ln)
    return kept


def _write_schedenv(name: str, env: dict[str, str]) -> None:
    path = _schedenv_path(name)
    tmp = path.with_suffix(".schedenv.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(env, fh)
    os.replace(tmp, path)


def _read_schedenv(name: str) -> dict[str, str]:
    try:
        with open(_schedenv_path(name)) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise _LaunchError(f"scheduled environment of '{name}' is unreadable: {e}") from e


def _drop_schedenv(name: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        _schedenv_path(name).unlink()


def _run_spec(cmd, cwd, timeout, env, heavy, force_concurrent, at_ts) -> dict:
    """The part of a registry entry that describes one future run."""
    return {
        "cmd": list(cmd),
        "cwd": cwd,
        "timeout": timeout,
        "env": env,
        "heavy": heavy,
        "force_concurrent": force_concurrent,
        "scheduled_ts": at_ts,
        "scheduled_iso": _iso(at_ts),
    }


def _apply_next_run(meta: dict) -> None:
    """A running job asked to run again later and has now ended: turn its entry
    into a scheduled one. The log is kept and keeps growing across runs."""
    nxt = meta.pop("next_run")
    meta.update(nxt["spec"])
    meta.update(
        status=SCHEDULED,
        pid=None,
        pgid=None,
        pid_starttime=None,
        start_ts=None,
        start_iso=None,
        end_ts=None,
        exit_code=None,
    )


def _set_next_run(meta: dict, spec: dict, full_env: dict[str, str]) -> None:
    """Record, on a RUNNING job's own entry, the run that follows it. Caller holds
    the lock and saves."""
    meta["next_run"] = {"at_ts": spec["scheduled_ts"], "spec": spec}
    _write_schedenv(meta["name"], full_env)


_SCOPE_OK: bool | None = None  # outcome of the first scope probe in this process


def _scope_works() -> bool:
    """True when `systemd-run --user --scope` can start a command here. Probed once per
    process, only when a job is about to start, with a hard timeout: a launcher running
    where the user manager or bus is unreachable then starts jobs unscoped instead of
    losing them to a systemd-run that exits before running anything."""
    global _SCOPE_OK
    if _SCOPE_OK is None:
        import subprocess

        try:
            r = subprocess.run(
                ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", "true"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            _SCOPE_OK = r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _SCOPE_OK = False
    return _SCOPE_OK


def _scope_prefix(name: str) -> list[str]:
    """`systemd-run` words that move the job into its own transient scope, or [] when
    the caller is not a systemd unit (or has no systemd-run). A job the launcher's
    unit starts would otherwise stay in that unit's cgroup, and systemd would log it
    as a left-over process every time the unit ends. systemd-run --scope execs the
    command in the same process, so the pid, starttime and session the registry
    records are the job's own."""
    import shutil

    if not os.environ.get("INVOCATION_ID") or not shutil.which("systemd-run"):
        return []
    if not _scope_works():
        return []
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:80]
    unit = f"proc-job-{safe}-{int(_now() * 1000)}"
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}", "--"]


def _wrapper_script(
    quoted: str, rcp: Path, time_bin: str | None = None, rssp: Path | None = None
) -> str:
    """The bash payload around a job. After the job is reaped it writes the job's exit
    code (first line) and its cost into the `.rc` sidecar: `times` (the CPU of its
    reaped children), this shell's own /proc/<pid>/io, which the kernel has folded
    the reaped job's counters into, and the peak RSS. Bash cannot read the latter, so
    with `time_bin` (GNU time) the job runs under it, its figure goes to the side file
    `rssp` (never the job's log) and is folded in as `maxrss_kb`; the exit code is
    unchanged (`time` exits with the job's code, 128+n for a signal, as bash does).
    Builtins only, apart from `time` itself and one `rm` of the side file."""
    rc_file = shlex.quote(str(rcp))
    run = quoted
    fold = f"  echo {shlex.quote('maxrss_note ' + _RSS_NOTE_NO_TIME)}\n"
    if time_bin and rssp is not None:
        rss_file = shlex.quote(str(rssp))
        run = f"{shlex.quote(time_bin)} -f 'maxrss %M' -o {rss_file} {quoted}"
        fold = (
            "  mr=\n"
            f"  if [ -r {rss_file} ]; then\n"
            "    while IFS=' ' read -r k v; do\n"
            '      [ "$k" = maxrss ] && mr=$v\n'
            f"    done < {rss_file}\n"
            f"    rm -f {rss_file}\n"
            "  fi\n"
            '  case "$mr" in\n'
            '    ""|*[!0-9]*) echo ' + shlex.quote("maxrss_note " + _RSS_NOTE_NO_FIGURE) + " ;;\n"
            '    *) echo "maxrss_kb $mr" ;;\n'
            "  esac\n"
        )
    return (
        f"{run}\n"
        "rc=$?\n"
        '{ echo "$rc"; times\n'
        "  if [ -r /proc/$$/io ]; then\n"
        '    while IFS= read -r l; do echo "io $l"; done < /proc/$$/io\n'
        "  fi\n"
        f"{fold}"
        f"}} > {rc_file}\n"
        'exit "$rc"'
    )


def _spawn(name, cmd, cwd, timeout, heavy, full_env, recorded_env, scope=False) -> dict:
    """Start `cmd` detached and return its RUNNING registry entry (unsaved).

    Shared by `start` (now) and the launcher (at the scheduled time), so a job
    behaves identically however it was started. `scope` puts it in its own systemd
    scope when the caller runs inside a unit (the launcher does). Raises
    _LaunchError. Caller holds the registry lock and saves the entry."""
    # Inner command, optionally wrapped in coreutils `timeout` so the OS
    # enforces the cap: SIGTERM at the deadline, SIGKILL 10s later.
    # `timeout` exits 124 when it fires — that code flows out as the
    # inner status and is what lets us label the job `timeout` vs `exited`.
    import shutil
    import subprocess

    from proc_lib import heavy_job_lock

    inner = list(cmd)
    if timeout is not None:
        if not shutil.which(TIMEOUT_BIN) and not shutil.which("timeout"):
            raise _LaunchError("coreutils `timeout` not found; cannot honor --timeout")
        to_bin = TIMEOUT_BIN if os.path.exists(TIMEOUT_BIN) else "timeout"
        inner = [to_bin, "--signal=TERM", "--kill-after=10", str(timeout)] + inner

    # We launch detached and never become this PID's reaper across separate
    # proc invocations, so we can't waitpid for the real exit code. Instead
    # we wrap in a bash group-leader that writes the inner command's $? to a
    # .rc sidecar. The wrapper is the process-group leader, so killpg still
    # signals the whole tree (timeout + the real command); the wrapper then
    # records the code. `exec` is intentionally NOT used so the rc line runs.
    rcp = _rc_path(name)
    with contextlib.suppress(FileNotFoundError):
        rcp.unlink()
    # Quote inner argv safely for the bash -c payload.
    quoted = " ".join(shlex.quote(a) for a in inner)
    rssp = _rss_path(name)
    with contextlib.suppress(FileNotFoundError):
        rssp.unlink()
    script = _wrapper_script(quoted, rcp, _gnu_time(), rssp)
    argv = [*(_scope_prefix(name) if scope else []), "bash", "-c", script]

    logp = _log_path(name)
    # Opened here, handed to the child, and closed below once it has the fd;
    # a `with` block can't span the Popen handoff, hence the explicit close.
    logf = open(logp, "ab")  # noqa: SIM115
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=full_env | {ENV_JOB_NAME: name},
        )
    except FileNotFoundError:
        raise _LaunchError(f"command not found: {argv[0]} (or cwd missing: {cwd})") from None
    except OSError as e:
        raise _LaunchError(f"failed to launch: {e}") from e
    finally:
        # parent doesn't need the fd; child inherited it
        with contextlib.suppress(OSError):
            logf.close()

    pid = proc.pid
    if heavy:
        heavy_job_lock.claim(name, pid, " ".join(cmd))
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        pgid = pid
    # Best-effort: a job living >~ a few ms always yields a starttime; this
    # hardens the PID-reuse guard against a transient /proc race at launch.
    starttime = _capture_starttime(pid)
    ts = _now()
    meta = {
        "name": name,
        "cmd": list(cmd),
        "cwd": cwd,
        "pid": pid,
        "pgid": pgid,
        "start_ts": ts,
        "start_iso": _iso(ts),
        "pid_starttime": starttime,
        "timeout": timeout,
        "env": recorded_env,
        "status": RUNNING,
        "exit_code": None,
        "end_ts": None,
        "log_path": str(logp),
        "rc_path": str(rcp),
    }
    if heavy:  # recorded only for heavy jobs so a plain job keeps the long-standing key set
        meta["heavy"] = True
    return meta


def _ensure_launcher() -> bool:
    from proc_lib import launcher

    try:
        launcher.ensure(BASE)
    except launcher.LauncherError as e:
        _err(f"scheduled start needs a launcher and none works: {e}")
        return False
    return True


def _ensure_claude_hook() -> None:
    from proc_lib import claude_hook

    note = claude_hook.ensure(BASE)
    if note:
        print(note, file=sys.stderr)


def _note_session(meta: dict) -> None:
    session = (meta.get("owner") or {}).get("session")
    if session:
        _session_add(session, meta["name"])


def cmd_start(args) -> int:
    name = args.name
    cmd = args.cmd
    if not cmd:
        _err("no command given after '--'")
        return EXIT_USAGE
    # argparse.REMAINDER keeps a leading '--' if present; strip one.
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        _err("no command given after '--'")
        return EXIT_USAGE

    cwd = args.cwd or os.getcwd()
    if not os.path.isdir(cwd):
        _err(f"cwd does not exist: {cwd}")
        return EXIT_USAGE
    _ensure_claude_hook()

    try:
        job_env = _parse_env(getattr(args, "env", None) or [])
    except ValueError as e:
        _err(str(e))
        return EXIT_USAGE

    at = getattr(args, "at", None)
    at_ts = None
    if at is not None:
        try:
            at_ts = _parse_at(at)
        except ValueError as e:
            _err(str(e))
            return EXIT_USAGE
        if not _ensure_launcher():
            return EXIT_LAUNCHER
    heavy = bool(getattr(args, "heavy", False))
    force_concurrent = bool(getattr(args, "force_concurrent", False))
    owner = _owner_from_env()

    def op():
        existing = _load(name)
        if existing is not None:
            existing = _reconcile(existing)
            _save(existing)
            status = existing.get("status")
            if at_ts is not None and status == RUNNING and os.environ.get(ENV_JOB_NAME) == name:
                spec = _run_spec(
                    cmd, cwd, args.timeout, _masked_env(job_env), heavy, force_concurrent, at_ts
                )
                _set_next_run(existing, spec, dict(os.environ) | job_env)
                _save(existing)
                print(f"'{name}' will run again at {_iso_short(at_ts)} once this run ends")
                return 0
            if status == RUNNING:
                _err(
                    f"job '{name}' is already running (pid {existing.get('pid')}); "
                    f"kill it or choose another name"
                )
                return EXIT_USAGE
            if status == SCHEDULED:
                _err(
                    f"job '{name}' is already scheduled for "
                    f"{_iso_short(existing['scheduled_ts'])}; `proc reschedule {name} --at ...` "
                    f"moves it, `proc kill {name}` cancels it"
                )
                return EXIT_USAGE
            _err(
                f"job '{name}' already exists with status "
                f"'{status}'; run `proc rm {name}` first "
                f"(won't overwrite its log)"
            )
            return EXIT_USAGE

        if at_ts is not None:
            logp = _log_path(name)
            meta = _run_spec(
                cmd, cwd, args.timeout, _masked_env(job_env), heavy, force_concurrent, at_ts
            ) | {
                "name": name,
                "pid": None,
                "pgid": None,
                "start_ts": None,
                "start_iso": None,
                "pid_starttime": None,
                "status": SCHEDULED,
                "exit_code": None,
                "end_ts": None,
                "log_path": str(logp),
                "rc_path": str(_rc_path(name)),
            }
            if owner:
                meta["owner"] = owner
            _write_schedenv(name, dict(os.environ) | job_env)
            _save(meta)
            _note_session(meta)
            print(f"scheduled '{name}' for {_iso_short(at_ts)}")
            print(f"log: {logp}")
            return 0

        if heavy:
            from proc_lib import heavy_job_lock

            try:
                heavy_job_lock.check_free(name, force_concurrent)
            except heavy_job_lock.HeavyJobBusy as e:
                _err(str(e))
                return EXIT_USAGE

        try:
            meta = _spawn(
                name,
                cmd,
                cwd,
                args.timeout,
                heavy,
                os.environ | job_env,
                _masked_env(job_env),
            )
        except _LaunchError as e:
            _err(str(e))
            return EXIT_USAGE
        if owner:
            meta["owner"] = owner
        _save(meta)
        _note_session(meta)
        print(f"started '{name}' pid={meta['pid']} pgid={meta['pgid']}")
        print(f"log: {meta['log_path']}")
        return 0

    return _with_lock(op)


def cmd_reschedule(args) -> int:
    """Move a scheduled job's start, or let a running job set its own next run
    (same command, cwd, timeout and environment) from inside itself."""
    try:
        at_ts = _parse_at(args.at)
    except ValueError as e:
        _err(str(e))
        return EXIT_USAGE
    if not _ensure_launcher():
        return EXIT_LAUNCHER
    name = args.name

    def op():
        m = _load(name)
        if m is None:
            _err(f"no such job: {name}")
            return EXIT_NOTFOUND
        _reconcile(m)
        status = m.get("status")
        if status == SCHEDULED:
            m["scheduled_ts"] = at_ts
            m["scheduled_iso"] = _iso(at_ts)
        elif status == RUNNING:
            if os.environ.get(ENV_JOB_NAME) != name:
                _err(f"job '{name}' is running; only the job itself may set its next run")
                return EXIT_USAGE
            spec = _run_spec(
                m["cmd"],
                m["cwd"],
                m.get("timeout"),
                m.get("env") or {},
                bool(m.get("heavy")),
                False,
                at_ts,
            )
            _set_next_run(m, spec, dict(os.environ))
        else:
            _err(f"job '{name}' is {status}; `proc rm {name}` and start it again with --at")
            return EXIT_USAGE
        _save(m)
        print(f"'{name}' will run at {_iso_short(at_ts)}")
        return 0

    return _with_lock(op)


def _launch_one(name: str, now: float) -> str | None:
    """Flip one scheduled record to running (start its process), under the lock the
    caller holds. Returns "launched" or "failed" when it acted, None when the entry is
    no longer due (another pass started it, it was cancelled or moved)."""
    m = _load(name)
    if m is None:
        _index_set(name, None)  # the record is gone: the index entry is stale
        return None
    if m.get("status") != SCHEDULED or m["scheduled_ts"] > now:
        _index_set(name, _live_entry(m))  # the index entry is stale: bring it in line
        return None
    if m.get("heavy"):
        from proc_lib import heavy_job_lock

        try:
            heavy_job_lock.check_free(name, bool(m.get("force_concurrent")))
        except heavy_job_lock.HeavyJobBusy as e:
            print(f"deferred '{name}': {e}")
            return None
    try:
        new = _spawn(
            name,
            m["cmd"],
            m["cwd"],
            m.get("timeout"),
            bool(m.get("heavy")),
            _read_schedenv(name),
            m.get("env") or {},
            scope=True,
        )
    except _LaunchError as e:
        m.update(status=EXITED, exit_code=None, end_ts=_now(), launch_error=str(e))
        with open(m["log_path"], "a") as fh:
            fh.write(f"proc: scheduled start failed: {e}\n")
        _save(m)
        _drop_schedenv(name)
        print(f"failed '{name}': {e}")
        return "failed"
    new["scheduled_for"] = m["scheduled_iso"]
    if m.get("owner"):  # the scheduling session owns it, not the launcher's environment
        new["owner"] = m["owner"]
    _save(new)
    _drop_schedenv(name)
    print(f"launched '{name}' pid={new['pid']}")
    return "launched"


def cmd_launch_due(args) -> int:
    """Start every scheduled job whose time has come, exactly once.

    Run once a minute by the launcher backend (see launcher.py). It finds the work in
    the live index (a few KB), never opening a record that is not due or dead, and
    takes the registry lock only around each state flip. Each flip re-reads its record
    under the lock and acts only if it is still scheduled and due, so two overlapping
    passes can never start the same entry twice. Running jobs whose process has gone
    are finalised here too, which is what removes them from the index when nobody
    else asks about them. The pass ends by writing its cost line (see cost.py)."""
    probe = cost.Probe()
    jobs = _live_index()
    entries = len(jobs)
    finalised = 0
    dead = [
        n
        for n, e in jobs.items()
        if e.get("status") == RUNNING and not _alive(e.get("pid"), e.get("st"))
    ]
    for name in dead:
        _with_lock(lambda name=name: _reconcile_and_save(name))
        finalised += 1
    if dead:
        jobs = _live_index()
    now = _now()
    due = sorted(
        (e.get("scheduled_ts") or 0, n)
        for n, e in jobs.items()
        if e.get("status") == SCHEDULED and (e.get("scheduled_ts") or 0) <= now
    )
    launched = failed = 0
    for _, name in due:
        outcome = _with_lock(lambda name=name: _launch_one(name, now))
        launched += outcome == "launched"
        failed += outcome == "failed"
    cost.finish_launch_due(
        BASE,
        probe,
        entries=entries,
        launched=launched,
        failed=failed,
        finalised=finalised,
        lock_wait_s=_LOCK_SECONDS["wait"],
        lock_hold_s=_LOCK_SECONDS["hold"],
    )
    return 0


def cmd_launcher(args) -> int:
    """install / status / uninstall the per-minute launcher backend."""
    from proc_lib import launcher

    try:
        if args.action == "install":
            st = launcher.install(BASE)
        elif args.action == "uninstall":
            st = launcher.uninstall()
        else:
            st = launcher.status()
    except launcher.LauncherError as e:
        _err(str(e))
        return EXIT_LAUNCHER
    print(st.line())
    return 0 if st.ok or args.action == "uninstall" else EXIT_LAUNCHER


def cmd_hook(args) -> int:
    """install / uninstall / status of the Claude Code hook, or `unwrap` (the hook itself)."""
    from proc_lib import claude_hook

    if args.action == "unwrap":
        return claude_hook.main()
    dirs = [Path(d) for d in args.config_dir] or claude_hook.default_config_dirs()
    code = 0
    for d in dirs:
        try:
            if args.action == "install":
                st = claude_hook.install(d, BASE)
            elif args.action == "uninstall":
                st = claude_hook.uninstall(d, BASE)
            else:
                st = claude_hook.status(d)
        except (OSError, ValueError) as e:
            _err(f"{claude_hook.settings_path(d)}: {e}")
            return EXIT_USAGE
        print(st.line())
        if args.action != "uninstall" and not st.installed:
            code = EXIT_HOOK_MISSING
    return code


def _entry_meta(name: str, e: dict) -> dict:
    """A record-shaped view of an index entry: every field `list --json` shows."""
    meta = {
        "name": name,
        "status": e.get("status"),
        "pid": e.get("pid"),
        "pid_starttime": e.get("st"),
        "owner": e.get("owner") or {},
        "start_ts": e.get("start"),
    }
    if e.get("status") == SCHEDULED:
        meta["scheduled_ts"] = e.get("scheduled_ts")
    return meta


def _list_source(args, from_records: bool) -> tuple[list[dict], set[str]]:
    """The jobs `list` considers and the statuses to show (empty set: any).

    Default: the live index (running and scheduled). `--running` / `--scheduled`
    narrow it, with `--session ID` also by owner: that is the live index alone, and
    a record is opened only when `from_records` (the table view and `--full`, which
    show command, timeout or paths) asks for the listed jobs' own records. `--session
    ID` without `--running`/`--scheduled` is that session's own list of every job it
    started, in any state, and opens a record per job. `--all` is the whole registry,
    the only form that reads every record."""
    session = getattr(args, "session", None)
    want: set[str] = set()
    if getattr(args, "running", False):
        want.add(RUNNING)
    if getattr(args, "scheduled", False):
        want.add(SCHEDULED)
    if session and not want:
        names = _session_names(session)
        return [m for n in names if (m := _load(n)) is not None], want
    if getattr(args, "all", False) and not want:
        return _all_metas(history=True), want
    if not want:
        want = set(LIVE)
    entries = {
        n: e
        for n, e in _live_index().items()
        if e.get("status") in want
        and (not session or (e.get("owner") or {}).get("session") == session)
    }
    if from_records:
        return [m for n in entries if (m := _load(n)) is not None], want
    return [_entry_meta(n, e) for n, e in entries.items()], want


def _settle(metas: list[dict]) -> list[dict]:
    """Return the records with each still-`running`-but-dead one finalised, the writes
    done under one lock section (no lock when nothing needs finalising)."""
    stale = [
        m["name"]
        for m in metas
        if m.get("status") == RUNNING and not _alive(m.get("pid"), m.get("pid_starttime"))
    ]
    if not stale:
        return metas

    def op():
        return {n: _reconcile_and_save(n) for n in stale}

    fresh = _with_lock(op)
    return [fresh.get(m["name"]) or m for m in metas]


def cmd_reindex(args) -> int:
    """Rebuild the live index and the per-session lists from every record."""
    jobs = _with_lock(_rebuild_index)
    print(f"reindexed: {len(jobs)} live job(s)")
    return 0


def _exited_since(hours: float) -> list[dict]:
    """The jobs that ended in the last `hours`, from the live folder (jobs not yet swept)
    and the history day folders the window covers, nothing else. A running job whose process
    has gone is finalised first, so a job that just ended is not missed. A record whose file
    was last written before the window cannot have ended inside it (the record is written
    at, or after, the end), so it is skipped without being opened."""
    dead = [
        n
        for n, e in _live_index().items()
        if e.get("status") == RUNNING and not _alive(e.get("pid"), e.get("st"))
    ]
    if dead:
        _with_lock(lambda: [_reconcile_and_save(n) for n in dead])
    cutoff = _now() - hours * 3600
    first = _day(cutoff)
    metas = []
    seen: set[str] = set()
    folders = [BASE] + [_history_root() / d for d in reversed(_history_days()) if d >= first]
    for folder in folders:
        with os.scandir(folder) as it:
            for e in it:
                if (
                    not e.name.endswith(".json")
                    or e.name.startswith(".")
                    or e.stat().st_mtime < cutoff
                ):
                    continue
                m = _read_json(Path(e.path))
                if not m or m.get("name") in seen:
                    continue
                if m.get("status") in TERMINAL and (m.get("end_ts") or 0) >= cutoff:
                    seen.add(m.get("name"))
                    metas.append(m)
    return metas


def _list_exited(args) -> list[dict] | None:
    """The `--exited --since` listing, or None (error printed) when the flags are wrong."""
    since = getattr(args, "since", None)
    clash = [
        f
        for f in ("all", "running", "scheduled", "session")
        if getattr(args, f, None) not in (None, False)
    ]
    if getattr(args, "exited", False):
        if since is None or since <= 0:
            _err("--exited needs --since HOURS (a positive number)")
            return None
        if clash:
            _err(f"--exited cannot be combined with --{clash[0]}")
            return None
        return _exited_since(since)
    if since is not None:
        _err("--since belongs to --exited")
        return None
    return []


def cmd_list(args) -> int:
    as_json = getattr(args, "json", False)
    full = getattr(args, "full", False)
    exited = getattr(args, "exited", False)
    if exited or getattr(args, "since", None) is not None:
        metas = _list_exited(args)
        if metas is None:
            return EXIT_USAGE
        want: set[str] = set()
    else:
        metas, want = _list_source(args, from_records=full or not as_json)
        metas = _settle(metas)
    if want:
        metas = [m for m in metas if m.get("status") in want]

    # running first, then scheduled by start time, then the rest by start_ts desc
    def sort_key(m):
        status = m.get("status")
        if status == RUNNING:
            return (0, -(m.get("start_ts") or 0))
        if status == SCHEDULED:
            return (1, m["scheduled_ts"])
        return (2, -(m.get("start_ts") or 0))

    metas.sort(key=sort_key)
    if as_json:
        render = _status_json if full else _list_json
        print(json.dumps([render(m) for m in metas], indent=2 if full else None))
        return 0
    if not metas:
        print("no jobs")
        return 0

    rows = []
    for m in metas:
        cmd = _cmd_str(m)
        if len(cmd) > 50:
            cmd = cmd[:47] + "..."
        to = m.get("timeout")
        rows.append(
            [
                m.get("name", "?"),
                str(m.get("pid") or "-"),
                m.get("status", "?"),
                _elapsed(m),
                ("-" if to is None else f"{to}s"),
                _iso_short(m["scheduled_ts"]) if m.get("status") == SCHEDULED else "-",
                cmd,
            ]
        )
    headers = ["NAME", "PID", "STATUS", "ELAPSED", "TIMEOUT", "START", "CMD"]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    print(fmt.format(*headers))
    for r in rows:
        print(fmt.format(*r))
    return 0


def cmd_status(args) -> int:
    m = _current(args.name)
    if m is None:
        _err(f"no such job: {args.name}")
        return EXIT_NOTFOUND
    if getattr(args, "json", False):
        print(json.dumps(_status_json(m), indent=2))
    else:
        print("\n".join(_status_lines(m)))
    return 0


def cmd_logs(args) -> int:
    m = _load(args.name)
    if m is None:
        _err(f"no such job: {args.name}")
        return EXIT_NOTFOUND
    logp = Path(m.get("log_path") or _log_path(args.name))
    if not logp.exists():
        _err(f"no log file for job: {args.name}")
        return EXIT_NOTFOUND

    since_ts = None
    if getattr(args, "since", None) is not None:
        try:
            since_ts = _parse_zoned(args.since, "--since")
        except ValueError as e:
            _err(str(e))
            return EXIT_USAGE

    def _initial(lines: list[str]) -> list[str]:
        if since_ts is not None:
            lines = _since_filter(lines, since_ts, logp.stat().st_mtime)
        if args.tail is not None and args.tail >= 0:
            lines = lines[-args.tail :] if args.tail else []
        return lines

    if not args.follow:
        with open(logp, errors="replace") as fh:
            lines = fh.readlines()
        lines = _initial(lines)
        sys.stdout.write("".join(lines))
        if lines and not lines[-1].endswith("\n"):
            sys.stdout.write("\n")
        return 0

    # follow: poll for new bytes; auto-stop on terminal status or the hard cap.
    started = _now()
    with open(logp, errors="replace") as fh:
        if args.tail is not None or since_ts is not None:
            # show only the filtered / last N lines first
            sys.stdout.write("".join(_initial(fh.readlines())))
            sys.stdout.flush()
        else:
            sys.stdout.write(fh.read())
            sys.stdout.flush()
        while True:
            chunk = fh.read()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            cur = _current(args.name)
            status = cur.get("status") if cur else None
            if status in TERMINAL:
                # drain any final bytes
                tail = fh.read()
                if tail:
                    sys.stdout.write(tail)
                    sys.stdout.flush()
                print(f"\n-- job {status} --", file=sys.stderr)
                return 0
            if _now() - started > FOLLOW_HARD_CAP_SECONDS:
                print("\n-- follow auto-stopped after 600s cap --", file=sys.stderr)
                return EXIT_WAIT_TIMEOUT
            time.sleep(0.5)


def cmd_wait(args) -> int:
    m = _load(args.name)
    if m is None:
        _err(f"no such job: {args.name}")
        return EXIT_NOTFOUND
    deadline = _now() + (args.timeout if args.timeout is not None else 3600)
    while True:
        cur = _current(args.name)  # no registry lock while the job is simply still running
        if cur is None:
            _err(f"no such job: {args.name}")
            return EXIT_NOTFOUND
        status = cur.get("status")
        if status in TERMINAL:
            # Non-zero on any unsuccessful end, so `proc wait a && next` chains
            # stop on failure; a distinct code keeps it apart from 3 (still
            # running) and 4 (unknown job) instead of passing the job's own rc
            # through, which would collide with them.
            print(_verdict(cur))
            return 0 if _job_succeeded(cur) else EXIT_JOB_FAILED
        if _now() >= deadline:
            print(f"{_verdict(cur)} (wait timed out)")
            return EXIT_WAIT_TIMEOUT
        time.sleep(0.5)


def cmd_kill(args) -> int:
    def op():
        m = _load(args.name)
        if m is None:
            return ("notfound", None)
        # Reconcile FIRST: a job that already exited cleanly but whose registry
        # entry is still 'running' (no list/status/wait ran since) must be
        # finalized from its .rc sidecar -> 'exited' with the real exit code,
        # NOT mislabeled and signaled. This is the tech-debt fix that retires
        # the old `lost` status (which discarded the .rc code).
        before = m.get("status")
        _reconcile(m)
        if m.get("status") != before:
            _save(m)
        if m.get("status") in TERMINAL:
            return ("already", m)
        if m.get("status") == SCHEDULED:
            m.update(status=KILLED, end_ts=_now())
            _save(m)
            _drop_schedenv(args.name)
            return ("cancelled", m)
        if m.pop("next_run", None):
            # killing a job also cancels the run it had asked for
            _drop_schedenv(args.name)
            _save(m)

        pid = m.get("pid")
        # Launched with start_new_session=True, so the wrapper is the session
        # leader and sid == its pid. We signal by SESSION, not pgid (see
        # _session_members for why timeout's nested process group defeats killpg).
        sid = pid
        # After reconcile a still-RUNNING job is genuinely alive (reconcile would
        # have finalized a dead one). Re-check defensively before signaling.
        if not _alive(pid, m.get("pid_starttime")):
            _reconcile(m)
            _save(m)
            return ("already", m)

        sig = signal.SIGKILL if args.force else signal.SIGTERM
        signaled = _signal_session(sid, sig)
        if signaled == 0:
            # session already empty — nothing left to kill
            m["status"] = KILLED
            m["end_ts"] = _now()
            ec = _read_rc(m)
            if ec is not None:
                m["exit_code"] = ec
            _save(m)
            return ("gone", m)
        return ("signaled", m, sig)

    res = _with_lock(op)
    tag = res[0]
    if tag == "notfound":
        _err(f"no such job: {args.name}")
        return EXIT_NOTFOUND
    if tag == "already":
        m = res[1]
        ec = m.get("exit_code")
        print(f"{args.name}: already {m.get('status')} (exit_code={ec})")
        return 0
    if tag == "gone":
        print(f"{args.name}: process already gone -> marked killed")
        return 0
    if tag == "cancelled":
        print(f"{args.name}: scheduled start cancelled")
        return 0

    # signaled — wait outside the lock, then finalize.
    m = res[1]
    sig = res[2]
    sid = m.get("pid")  # session leader id == wrapper pid

    # Drain the WHOLE SESSION, not just the leader pid. The bash wrapper (leader)
    # dies on SIGTERM instantly; `timeout` and a worker with its own SIGTERM
    # handler live on in a different process group but the SAME session. We
    # RE-signal each poll iteration rather than signal once: a process tree that
    # is still forking children when the first signal lands (e.g. `proc kill`
    # immediately after `start`, before bash has spawned its grandchildren) would
    # otherwise let a late-forked descendant escape a one-shot kill.
    if sig == signal.SIGTERM:
        # grace period: let the session exit on its own, re-TERMing stragglers.
        deadline = _now() + KILL_GRACE_SECONDS
        while _now() < deadline:
            if not _session_alive(sid):
                break
            _signal_session(sid, signal.SIGTERM)
            time.sleep(0.2)
    # Hard-kill drain (also the entire --force path): re-SIGKILL any member that
    # is still or newly alive until the session is empty or the budget is spent.
    for _ in range(KILL_DRAIN_POLLS):  # ~5s ceiling, exits early the moment the session drains
        if not _session_alive(sid):
            break
        _signal_session(sid, signal.SIGKILL)
        time.sleep(0.1)

    def finalize():
        cur = _load(args.name) or m
        if cur.pop("next_run", None):  # asked for during the kill grace
            _drop_schedenv(args.name)
        # Decided under the lock, on a fresh scan: `killed` is written only when
        # no member of the session is alive.
        survivors = _session_members(sid)
        if survivors:
            # Not dead: keep the status as it was and name the pids in the record.
            cur["kill_survivors"] = survivors
            _save(cur)
            return survivors
        cur.pop("kill_survivors", None)
        cur["status"] = KILLED
        cur["end_ts"] = _now()
        # If the job recorded an exit code before dying (e.g. it caught SIGTERM
        # and exited cleanly), keep it for information — status stays `killed`.
        ec, job_cost = _read_rc_file(cur)
        if ec is not None:
            cur["exit_code"] = ec
        if job_cost:
            cur["cost"] = job_cost
        _save(cur)
        return []

    survivors = _with_lock(finalize)
    if survivors:
        _err(
            f"{args.name}: NOT killed - session {sid} still has live members "
            f"after SIGKILL: pids {', '.join(map(str, survivors))}"
        )
        return EXIT_KILL_SURVIVORS
    print(f"{args.name}: killed (sid {sid})")
    return 0


def _job_files(name: str, m: dict | None) -> list[Path]:
    """Every file of a job, wherever it is now: the record in the live folder and in a
    history folder, the log and `.rc` its record names (else the live defaults), the
    `.rss`, each also in the history folder the record sits in."""
    hp = _history_record(name)
    homes = [BASE] + ([hp.parent] if hp else [])
    files = [_meta_path(name)] + ([hp] if hp else [])
    files += [Path(m[k]) for k in ("log_path", "rc_path") if m and m.get(k)]
    for h in homes:
        files += [h / f"{name}.log", h / f"{name}.rc", h / f"{name}.rss"]
    return list(dict.fromkeys(files))


def _rm_one(name: str, force: bool) -> int:
    def op():
        m = _load(name)
        if m is None:
            return ("notfound", None)
        before = m.get("status")
        _reconcile(m)
        if m.get("status") != before:
            _save(m)
        if m.get("status") == RUNNING and not force:
            return ("running", m)
        return ("ok", m)

    res = _with_lock(op)
    tag, m = res
    if tag == "notfound":
        _err(f"no such job: {name}")
        return EXIT_NOTFOUND
    if tag == "running":
        _err(
            f"job '{name}' is still running; use `proc kill {name}` "
            f"first or `proc rm --force {name}`"
        )
        return EXIT_USAGE

    # if forcing removal of a running job, kill it first
    if m.get("status") == RUNNING and force:
        kargs = types.SimpleNamespace(name=name, force=True)
        krc = cmd_kill(kargs)
        if krc not in (0, EXIT_NOTFOUND):
            return krc  # members survive: keep the record that names them

    def do_remove():
        # Also drop this job's heavy-job lockfile, if any — otherwise an rm'd
        # heavy job leaves a stale lock that only ages out (or, under PID reuse,
        # could falsely block a future heavy job for up to HEAVY_JOB_STALE_SEC).
        hlock = BASE / "heavy_job" / f"{name}.lock"
        for f in (*_job_files(name, m), hlock, _schedenv_path(name)):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                _err(f"could not remove {f}: {e}")
        _index_set(name, None)
        return True

    _with_lock(do_remove)
    print(f"removed '{name}'")
    return 0


def cmd_rm(args) -> int:
    # `name` is nargs="+" (a list); tolerate a bare string for internal callers.
    names = args.name if isinstance(args.name, list) else [args.name]
    worst = 0
    for name in names:
        rc = _rm_one(name, args.force)
        if rc != 0 and worst == 0:
            worst = rc
    return worst


def cmd_clean(args) -> int:
    """Remove terminal jobs whose end is older than the cutoff (default 24 h).

    The scan reads records without the registry lock; each removal re-reads its record
    under the lock and removes it only if it is still terminal and old, so the lock is
    held per removal, not for the whole scan. Only records stored as terminal are
    considered: a job still marked running is the reconcile's business (launch-due
    finalises it within a minute)."""
    hours = args.older_than if args.older_than is not None else 24
    cutoff = _now() - hours * 3600

    def expired(m) -> bool:
        if not m or m.get("status") not in TERMINAL:
            return False
        return (m.get("end_ts") or m.get("start_ts") or 0) <= cutoff

    def remove(name: str) -> bool:
        m = _load(name)
        if not expired(m):
            return False
        for f in _job_files(name, m):
            with contextlib.suppress(FileNotFoundError):
                f.unlink()
        _index_set(name, None)
        return True

    removed = []
    for m in _all_metas(history=True):
        if expired(m) and _with_lock(lambda n=m["name"]: remove(n)):
            removed.append(m["name"])
    print(f"removed {len(removed)} terminal job(s) older than {hours}h")
    for n in removed:
        print(f"  - {n}")
    return 0


def cmd_archive(args) -> int:
    """Move every finished record still in the live folder, with its log and sidecars,
    into its history day folder (records past the retention bound are deleted): the one-time
    move of a registry that grew before history existed, by the same `_archive` a job's end
    uses. A single pass over the live folder, the lock held per record; a record that
    changed meanwhile (no longer finished) is left."""
    moved: list[str] = []
    _ensure_base()

    def one(name: str) -> bool:
        m = _read_json(_meta_path(name))
        if not m or m.get("status") not in TERMINAL or m.get("name") != name:
            return False
        _archive(m)
        return True

    with os.scandir(BASE) as it:
        names = sorted(e.name[:-5] for e in it if e.name.endswith(".json") and e.is_file())
    for name in names:
        if _with_lock(lambda name=name: one(name)):
            moved.append(name)

    def forget():  # a finished job a crash left in the index leaves it now
        jobs = _index_locked()
        gone = [n for n in moved if n in jobs]
        for n in gone:
            del jobs[n]
        if gone:
            _write_index(jobs)

    _with_lock(forget)
    kept = len(names) - len(moved)
    print(f"archived {len(moved)} finished job(s); live folder keeps {kept} record(s)")
    return 0
