"""Worktree mode: the diff and the sandbox must describe the same facts.

`load_worktree_diff` exists because an agent mid-session has dirty,
uncommitted edits — the exact state `load_pr_diff` (refs only) cannot see
and the exact state the sandbox copytree DOES execute. These tests pin the
boundary between the two modes, and the preflight inversion that goes with
it (dirty tree: blocker in refs mode, the point of the run in worktree
mode)."""

import os
import subprocess

import pytest

from agentboard.config import preflight
from agentboard.ingestion.pr_diff import load_pr_diff, load_worktree_diff


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    """A one-commit repo with one tracked file."""
    r = str(tmp_path)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (tmp_path / "a.ts").write_text("export const a = 1\n")
    _git(r, "add", "a.ts")
    _git(r, "commit", "-q", "-m", "init")
    return r


def test_worktree_diff_sees_uncommitted_edits(repo):
    with open(os.path.join(repo, "a.ts"), "a", encoding="utf-8") as fh:
        fh.write("export const b = 2\n")
    d = load_worktree_diff(repo)
    assert [f.path for f in d.files] == ["a.ts"]
    assert "export const b = 2" in d.files[0].added
    assert d.head == "WORKTREE"


def test_refs_diff_is_blind_to_the_same_edits(repo):
    """The reason worktree mode exists, stated as an executable fact."""
    with open(os.path.join(repo, "a.ts"), "a", encoding="utf-8") as fh:
        fh.write("export const b = 2\n")
    d = load_pr_diff(repo, head="HEAD", base="HEAD")
    assert d.files == []


def test_worktree_diff_clean_tree_is_empty(repo):
    assert load_worktree_diff(repo).files == []


def test_worktree_diff_against_explicit_base(repo):
    """base can be any ref: diff working tree vs main's parent-of-tip etc."""
    with open(os.path.join(repo, "a.ts"), "w", encoding="utf-8") as fh:
        fh.write("export const a = 99\n")
    _git(repo, "add", "a.ts")
    _git(repo, "commit", "-q", "-m", "bump")
    d = load_worktree_diff(repo, base="HEAD~1")
    assert "export const a = 99" in d.files[0].added


def _preflight(repo, **kw):
    kw.setdefault("repo_root", repo)
    kw.setdefault("head", "HEAD")
    kw.setdefault("base", "HEAD")
    kw.setdefault("target", "a.ts")
    kw.setdefault("tests", "")
    kw.setdefault("reviewer_model", "gpt-4o")
    kw.setdefault("need_critic", False)
    kw.setdefault("critic_model", "")
    return preflight(**kw)


def test_preflight_dirty_tree_blocks_refs_mode_only(repo, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    with open(os.path.join(repo, "a.ts"), "a", encoding="utf-8") as fh:
        fh.write("dirty\n")
    refs_problems = _preflight(repo, worktree=False)
    assert any("uncommitted changes" in p for p in refs_problems)
    # same repo, worktree mode: the dirty tree is the subject, not a problem
    wt_problems = _preflight(repo, worktree=True)
    assert not any("uncommitted changes" in p for p in wt_problems)


def test_preflight_refs_mode_error_mentions_worktree_flag(repo, monkeypatch):
    """The block message must teach the escape hatch it now has."""
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    with open(os.path.join(repo, "a.ts"), "a", encoding="utf-8") as fh:
        fh.write("dirty\n")
    [msg] = [p for p in _preflight(repo, worktree=False) if "uncommitted" in p]
    assert "--worktree" in msg
