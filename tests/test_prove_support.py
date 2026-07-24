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


def test_untracked_source_files_are_named_not_silently_ignored(repo):
    from agentboard.config import untracked_source_files
    (repo / "brand_new.py").write_text("N = 1\n")
    (repo / "test_new.py").write_text("def test_n(): pass\n")
    (repo / "notes.md").write_text("hi\n")
    assert untracked_source_files(str(repo)) == ["brand_new.py"]


def test_import_surface_names_the_module_and_public_names(tmp_path):
    from agentboard.agents.reviewer_agent import import_surface
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (tmp_path / "src" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text(
        "CONST = 1\n_hidden = 2\n\n"
        "def public_fn():\n    pass\n\n"
        "def _private_fn():\n    pass\n\n"
        "class Thing:\n    pass\n"
    )
    out = import_surface(str(tmp_path), "src/pkg/mod.py")
    # src is a source ROOT, not a package: `pkg.mod` is what pip installs
    # and what a test can import. The first version of this test blessed
    # `src.pkg.mod` and the gate's round-three review corrected us.
    assert "`pkg.mod`" in out
    assert "public_fn" in out and "Thing" in out and "CONST" in out
    assert "_private_fn" not in out and "_hidden" not in out


def test_import_surface_for_package_init_is_the_package(tmp_path):
    from agentboard.agents.reviewer_agent import import_surface
    pkg = tmp_path / "acme"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("def hello():\n    pass\n")
    out = import_surface(str(tmp_path), "acme/__init__.py")
    assert "`acme`" in out
    assert "hello" in out


def test_import_surface_is_empty_for_non_python_and_unparsable(tmp_path):
    from agentboard.agents.reviewer_agent import import_surface
    (tmp_path / "a.ts").write_text("export const a = 1\n")
    (tmp_path / "bad.py").write_text("def broken(:\n")
    assert import_surface(str(tmp_path), "a.ts") == ""
    assert import_surface(str(tmp_path), "bad.py") == ""


def test_import_surface_round_three_regressions(tmp_path):
    """The five gaps the gate found in import_surface itself (run
    e4a011add5ff924c), one file: annotated constants, tuple/compound
    assignments, lowercase public bindings, and privates still excluded."""
    from agentboard.agents.reviewer_agent import import_surface
    (tmp_path / "mod.py").write_text(
        "ENABLED: bool = True\n"
        "bare_annotation: int\n"
        "FIRST, SECOND = 1, 2\n"
        "a = b = 3\n"
        "router = object()\n"
        "_private = 4\n"
    )
    out = import_surface(str(tmp_path), "mod.py")
    for name in ("ENABLED", "FIRST", "SECOND", "a", "b", "router"):
        assert name in out
    assert "_private" not in out
    assert "bare_annotation" not in out  # annotation without value: no binding


def test_import_surface_init_reexports_are_the_public_surface(tmp_path):
    from agentboard.agents.reviewer_agent import import_surface
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "from .mod import Thing as Thing, helper\n"
        "from ._impl import _secret\n"
    )
    out = import_surface(str(tmp_path), "src/pkg/__init__.py")
    assert "`pkg`" in out
    assert "Thing" in out and "helper" in out
    assert "_secret" not in out


def test_import_surface_namespace_packages_keep_the_full_path(tmp_path):
    from agentboard.agents.reviewer_agent import import_surface
    d = tmp_path / "company" / "product"
    d.mkdir(parents=True)  # PEP 420: no __init__.py anywhere
    (d / "feature.py").write_text("def go():\n    pass\n")
    out = import_surface(str(tmp_path), "company/product/feature.py")
    assert "`company.product.feature`" in out


def test_import_surface_regular_module_imports_are_not_api(tmp_path):
    from agentboard.agents.reviewer_agent import import_surface
    (tmp_path / "mod.py").write_text("import json\n\ndef fn():\n    pass\n")
    out = import_surface(str(tmp_path), "mod.py")
    assert "fn" in out
    assert "json" not in out


def test_import_surface_round_four_regressions(tmp_path):
    """Run 6e0f5bfb428f68c7: module-level for/with targets persist as
    globals and are listed; a del removes its name; a top-level walrus
    binds; names inside nested scopes still never leak."""
    from agentboard.agents.reviewer_agent import import_surface
    (tmp_path / "mod.py").write_text(
        "for item in [1]:\n    pass\n"
        "with open(__file__) as handle:\n    pass\n"
        "TEMP = 1\n"
        "del TEMP\n"
        "total = (walrus_public := 5) + 1\n"
        "def fn():\n    inner = (nested_walrus := 2)\n    return inner\n"
    )
    out = import_surface(str(tmp_path), "mod.py")
    for name in ("item", "handle", "walrus_public", "total", "fn"):
        assert name in out
    assert "TEMP" not in out
    assert "nested_walrus" not in out
    assert "inner" not in out


def test_import_surface_refuses_untrustworthy_truncated_paths(tmp_path):
    """A non-identifier path segment below any source root makes the
    module path a guess; the honest surface is none at all."""
    from agentboard.agents.reviewer_agent import import_surface
    bad = tmp_path / "my-lib" / "pkg"
    bad.mkdir(parents=True)
    (bad / "mod.py").write_text("def fn():\n    pass\n")
    assert import_surface(str(tmp_path), "my-lib/pkg/mod.py") == ""
    ok = tmp_path / "packages" / "my-app" / "src" / "pkg"
    ok.mkdir(parents=True)
    (ok / "mod.py").write_text("def fn():\n    pass\n")
    out = import_surface(str(tmp_path), "packages/my-app/src/pkg/mod.py")
    assert "`pkg.mod`" in out  # cut at a src root: self-contained, trusted


def test_import_surface_round_six_regressions(tmp_path):
    """Run c6b038372514ff65's five real gaps, one file: non-identifier
    stem refused; del-then-rebind keeps the rebind; tuple del removes
    every contained name; walrus in a def's default binds; a binding
    inside a module-level except body persists while the handler alias
    (deleted by Python) stays out."""
    from agentboard.agents.reviewer_agent import import_surface
    (tmp_path / "my-module.py").write_text("def fn():\n    pass\n")
    assert import_surface(str(tmp_path), "my-module.py") == ""

    (tmp_path / "mod.py").write_text(
        "X = 1\n"
        "del X\n"
        "X = 2\n"
        "A, B, C = 1, 2, 3\n"
        "del (A, B)\n"
        "def f(x=(w_default := 1)):\n    pass\n"
        "try:\n"
        "    import missing_mod\n"
        "except ImportError as boom:\n"
        "    fallback_flag = True\n"
        "if True:\n"
        "    FEATURE = 1\n"
    )
    out = import_surface(str(tmp_path), "mod.py")
    for name in ("X", "C", "w_default", "f", "fallback_flag", "FEATURE"):
        assert name in out
    for name in ("A", "B", "boom"):
        assert f" {name}," not in out and f" {name}." not in out
