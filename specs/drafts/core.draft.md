# core — draft

> MANDATORY origin record. AI-assembled from the chat that requested this work.
> The captured request + reasoning the spec was designed from. Frozen at acceptance:
> never edited again. On any later requirement change, edit the permanent spec — NOT this draft.

## Request (verbatim intent)
Give proc-lib real specs under the headspec methodology, as part of preparing the repository
for an eventual open-source release.

This is a retrofit, not a green-field design: `core.py` already exists (854 lines, 3 local
commits ahead of origin, tests passing) and implements a working process supervisor. The spec
below documents the actual shipped behaviour and the reasoning recovered from its own comments
and `CLAUDE.md`'s hard invariants — it does not propose a new design.

## Clarifications (Q/A from chat)
- Q: Should the spec cover `heavy_job_lock.py` and `cli.py` too?
  A: No — each module gets its own spec (`heavy_job_lock.spec.md`, `cli.spec.md`); `core.py`
    imports `heavy_job_lock` as a declared dependency rather than re-describing it.
- Q: Does this retrofit change any runtime behaviour?
  A: No — headspec spec authoring is documentation-only here; no source line in `core.py` changes.

## Decisions
- Domain directory `specs/proc_lib/` mirrors the package layout (`src/proc_lib/core.py` ->
  `specs/proc_lib/core.spec.md`), so the header ref is a direct one-to-one path.
- The Names registry lists the status/exit-code vocabulary as canonical with no
  forbidden-aliases entries — nothing has been renamed in this retrofit, so the naming gate
  has nothing to enforce yet; the table exists so a future rename has somewhere to register.

## Resulting spec
`specs/proc_lib/core.spec.md`
