# heavy_job_lock — spec

> Permanent contract and source of truth. Edit THIS file directly on any change.
> Every source `.py` implementing it carries `# spec: <path-to-this-file>`.

## Intent
A process-level exclusive lock that prevents two "heavy" jobs (>300 MB peak RAM for more
than ~30s, or a subprocess fleet) from running concurrently on a shared host. Exists because
two such jobs layering on a WSL2 host can breach its memory cap; the hypervisor
then SIGKILLs indiscriminately, which has previously corrupted a git repository's loose
objects mid-write. Usable both as an in-process context manager (`acquire`) and as the two-step
check/claim pair `proc start --heavy` uses around a detached `Popen`.

## Rationale / Key decisions
- **Origin: a real incident, not a hypothetical.** Two independently-launched RAM-heavy jobs
  layered to ~7 GB combined on a shared WSL2 host past its 8 GB hypervisor cap; the resulting
  SIGKILL corrupted a git loose object. The lock exists specifically to make that class of
  incident structurally impossible, not to generically throttle concurrency.
- **Lockfile-per-job under a shared directory, not a single global lockfile.** Each holder
  writes `<job_name>.lock`; `current_holders()`/`check_free()` scan the whole directory. This
  keeps the holder's identity (PID, job name, start time, argv) inspectable by reading one
  file, and lets a stale lock be reclaimed individually without disturbing an unrelated one.
- **Atomic claim via `O_CREAT|O_EXCL`.** `_write_lock_atomic` uses the POSIX-atomic exclusive
  create so two racing claimants can never both believe they hold the lock.
- **Staleness by PID-death OR age, not PID alone.** `_is_stale` reclaims a lock whose holder
  PID is dead (crash without cleanup) or whose lockfile is older than `STALE_AFTER_SEC` (4h
  default, env-tunable) — covering both "the holder crashed" and "the holder hung forever."
- **`check_free()`/`claim()` split, not one atomic call, for the `proc start --heavy`
  integration.** The lock must be claimed under the *launched job's* PID, which is only known
  after `Popen()` returns — so the check happens before launch (fail fast, don't spawn a
  process that will immediately conflict) and the claim happens after (`core.cmd_start`).
- **Same-process re-entry is a pass-through, not a deadlock.** `acquire()` detects its own PID
  already holding its own lockfile and yields without re-claiming or erroring — a nested
  `with acquire("x")` inside code already holding that same lock must not deadlock or raise.
- **Fail loud on conflict, never silently retry.** `HeavyJobBusy` is raised immediately; the
  module docstring is explicit that a caller must not "silently sleep and retry" since that
  would hide the very conflict the lock exists to surface.

## Rejected alternatives
- **A single global lockfile (one lock for all heavy jobs combined).** Rejected: loses holder
  identity — a caller hitting `HeavyJobBusy` needs to know *which* job/PID/argv is holding it
  to decide whether to wait, override, or investigate; a single opaque file can't carry that
  without becoming exactly the per-job design chosen instead.
- **OS-level `flock` instead of `O_CREAT|O_EXCL`.** Rejected: an `flock` is released
  automatically when its holding process dies (which is actually wanted for staleness), but
  the module already needs to read holder metadata (PID, job name, argv) independent of lock
  state for `current_holders()`/error messages, so a payload-bearing file plus explicit
  staleness logic was simpler than layering metadata on top of an `flock`.
- **Silent wait-and-retry on conflict.** Rejected: hides the conflict from the caller
  instead of surfacing it as an explicit, actionable `HeavyJobBusy`.

## Acceptance criteria
- GIVEN no live heavy job holds the lock WHEN `check_free()` is called THEN it returns without
  raising and writes nothing.
- GIVEN another live, non-stale heavy job holds the lock WHEN `check_free()` or `acquire()` is
  called (without `allow_concurrent`) THEN `HeavyJobBusy` is raised, naming the holder's PID,
  job name, start time, and argv.
- GIVEN a lock whose holder PID is dead, or whose lockfile is older than `STALE_AFTER_SEC`
  WHEN `check_free()`/`acquire()`/`claim()` runs THEN the stale lock is silently reclaimed
  (not treated as a conflict).
- GIVEN the same process already holds its own lock via `acquire()` WHEN it calls `acquire()`
  again for the same `job_name` THEN it yields without re-claiming, error-free.
- GIVEN `acquire()`'s `with` block exits (normally, by exception, or by `KeyboardInterrupt`)
  WHEN the lockfile still records this process's own PID THEN the lockfile is removed.

## State
Lockfiles under `<PROC_HOME>/heavy_job/<job_name>.lock` (directory resolved from
`proc_lib.core.BASE` at call time, via a late import that avoids a circular dependency and
picks up test monkeypatches on `core.BASE` automatically). Each lockfile's content:
```
<pid>
<job_name>
<started_iso (UTC)>
<argv_str>
```

## Methods
- `check_free(job_name, allow_concurrent=False) -> None` — raise `HeavyJobBusy` if a live,
  non-stale holder exists; reclaims stale locks found along the way; writes nothing.
- `claim(job_name, pid, argv_str) -> Path` — atomically write a lockfile for a just-launched
  PID; reclaims a stale leftover under the same name first.
- `acquire(job_name, *, allow_concurrent=False) -> ContextManager[Path]` — in-process
  exclusive-lock context manager; same-process re-entry is a no-op pass-through; releases on
  exit only if the lockfile still names this process's own PID.
- `current_holders() -> list[LockHolder]` — every live (non-stale) holder currently recorded.

## Invariants
- Two distinct live PIDs never simultaneously hold a non-stale lock for overlapping heavy
  work, absent an explicit `allow_concurrent=True` / `--force-concurrent` override.
- A lock is only ever released by the process whose PID it currently records (`acquire`'s
  `finally` re-reads the file and checks `holder.pid == os.getpid()` before unlinking).
- Staleness reclaim never requires a live process's cooperation — a crashed holder's lock is
  always eventually reclaimable, bounded by `STALE_AFTER_SEC`.
- stdlib-only at runtime — no dependency beyond the Python standard library.

## Formulas
n/a

## I/O contracts
- `LockHolder` (frozen dataclass): `pid: int`, `job_name: str`, `started_iso: str`, `argv: str`.
- `HeavyJobBusy(RuntimeError)`: carries `.holder: LockHolder` and `.attempted_job: str`; its
  message names the holder and suggests `--force-concurrent`.
- `STALE_AFTER_SEC: int` — read from env `HEAVY_JOB_STALE_SEC`, default `4 * 3600`.

## Names
| canonical | kind | forbidden-aliases |
|-----------|------|-------------------|
| HeavyJobBusy | class | |
| LockHolder | class | |
| STALE_AFTER_SEC | const | |
| check_free | function | |
| claim | function | |
| acquire | function | |

## Dependencies
None within `proc_lib` other than the late-bound read of `proc_lib.core.BASE` (see State) —
deliberately not a module-level import, to avoid a circular dependency with `core.py`, which
imports `heavy_job_lock` at module level.

## Test cases
- GIVEN no existing lock WHEN `check_free()` runs THEN it returns cleanly
  (`test_heavy_job_lock.py`).
- GIVEN a live holder's lock WHEN a second `check_free()`/`acquire()` runs without
  `allow_concurrent` THEN `HeavyJobBusy` is raised naming the first holder
  (`test_heavy_job_lock.py`).
- GIVEN a lock whose PID is dead WHEN `check_free()` runs THEN the stale lock is reclaimed and
  the call succeeds (`test_heavy_job_lock.py`).
- GIVEN `acquire()` is entered twice in the same process for the same `job_name` WHEN the
  second call runs THEN it does not deadlock and does not re-claim (`test_heavy_job_lock.py`).
- GIVEN `proc start --heavy` is called while another heavy job is running WHEN `cmd_start`
  runs `heavy_job_lock.check_free` THEN `proc start` refuses to launch with `EXIT_USAGE`
  (`test_heavy_job_lock.py`, cross-checked against `core.spec.md`).
