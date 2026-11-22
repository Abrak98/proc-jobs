"""The deny-list scan used by the pre-push hook and by CI (scripts/denylist_check.py)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "denylist_check.py"
_spec = importlib.util.spec_from_file_location("denylist_check", SCRIPT)
assert _spec and _spec.loader
dc = importlib.util.module_from_spec(_spec)
sys.modules["denylist_check"] = dc
_spec.loader.exec_module(dc)

DENY = "# a comment\n\nsecretword\nre:corp-[0-9]+\n"


def _git(repo, *args):
    # A commit hook exports GIT_DIR and GIT_INDEX_FILE; they would aim this git at the outer repo.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _commit(repo, message, author="Dev <dev@example.org>"):
    name, email = author.rstrip(">").split(" <")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "commit",
        "-q",
        "-m",
        message,
    )


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = tmp_path_factory.mktemp("repo")
    _git(r, "init", "-q")
    (r / "a.txt").write_text("fine\n")
    _commit(r, "clean start")
    (r / "a.txt").write_text("fine\nline with SecretWord inside\nand corp-42 too\n")
    _commit(r, "mention corp-7 in message")
    (r / "a.txt").write_text("fine\n")
    (r / "secretword.md").write_text("x\n")
    _commit(r, "remove the words", author="Mallory <mallory@secretword.test>")
    return r


def _words(repo, revs):
    return {f.word.lower() for f in dc.scan(repo, revs, dc.parse_denylist(DENY))}


def _where(repo, revs):
    return [f.where for f in dc.scan(repo, revs, dc.parse_denylist(DENY))]


def test_parse_skips_comments_and_blanks():
    assert len(dc.parse_denylist(DENY)) == 2


def test_added_lines_are_found_with_file_and_line(repo):
    where = _where(repo, ["--all"])
    assert any(w.endswith("a.txt:2") for w in where)
    assert any(w.endswith("a.txt:3") for w in where)


def test_matching_is_case_insensitive_and_regex_entries_work(repo):
    assert _words(repo, ["--all"]) >= {"secretword", "corp-42", "corp-7"}


def test_message_file_name_and_author_email_are_scanned(repo):
    where = " | ".join(_where(repo, ["--all"]))
    assert "message line 1" in where
    assert "file name secretword.md" in where
    assert "author email" in where


def test_removed_lines_are_not_findings(repo):
    where = _where(repo, ["HEAD~1..HEAD"])
    assert not any("a.txt" in w for w in where if "message" not in w)


def test_a_revision_range_scans_only_those_commits(repo):
    assert _where(repo, ["HEAD~2..HEAD~1"])
    assert _where(repo, ["HEAD~2"]) == []


def test_cli_exit_codes_and_masking(repo, tmp_path, capsys, monkeypatch):
    deny = tmp_path / "deny.txt"
    deny.write_text(DENY)
    assert dc.main(["--denylist-file", str(deny), "--repo", str(repo), "--all-history"]) == 1
    assert "secretword" in capsys.readouterr().out.lower()
    assert (
        dc.main(["--denylist-file", str(deny), "--repo", str(repo), "--mask", "--all-history"]) == 1
    )
    out = capsys.readouterr().out.lower()
    assert "a.txt:2" in out and "secretword" not in out.replace("secretword.md", "")
    clean = ["--denylist-file", str(deny), "--repo", str(repo), "HEAD~2"]
    assert dc.main(clean) == 0


def test_missing_or_empty_denylist_fails_closed(repo, tmp_path, monkeypatch):
    empty = tmp_path / "empty.txt"
    empty.write_text("# only a comment\n")
    base = ["--repo", str(repo), "--all-history"]
    assert dc.main(["--denylist-file", str(empty), *base]) == 2
    assert dc.main(["--denylist-file", str(tmp_path / "absent.txt"), *base]) == 2
    monkeypatch.delenv("PROC_TEST_DENYLIST", raising=False)
    assert dc.main(["--denylist-env", "PROC_TEST_DENYLIST", *base]) == 2
    monkeypatch.setenv("PROC_TEST_DENYLIST", "secretword")
    assert dc.main(["--denylist-env", "PROC_TEST_DENYLIST", *base]) == 1
