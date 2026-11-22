# <Component> — spec

> Permanent contract and source of truth. Edit THIS file directly on any change.
> Every source `.py` implementing it carries `# spec: <path-to-this-file>`.

## Intent
<one paragraph: what this does and why it exists>

## Rationale / Key decisions
<why this design — the durable reasoning; the draft holds the original request, this holds the decisions>

## Rejected alternatives
<options considered and discarded, and why (so a future AI does not re-litigate them)>

## Acceptance criteria
- <measurable success condition — prefer EARS or GIVEN/WHEN/THEN phrasing>
- <boundary / failure condition>

## State
<persistent state or data this component owns; "n/a" if stateless>

## Methods
<public surface: function/class signatures, each with a one-line contract>

## Invariants
- <condition that is always true>

## Formulas
<domain math, if any; else "n/a">

## I/O contracts
<input/output schema of the public surface>

## Names
<!-- Canonical name registry: one row per concept THIS spec owns. The canonical
     spelling is the only one allowed in code; a retired name goes under
     forbidden-aliases and the gate then bans its reappearance. Omit the section if
     this component introduces no new domain name. -->
| canonical | kind | forbidden-aliases |
|-----------|------|-------------------|
| <name> | field/function/class/const/column | <old-name-if-renamed> |

## Dependencies
<spec dependencies — cross-checked against the import-derived spec graph>

## Test cases
- GIVEN <state> WHEN <action> THEN <expected>
