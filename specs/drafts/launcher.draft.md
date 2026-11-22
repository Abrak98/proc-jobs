# launcher — draft

> MANDATORY origin record. AI-assembled from the chat that requested this work.
> The captured request + reasoning the spec was designed from. Frozen at acceptance:
> never edited again. On any later requirement change, edit the permanent spec — NOT this draft.

## Request (verbatim intent)
A job meant to run later was started at once and slept inside its own script (until a clock
time, or 600 s between load checks). Each such job held a live process and showed as a running
background job for hours or days. The request: a job scheduled for later is a registry record,
not a sleeping process; a small per-minute launcher starts it when due; a job can re-schedule
itself instead of sleeping.

## Clarifications (Q/A from chat)
- Q: What fires the launcher?
  A: A systemd user timer every minute, installed and checked by `proc` itself; cron as the
    fallback when user systemd is unavailable (WSL).
- Q: Can a running job set its own next run?
  A: Yes — `proc start --at` with its own `--name`, or `proc reschedule <name> --at`, from
    inside the job.

## Decisions
- The backend lives in its own module so the engine's registry logic never mixes with
  systemd/cron specifics; the module takes the base dir as an argument and imports nothing
  from `core`, so no spec cycle.

## Resulting spec
`specs/proc_lib/launcher.spec.md`
