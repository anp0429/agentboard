# sig EDGEVERDICT_VITE_RESTOW_TESTS_V1
# v13: after a run, node_modules/.vite (vite's dep-optimizer output, built
# DURING the run) is re-stowed into the warm-cache entry so the next
# restore starts with warm transforms. Priced: cold vite transforms were a
# prime suspect in the ~300x studio boot gap.
from __future__ import annotations

import os

import pytest

from edgeverdict.verifiers import finding_verifier as fv_mod
from edgeverdict.verifiers.finding_verifier import FindingVerifier


def _mk(tmp_path, monkeypatch, *, enabled=True, fp="cafef00dcafef00d"):
    monkeypatch.setenv("EDGEVERDICT_WARM_CACHE", "1" if enabled else "")
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("EDGEVERDICT_WARM_CACHE_DIR", str(cache_root))
    repo = tmp_path / "warm" / "repo"
    vite = repo / "node_modules" / ".vite" / "deps"
    vite.mkdir(parents=True)
    (vite / "chunk.js").write_text("export default 1\n")
    fv = FindingVerifier.__new__(FindingVerifier)
    fv._warm_repo = str(repo)
    fv._cache_fp = fp
    fv.project_dir = "."
    fv.log = lambda *a, **k: None
    cdir = cache_root / fp
    (cdir / "node_modules").mkdir(parents=True)
    return fv, cdir


def test_restow_copies_vite_dir_into_entry(tmp_path, monkeypatch):
    fv, cdir = _mk(tmp_path, monkeypatch)
    fv._restow_vite_cache()
    assert (cdir / "node_modules" / ".vite" / "deps" / "chunk.js").is_file()


def test_restow_replaces_stale_vite_dir(tmp_path, monkeypatch):
    fv, cdir = _mk(tmp_path, monkeypatch)
    stale = cdir / "node_modules" / ".vite"
    stale.mkdir(parents=True)
    (stale / "old.js").write_text("stale\n")
    fv._restow_vite_cache()
    assert not (stale / "old.js").exists()
    assert (stale / "deps" / "chunk.js").is_file()


def test_restow_noop_when_cache_disabled(tmp_path, monkeypatch):
    fv, cdir = _mk(tmp_path, monkeypatch, enabled=False)
    fv._restow_vite_cache()
    assert not (cdir / "node_modules" / ".vite").exists()


def test_restow_noop_without_vite_artifacts(tmp_path, monkeypatch):
    fv, cdir = _mk(tmp_path, monkeypatch)
    import shutil
    shutil.rmtree(os.path.join(fv._warm_repo, "node_modules", ".vite"))
    fv._restow_vite_cache()
    assert not (cdir / "node_modules" / ".vite").exists()


def test_restow_noop_when_entry_invalidated(tmp_path, monkeypatch):
    # entry deleted mid-run (self-heal): nothing to enrich, no crash,
    # and the entry must NOT be resurrected as a vite-only husk
    fv, cdir = _mk(tmp_path, monkeypatch)
    import shutil
    shutil.rmtree(cdir)
    fv._restow_vite_cache()
    assert not cdir.exists()


def test_restow_never_raises_on_copy_failure(tmp_path, monkeypatch):
    fv, cdir = _mk(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(fv_mod.shutil, "copytree", boom)
    fv._restow_vite_cache()  # best-effort: must swallow, not raise
    assert not (cdir / "node_modules" / ".vite").exists()


@pytest.mark.parametrize("missing", ["fp", "repo"])
def test_restow_requires_key_and_repo(tmp_path, monkeypatch, missing):
    fv, cdir = _mk(tmp_path, monkeypatch)
    if missing == "fp":
        fv._cache_fp = None
    else:
        fv._warm_repo = None
    fv._restow_vite_cache()
    assert not (cdir / "node_modules" / ".vite").exists()
