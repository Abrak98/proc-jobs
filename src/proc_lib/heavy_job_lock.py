# spec: specs/proc_lib/heavy_job_lock.spec.md
"""Exclusive lock keeping two memory-heavy jobs from running at the same time.

Two heavy jobs layered on one shared host can push it past its memory cap, after which
the hypervisor kills processes indiscriminately. Each holder records itself in
``<BASE>/heavy_job/<job_name>.lock``; a conflicting claimant gets :class:`HeavyJobBusy`
at once. A caller must never silently sleep and retry: that would hide the very conflict
this lock exists to surface.

Two entry points: :func:`acquire` (in-process context manager) and the
:func:`check_free` / :func:`claim` pair used around a detached launch, where the lock must
carry the launched job's PID, known only after the launch.

stdlib-only, like the rest of the package.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "STALE_AFTER_SEC",
    "HeavyJobBusy",
    "LockHolder",
    "acquire",
    "check_free",
    "claim",
    "current_holders",
]

STALE_AFTER_SEC: int = int(os.environ.get("HEAVY_JOB_STALE_SEC", 4 * 3600))


@dataclass(frozen=True)
class LockHolder:
    pid: int
    job_name: str
    started_iso: str
    argv: str


class HeavyJobBusy(RuntimeError):  # noqa: N818 - the name is the spec's public contract
    """A live, non-stale heavy job already holds the lock."""

    def __init__(self, holder: LockHolder, attempted_job: str):
        self.holder = holder
        self.attempted_job = attempted_job
        super().__init__(
            f"heavy job {attempted_job!r} refused: {holder.job_name!r} "
            f"(pid {holder.pid}, started {holder.started_iso}, argv: {holder.argv}) "
            f"already holds the heavy-job lock; wait for it to finish or pass "
            f"--force-concurrent to override"
        )


def _lock_dir() -> Path:
    from proc_lib import core  # late: core imports this module at load time

    return Path(core.BASE) / "heavy_job"


def _lock_path(job_name: str) -> Path:
    return _lock_dir() / f"{job_name}.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read(path: Path) -> LockHolder | None:
    """Parse a lockfile; None when missing or not (yet) well-formed."""
    try:
        lines = path.read_text().split("\n")
        return LockHolder(
            pid=int(lines[0].strip()),
            job_name=lines[1],
            started_iso=lines[2],
            argv="\n".join(lines[3:]),
        )
    except (OSError, ValueError, IndexError):
        return None


def _too_old(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime > STALE_AFTER_SEC
    except OSError:
        return False


def _is_stale(path: Path, holder: LockHolder | None) -> bool:
    if _too_old(path):
        return True
    return holder is not None and not _pid_alive(holder.pid)


def _unlink(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _scan() -> list[tuple[Path, LockHolder]]:
    """Live holders with their files; stale locks found on the way are removed."""
    found: list[tuple[Path, LockHolder]] = []
    d = _lock_dir()
    if not d.is_dir():
        return found
    for path in sorted(d.glob("*.lock")):
        holder = _read(path)
        if _is_stale(path, holder):
            _unlink(path)
        elif holder is not None:
            found.append((path, holder))
    return found


def current_holders() -> list[LockHolder]:
    return [h for _, h in _scan()]


def check_free(job_name: str, allow_concurrent: bool = False) -> None:
    """Raise HeavyJobBusy if another live holder exists. Writes nothing."""
    holders = current_holders()
    if allow_concurrent:
        return
    for h in holders:
        if h.pid != os.getpid():
            raise HeavyJobBusy(h, job_name)


def _write_lock_atomic(path: Path, payload: str) -> bool:
    """Exclusive create; False if the file already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(payload)
    return True


def _payload(pid: int, job_name: str, argv_str: str) -> str:
    started = datetime.now(UTC).isoformat(timespec="seconds")
    return f"{pid}\n{job_name}\n{started}\n{argv_str}"


def claim(job_name: str, pid: int, argv_str: str) -> Path:
    """Write the lockfile for an already-launched PID, reclaiming a stale leftover."""
    path = _lock_path(job_name)
    payload = _payload(pid, job_name, argv_str)
    for _ in range(2):
        if _write_lock_atomic(path, payload):
            return path
        holder = _read(path)
        if holder is not None and not _is_stale(path, holder):
            raise HeavyJobBusy(holder, job_name)
        _unlink(path)
    raise RuntimeError(f"could not claim heavy-job lock {path}")


@contextlib.contextmanager
def acquire(job_name: str, *, allow_concurrent: bool = False) -> Iterator[Path]:
    """Hold the lock for the duration of the block."""
    path = _lock_path(job_name)
    me = os.getpid()
    own = _read(path)
    if own is not None and own.pid == me and not _is_stale(path, own):
        yield path  # re-entry: already ours
        return
    check_free(job_name, allow_concurrent)
    try:
        claim(job_name, me, " ".join(_argv()))
    except HeavyJobBusy:
        if not allow_concurrent:
            raise
        yield path  # overridden: the name is taken by another holder, run unlocked
        return
    try:
        yield path
    finally:
        now = _read(path)
        if now is not None and now.pid == me:
            _unlink(path)


def _argv() -> list[str]:
    try:
        raw = Path("/proc/self/cmdline").read_bytes()
        return [a.decode(errors="replace") for a in raw.split(b"\0") if a]
    except OSError:
        import sys

        return list(sys.argv)
