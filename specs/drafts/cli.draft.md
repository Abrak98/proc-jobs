# cli — draft

> MANDATORY origin record. AI-assembled from the chat that requested this work.
> The captured request + reasoning the spec was designed from. Frozen at acceptance:
> never edited again. On any later requirement change, edit the permanent spec — NOT this draft.

## Request (verbatim intent)
Same originating request as `core.draft.md`: retrofit headspec specs for proc-lib's source
modules. `cli.py` is the argparse front end; documented as its own spec so the module boundary
(argument parsing vs. engine logic) is visible in the spec graph, not folded into
`core.spec.md`.

## Clarifications (Q/A from chat)
- Q: Does `cli.py` own any state or business logic of its own?
  A: No — verified by reading the file: every subparser's `set_defaults(func=core.cmd_*)`
    delegates straight to `core`; `cli.py`'s only logic is argument-shape definition and the
    top-level `KeyboardInterrupt`/`BrokenPipeError` handling in `main()`.

## Decisions
- Kept as a separate spec from `core.spec.md` rather than folded in, because the module split
  in the source (`cli.py` vs `core.py`) is itself a real interface: `core`'s public functions
  are called from `cli.py` today, but are equally callable from any other frontend an
  in-process caller writes.

## Resulting spec
`specs/proc_lib/cli.spec.md`
