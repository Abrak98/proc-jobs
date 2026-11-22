# Public release: how a change reaches the public repository

The maintained tree lives in a private repository. The public repository,
`Abrak98/proc-supervisor`, holds the same tree as a separate history that starts from one clean
commit; the private history is never pushed to it. Every later change travels the one path below,
and each step is a guard that refuses a bad change before it is public.

## Path of a change

1. The change is made, tested and merged into the private main branch as usual.
2. From a checkout of the private main branch, with a clean tree, the maintainer runs
   `scripts/export_public.sh <public clone>`. The script puts the whole current tree into one
   commit on a new `sync-<sha>` branch of the public clone, authored with the maintainer's GitHub
   no-reply address, pushes the branch and opens a pull request.
3. The push passes the maintainer's pre-push hook first: `gitleaks` and the deny-list scan
   (`scripts/denylist_check.py`) over the pushed commits. A finding refuses the push; the
   fix is made in the private repository and the export is run again.
4. The pull request must pass the required checks `secrets` (gitleaks over the full history),
   `denylist` (the deny-list scan over the full history, from the encrypted `DENYLIST` secret),
   `gates` and `floor (3.12)` / `floor (3.13)` (lint, tests on both interpreters).
5. The maintainer squash-merges the pull request. The public main branch stays linear and takes
   no direct push, no force-push and no deletion.

## Settings in force on the public repository

| Setting | Value |
| --- | --- |
| Main branch | ruleset: pull request required (no approval count, a sole maintainer cannot approve his own), required checks, no force-push, no deletion, linear history |
| Bypass of the ruleset | repository administrator role only (the maintainer), in pull-request mode: he may merge a pull request whose `denylist` check cannot run (a fork's), but never push to `main` directly |
| Collaborators | none besides the maintainer; outside contributors use a fork and a pull request |
| Actions | default `GITHUB_TOKEN` read-only, workflows cannot approve pull requests, workflows from outside contributors need the maintainer's approval, only actions owned by GitHub plus `astral-sh/setup-uv` are allowed |
| Secret scanning and push protection | enabled |
| Dependabot alerts and security updates | enabled |
| Private vulnerability reporting | enabled (see `SECURITY.md`) |
| Code owners | `.github/CODEOWNERS`, the maintainer; code-owner review is not required, since it would block his own pull requests |
| Signed commits | not required, since the maintainer's automated tooling does not sign |

## Rules that keep the guards meaningful

- The deny list is a file outside every repository (the encrypted `DENYLIST` secret on GitHub is a
  copy of it). A word is added to the file and to the secret together; the secret is replaced with
  `gh secret set DENYLIST --repo Abrak98/proc-supervisor < <file>`.
- No guard has a skip flag. A false match is fixed by narrowing the entry in the deny list.
- The hook guards only pushes to the public remote; pushes to the private repository are not scanned.
