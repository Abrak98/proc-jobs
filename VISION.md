# proc-lib — Vision

## Problem

An autonomous agent (or a person) routinely launches long-running processes:
research runs, builds, smoke tests, agent runs. Doing this ad hoc (`nohup ... &`,
manual `kill`, hand-tailing log files) breaks in three ways: the caller hangs on a
foreground or wedged command; killing a wedged job needs an interactive confirmation
that stalls an unattended session; and jobs launched through a harness's built-in
background mechanism are lost when the host crashes.

## What proc is

A small, single-purpose process supervisor exposed as one CLI (`proc`). It
launches commands detached, records each in a persistent on-disk registry with a
combined log, and lets the caller monitor, tail, wait on and terminate them
through subcommands, acting only on processes it launched. An optional
OS-enforced timeout makes a runaway job die on its own.

It is a focused take on the supervisor pattern (`supervisor`, `pm2`,
`systemd --user`, `process-compose`) narrowed to one safety-scoped need: let an
autonomous agent manage its own long jobs without a human in the loop and without
risk to unrelated processes.

## Principles

- **Never hang.** `start` returns immediately; `wait` and `logs --follow` have
  hard caps. The tool is built for an unattended caller.
- **Never kill the wrong thing.** Signaling is registry-only (no `--pid`),
  PID-reuse-guarded via `/proc` starttime, and session-scoped. This is what
  makes `proc` safe to grant a blanket allow-list entry to an agent.
- **Survive crashes.** All state is on disk; status is reconciled from `/proc`
  liveness and an exit-code sidecar on every read, so a job's fate is recoverable
  after a host crash.
- **stdlib-only.** A supervisor must not depend on packages that could break the
  runs it manages. Zero runtime dependencies.

## Non-goals

- Not a scheduler, cron, or dependency graph between jobs.
- Not a daemon: there is no resident process; reconciliation happens lazily on
  each invocation.
- Not a remote or multi-host orchestrator. Single host, single user.
