# Security Policy

## Supported versions

Pre-1.0 (`0.x`) — only the latest commit on `main` is supported. There is no back-port policy
yet; a fix lands on `main` and that's the supported state.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for a security vulnerability.

Use GitHub's private vulnerability reporting instead: open the repository's **Security** tab
→ **Report a vulnerability**. This opens a private advisory visible only to the maintainer
until a fix is ready, which avoids disclosing an exploitable issue before a patch exists.

If you can't use that flow, open a regular issue asking for a private contact channel without
describing the vulnerability itself, and the maintainer will follow up.

## Scope notes specific to this project

- `proc` launches arbitrary caller-supplied commands by design (that is its entire purpose) —
  that is not itself a vulnerability. In scope is anything where `proc`'s *own* handling of a
  command introduces a risk beyond what the caller explicitly asked for: unsafe quoting in the
  generated bash wrapper (`shlex.quote` usage in `cmd_start`), a signal reaching a process
  `proc` did not launch (the PID-reuse guard in `_alive`), or a job name / path value reaching
  a shell context unsanitized.
- `proc` reads and trusts `$PROC_HOME`'s on-disk registry files as its own state. A vulnerability
  where a malformed or crafted `<name>.json`/`.rc` file causes anything beyond a clean parse
  failure (e.g. code execution via `json.load`, path traversal via a crafted job name) is in
  scope.
- The heavy-job lock (`heavy_job_lock.py`) is a cooperative, not adversarial, RAM-exclusivity
  guard — it is not a security boundary between untrusted processes, only a same-user
  concurrency safeguard. A report describing it as bypassable by an uncooperative process is
  expected behavior, not a vulnerability.
