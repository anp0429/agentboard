"""prove's zero-flag defaults are only honest if the pieces that fill them
in are deterministic and right. These tests pin the two new ones:
working_tree_dirty (the mode cue) and targets_from_diff (the target list a
user never typed). The rule under test for targets: changed source files
in, test files and deletions out, sorted."""

import subprocess

import pytest

from agentboard.config import targets_from_diff, working_tree_dirty


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    """A one-commit repo on main with a source file and its test."""
    r = str(tmp_path)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (tmp_path / "a.ts").write_text("export const a = 1\n")
    (tmp_path / "a.test.ts").write_text("test('a', () => {})\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return tmp_path


def test_clean_tree_is_not_dirty(repo):
    assert working_tree_dirty(str(repo)) is False


def test_tracked_edit_is_dirty_untracked_is_not(repo):
    (repo / "new.ts").write_text("export const n = 1\n")  # untracked
    assert working_tree_dirty(str(repo)) is False
    (repo / "a.ts").write_text("export const a = 2\n")  # tracked edit
    assert working_tree_dirty(str(repo)) is True


def test_branch_diff_yields_changed_source_not_its_test(repo):
    r = str(repo)
    _git(r, "checkout", "-q", "-b", "feat")
    (repo / "a.ts").write_text("export const a = 2\n")
    (repo / "a.test.ts").write_text("test('a2', () => {})\n")
    (repo / "b.py").write_text("B = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "feat")
    assert targets_from_diff(r, "main", "feat") == ["a.ts", "b.py"]


def test_base_movement_is_not_the_branchs_change(repo):
    """base...head (three dots): commits on main after the fork must not
    appear as the branch's own targets."""
    r = str(repo)
    _git(r, "checkout", "-q", "-b", "feat")
    (repo / "a.ts").write_text("export const a = 2\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "feat")
    _git(r, "checkout", "-q", "main")
    (repo / "mainonly.ts").write_text("export const m = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "main moved")
    _git(r, "checkout", "-q", "feat")
    assert targets_from_diff(r, "main", "feat") == ["a.ts"]


def test_worktree_diff_sees_uncommitted_edits(repo):
    (repo / "a.ts").write_text("export const a = 3\n")
    assert targets_from_diff(str(repo), "HEAD", worktree=True) == ["a.ts"]


def test_test_shaped_files_are_excluded_everywhere(repo):
    r = str(repo)
    _git(r, "checkout", "-q", "-b", "feat")
    (repo / "tests").mkdir()
    (repo / "tests" / "helper.py").write_text("H = 1\n")
    (repo / "test_mod.py").write_text("def test_x(): pass\n")
    (repo / "mod_test.py").write_text("def test_y(): pass\n")
    (repo / "conftest.py").write_text("pass\n")
    (repo / "real.py").write_text("R = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "feat")
    assert targets_from_diff(r, "main", "feat") == ["real.py"]


def test_deleted_and_nonsource_files_are_excluded(repo):
    r = str(repo)
    _git(r, "checkout", "-q", "-b", "feat")
    (repo / "a.ts").unlink()
    (repo / "notes.md").write_text("hi\n")
    (repo / "keep.js").write_text("var k = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "feat")
    assert targets_from_diff(r, "main", "feat") == ["keep.js"]


def test_renamed_files_appear_as_their_current_path(repo):
    r = str(repo)
    _git(r, "checkout", "-q", "-b", "feat")
    _git(r, "mv", "a.ts", "moved.ts")
    (repo / "moved.ts").write_text("export const a = 2\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "rename")
    targets = targets_from_diff(r, "main", "feat")
    assert "moved.ts" in targets
    assert "a.ts" not in targets


def test_empty_diff_returns_empty_list_not_an_error(repo):
    assert targets_from_diff(str(repo), "main", "main") == []
    assert targets_from_diff(str(repo), "HEAD", worktree=True) == []
