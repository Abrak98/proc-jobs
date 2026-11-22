# cost — draft

> MANDATORY origin record. AI-assembled from the chat that requested this work.
> The captured request + reasoning the spec was designed from. Frozen at acceptance:
> never edited again. On any later requirement change, edit the permanent spec — NOT this draft.

## Request (verbatim intent)
A resource audit found `proc launch-due`, run every minute, reading every job record to find
the few scheduled ones: seconds of CPU and tens of MB of reads per pass, with the registry lock
held. The request: the pass reads only the live set, and records its own cost and raises an
alert when it exceeds a bound, with no extra process.

## Clarifications (Q/A from chat)
- Q: Where does the cost line go?
  A: Proc's own daily file beside the registry; one line per pass.
- Q: How is the alert posted?
  A: One post per key per day, an exclusive stamp file decides who posts, the command comes
    from the user's configuration so no personal tool is named in this repository.

## Decisions
- A separate small module so the engine's registry logic stays apart from reporting.

## Resulting spec
`specs/proc_lib/cost.spec.md`
