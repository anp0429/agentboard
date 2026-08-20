"""Cache root anchoring + materialized whole-tree cache.

(sigs EDGEVERDICT_CACHE_ROOT_ANCHOR_V1, EDGEVERDICT_MATERIALIZED_TREE_V1)

The bug class under test: the direct runner repoints _workdir to the package
dir, and any CACHE path that follows it captures/restores the wrong tree —
the exact failure that cached apps/studio's symlink forest as the root
node_modules and restored a tree of dangling relative symlinks. These tests
pin (1) _rootdir stays put when the direct runner activates, (2) a poisoned
entry is recognized, (3) a pnpm-shaped workspace survives the full
capture -> wipe -> restore round trip with every relative symlink resolving.
"""

import os

from edgeverdict.verifiers.finding_verifier import (
    _MATERIALIZED_MARKER,
    FindingVerifier,
    _cache_entry_poisoned,
    _capture_pkg_trees,
    _clone_tree,
    _enumerate_pkg_nm,
    _restore_pkg_trees,
)

PNPM_INSTALL = ["npx", "pnpm@11.5.0", "install"]


def _mk_pnpm_workspace(root):
    """A minimal pnpm-shaped workspace: a real module in the root .pnpm
    store, per-package trees of RELATIVE symlinks into it, and a .bin shim —
    the exact layout whose portability the materialized cache depends on."""
    store_pkg = os.path.join(
        root, "node_modules", ".pnpm", "foo@1.0.0", "node_modules", "foo")
    os.makedirs(store_pkg)
    with open(os.path.join(store_pkg, "index.js"), "w") as fh:
        fh.write("module.exports = 42;\n")
    os.makedirs(os.path.join(root, "node_modules", "foo_root_link_dir"))
    for pkg in ("apps/studio", "packages/common"):
        nm = os.path.join(root, pkg, "node_modules")
        os.makedirs(nm)
        depth = 2 + pkg.count("/")
        target = ("../" * depth) + "node_modules/.pnpm/foo@1.0.0/node_modules/foo"
        os.symlink(target, os.path.join(nm, "foo"))
        os.makedirs(os.path.join(nm, ".bin"))
        os.symlink("../foo/index.js", os.path.join(nm, ".bin", "foo-tool"))
    with open(os.path.join(root, "pnpm-workspace.yaml"), "w") as fh:
        fh.write("packages:\n  - apps/*\n  - packages/*\n")
    with open(os.path.join(root, "pnpm-lock.yaml"), "w") as fh:
        fh.write("lockfileVersion: 9\n")


def _assert_links_resolve(root):
    for pkg in ("apps/studio", "packages/common"):
        link = os.path.join(root, pkg, "node_modules", "foo")
        assert os.path.islink(link), link
        resolved = os.path.join(root, pkg, "node_modules",
                                os.readlink(link), "index.js")
        assert os.path.isfile(os.path.normpath(resolved)), resolved
        shim = os.path.join(root, pkg, "node_modules", ".bin", "foo-tool")
        assert os.path.islink(shim), shim


def test_rootdir_ignores_direct_runner(tmp_path):
    v = FindingVerifier.__new__(FindingVerifier)
    v.project_dir = "."
    v._direct_runner_active = True

    class P:
        pkg_dir = "apps/studio"

    v.profile = P()
    repo = str(tmp_path)
    assert v._workdir(repo) == os.path.normpath(
        os.path.join(repo, "apps/studio"))
    assert v._rootdir(repo) == os.path.normpath(repo)


def test_poisoned_entry_recognized(tmp_path):
    cached_nm = str(tmp_path / "node_modules")
    os.makedirs(os.path.join(cached_nm, "react"))
    assert _cache_entry_poisoned(cached_nm, PNPM_INSTALL)
    os.makedirs(os.path.join(cached_nm, ".pnpm"))
    assert not _cache_entry_poisoned(cached_nm, PNPM_INSTALL)
    assert not _cache_entry_poisoned(cached_nm, ["npm", "install"])
    assert not _cache_entry_poisoned(str(tmp_path / "absent"), PNPM_INSTALL)


def test_enumerate_pkg_nm_skips_root_and_never_descends(tmp_path):
    root = str(tmp_path)
    _mk_pnpm_workspace(root)
    os.makedirs(os.path.join(root, ".git", "objects"))
    assert _enumerate_pkg_nm(root) == ["apps/studio", "packages/common"]


def test_clone_tree_preserves_symlinks(tmp_path):
    src = str(tmp_path / "src")
    os.makedirs(src)
    with open(os.path.join(src, "real.txt"), "w") as fh:
        fh.write("x")
    os.symlink("../elsewhere", os.path.join(src, "dangler"))
    dst = str(tmp_path / "dst")
    _clone_tree(src, dst)
    assert os.path.isfile(os.path.join(dst, "real.txt"))
    assert os.path.islink(os.path.join(dst, "dangler"))
    assert os.readlink(os.path.join(dst, "dangler")) == "../elsewhere"


def test_materialized_round_trip(tmp_path):
    """capture -> wipe every per-package tree -> restore -> every relative
    symlink resolves to a real file. This is the portability claim the
    relink-free restore stands on."""
    root = str(tmp_path / "repo")
    os.makedirs(root)
    _mk_pnpm_workspace(root)
    cdir = str(tmp_path / "entry")
    os.makedirs(cdir)
    assert _capture_pkg_trees(root, cdir)
    assert os.path.isfile(os.path.join(cdir, _MATERIALIZED_MARKER))
    fresh = str(tmp_path / "fresh")
    os.makedirs(fresh)
    _mk_pnpm_workspace(fresh)
    import shutil
    for pkg in ("apps/studio", "packages/common"):
        shutil.rmtree(os.path.join(fresh, pkg, "node_modules"))
    assert _restore_pkg_trees(cdir, fresh)
    _assert_links_resolve(fresh)


def test_restore_refuses_unmaterialized_entry(tmp_path):
    cdir = str(tmp_path / "entry")
    os.makedirs(os.path.join(cdir, "pkg-trees", "apps", "x", "node_modules"))
    assert not _restore_pkg_trees(cdir, str(tmp_path / "repo"))
