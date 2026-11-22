# heavy_job_lock — draft

> MANDATORY origin record. AI-assembled from the chat that requested this work.
> The captured request + reasoning the spec was designed from. Frozen at acceptance:
> never edited again. On any later requirement change, edit the permanent spec — NOT this draft.

## Request (verbatim intent)
Same originating request as `core.draft.md`: retrofit headspec specs for proc-lib's source
modules. `heavy_job_lock.py` is documented separately from `core.py` because it records a real
incident (a WSL2 out-of-memory kill that layered two RAM-heavy jobs past the host's hypervisor
cap) as its origin.

## Clarifications (Q/A from chat)
- Q: Should the spec name the project or pipeline the incident happened in?
  A: No. The spec keeps the technical lesson (host RAM cap, corruption consequence) and no
    name of any private repository or pipeline.

## Decisions
- Documented as a standalone spec, not folded into `core.spec.md`, because `heavy_job_lock`
  has its own public API usable in-process independent of `proc start` (`acquire()` as a
  context manager) — it is a real, separately-reusable module, not a private helper.

## Resulting spec
`specs/proc_lib/heavy_job_lock.spec.md`
