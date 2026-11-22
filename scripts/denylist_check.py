#!/usr/bin/env python3
"""Scan git history for words that must never reach a public repository.

The deny list is a text file kept OUTSIDE the repository (one entry per line, `#` starts a
comment, `re:<pattern>` is a regular expression, anything else is a case-insensitive plain
substring). The same script runs in a pre-push hook on a maintainer's machine and in CI, where
the list arrives through an environment variable filled from an encrypted secret.

Scanned for every commit of the given revisions: the author and committer names and emails, the
commit message, every changed file name, and every line a commit adds. Removed lines are not
scanned: a commit that deletes a word is the fix, not the leak.

Exit status: 0 clean, 1 findings, 2 setup error. A missing, unreadable or empty deny list is a
setup error, never a pass: a check that silently has nothing to check must not look green.
There is no flag that skips a finding; a false match is narrowed in the list itself.

Usage:
    denylist_check.py (--denylist-file PATH | --denylist-env NAME) [--mask] --all-history
    denylist_check.py (--denylist-file PATH | --denylist-env NAME) [--mask] -- <git rev args>

`--mask` prints only where a finding is, never the matched text (CI logs are public).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class Finding:
    where: str
    word: str


def parse_denylist(text: str) -> list[re.Pattern[str]]:
    """Compile the list text; blank lines and `#` comments are ignored."""
    patterns = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body = line[3:] if line.startswith("re:") else re.escape(line)
        patterns.append(re.compile(body, re.IGNORECASE))
    return patterns


def _git(repo: Path, *args: str) -> str:
    # A git hook exports GIT_DIR and GIT_INDEX_FILE, which would override `-C repo`.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
    }
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def _hits(patterns: list[re.Pattern[str]], where: str, text: str) -> Iterator[Finding]:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            yield Finding(where, match.group(0))


def _scan_metadata(repo: Path, revs: list[str], patterns: list[re.Pattern[str]]) -> list[Finding]:
    out = _git(repo, "log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x01", *revs)
    found: list[Finding] = []
    for record in out.split("\x01"):
        if not record.strip():
            continue
        sha, an, ae, cn, ce, body = record.lstrip("\n").split("\x00", 5)
        short = sha[:10]
        for label, value in (
            ("author name", an),
            ("author email", ae),
            ("committer name", cn),
            ("committer email", ce),
        ):
            found += _hits(patterns, f"commit {short} {label}", value)
        for number, line in enumerate(body.splitlines(), 1):
            found += _hits(patterns, f"commit {short} message line {number}", line)
    return found


def _scan_changes(repo: Path, revs: list[str], patterns: list[re.Pattern[str]]) -> list[Finding]:
    out = _git(
        repo,
        "log",
        "-p",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--no-renames",
        "--format=%x01%H",
        *revs,
    )
    found: list[Finding] = []
    short = ""
    path = ""
    number = 0
    for line in out.split("\n"):
        if line.startswith("\x01"):
            short = line[1:11]
        elif line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
            if path != "/dev/null":
                found += _hits(patterns, f"commit {short} file name {path}", path)
        elif line.startswith("@@"):
            hunk = _HUNK.match(line)
            number = int(hunk.group(1)) if hunk else 0
        elif line.startswith("+") and not line.startswith("+++"):
            found += _hits(patterns, f"commit {short} {path}:{number}", line[1:])
            number += 1
    return found


def scan(repo: Path, revs: list[str], patterns: list[re.Pattern[str]]) -> list[Finding]:
    """Every deny-list finding in the commits `revs` select."""
    return _scan_metadata(repo, revs, patterns) + _scan_changes(repo, revs, patterns)


def _load_denylist(args: argparse.Namespace) -> list[re.Pattern[str]]:
    if args.denylist_file:
        text = Path(args.denylist_file).read_text(encoding="utf-8")
    else:
        text = os.environ.get(args.denylist_env, "")
    patterns = parse_denylist(text)
    if not patterns:
        source = args.denylist_file or f"${args.denylist_env}"
        raise RuntimeError(f"the deny list {source} is missing or empty; refusing")
    return patterns


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--denylist-file")
    source.add_argument("--denylist-env")
    parser.add_argument("--mask", action="store_true", help="never print the matched text")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--all-history", action="store_true")
    parser.add_argument("revs", nargs="*", help="git revision arguments, after `--`")
    args = parser.parse_args(argv)
    if not args.all_history and not args.revs:
        parser.error("give --all-history or git revision arguments after `--`")
    try:
        patterns = _load_denylist(args)
        findings = scan(Path(args.repo), ["--all"] if args.all_history else args.revs, patterns)
    except (OSError, RuntimeError) as e:
        print(f"denylist_check: {e}", file=sys.stderr)
        return 2
    for f in findings:
        print(f"deny list: {f.where}" + ("" if args.mask else f": {f.word!r}"))
    if findings:
        print(f"denylist_check: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
