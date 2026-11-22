# claude_hook — draft

> MANDATORY origin record. AI-assembled from the chat that requested this work.
> The captured request + reasoning the spec was designed from. Frozen at acceptance:
> never edited again. On any later requirement change, edit the permanent spec — NOT this draft.

## Request (verbatim intent)
A team project's Claude Code hooks had to strip `proc start ... --` themselves to judge the
real command, which put the name of a personal tool into a company repository in about
sixteen hook files and their tests. The request: that repository's hooks know nothing of
`proc`; `proc` ships its own hook, installed into the user's Claude settings (never into a
project), which shows the current project's PreToolUse Bash checks the command behind
`proc start` and returns their verdict.

## Clarifications (Q/A from chat)
- Q: Does a rewrite of the command (`updatedInput`) reach the project's hooks?
  A: No. Measured on Claude Code 2.1.319: hooks of one event run in parallel on the original
    input; the call ran the rewrite while the other hook saw the original. So the hook runs the
    project's hooks itself.
- Q: Which forms of `proc start` are handled?
  A: Every one the project's own stripping handled: options before `--`, `--name=J`, `cd x &&`
    ahead, chains, an environment prefix, a path to `proc`.
- Q: Who installs it?
  A: `proc` does: `proc hook install`, and `proc start` once when the seat's settings lack it.

## Decisions
- One module, `claude_hook`, with the hook, the unwrapping and the settings editing; it imports
  nothing from `core`.
- Fail closed on every project-hook failure.

## Resulting spec
`specs/proc_lib/claude_hook.spec.md`
