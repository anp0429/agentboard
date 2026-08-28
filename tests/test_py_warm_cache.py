# EDGEVERDICT_PY_WARM_CACHE_TESTS_V1
"""Python-lane warm cache (sig EDGEVERDICT_PY_WARM_CACHE_V1).

Until now the cross-run cache knew only JS lockfiles, so a python repo
(posthog) paid its full install on every run. The python lane installs
into a user-site under the warm root; cache it keyed on the resolver's
lockfile / requirements / pyproject, restore it on a hit, skip install.
"""
from __future__ import annotations

import os
import subprocess

from edgeverdict.verifiers.finding_verifier import (
    _PY_CACHE_TREE,
    _PYUSER_DIR,
    FindingVerifier,
    _dep_fingerprint,
)
from edgeverdict.verifiers.pytest_harness import PytestHarness
from edgeverdict.verifiers.vitest_verifier import RepoProfile


def _py_repo(tmp_path, **files):
    d = tmp_path / "repo"
    d.mkdir()
    for name, text in files.items():
        (d / name).write_text(text)
    return str(d)


# -- fingerprint keying -------------------------------------------------------

def test_pyproject_alone_is_enough_to_key(tmp_path):
    d = _py_repo(tmp_path, **{"pyproject.toml": "[project]\nname='x'\n"})
    assert _dep_fingerprint(d, ".", ["pip", "install"], kind="pytest") is not None


def test_uv_lock_change_changes_key(tmp_path):
    d = _py_repo(tmp_path, **{"pyproject.toml": "[project]\n", "uv.lock": "v1"})
    a = _dep_fingerprint(d, ".", ["pip", "install"], kind="pytest")
    (tmp_path / "repo" / "uv.lock").write_text("v2")
    b = _dep_fingerprint(d, ".", ["pip", "install"], kind="pytest")
    assert a is not None and a != b


def test_requirements_files_are_keyed(tmp_path):
    d = _py_repo(tmp_path, **{"requirements.txt": "a==1"})
    a = _dep_fingerprint(d, ".", ["pip", "install"], kind="pytest")
    (tmp_path / "repo" / "requirements-dev.txt").write_text("b==2")
    b = _dep_fingerprint(d, ".", ["pip", "install"], kind="pytest")
    assert a is not None and a != b


def test_python_kind_ignores_js_lockfiles(tmp_path):
    # a python repo that also ships a frontend lockfile (posthog) must key
    # on its python deps, not on pnpm-lock.yaml
    d = _py_repo(tmp_path, **{"pyproject.toml": "[project]\n", "pnpm-lock.yaml": "js1"})
    a = _dep_fingerprint(d, ".", ["pip", "install"], kind="pytest")
    (tmp_path / "repo" / "pnpm-lock.yaml").write_text("js2")
    b = _dep_fingerprint(d, ".", ["pip", "install"], kind="pytest")
    assert a == b


def test_python_kind_with_no_dep_files_returns_none(tmp_path):
    d = _py_repo(tmp_path, **{"README.md": "hi"})
    assert _dep_fingerprint(d, ".", ["pip", "install"], kind="pytest") is None


def test_js_keying_unchanged_by_kind_default(tmp_path):
    # the vitest lane must key exactly as before: pyproject present or not
    d = _py_repo(tmp_path, **{"pnpm-lock.yaml": "js1"})
    a = _dep_fingerprint(d, ".", ["pnpm", "install"])
    (tmp_path / "repo" / "pyproject.toml").write_text("[project]\n")
    b = _dep_fingerprint(d, ".", ["pnpm", "install"])
    assert a == b


# -- end-to-end round trip through _ensure_warm --------------------------------

def _profile():
    return RepoProfile(
        name="pyrepo",
        install_cmd=["python", "-m", "pip", "install", "--user", "-e", "."],
        test_base=["python", "-m", "pytest"],
        smoke_cmd=["python", "-m", "pytest", "--collect-only", "-q", "tests/test_x.py"],
        kind="pytest",
    )


class _Runner:
    """Stands in for the docker backend: the install populates the user-site
    under the warm root exactly where PYTHONUSERBASE would point."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, verifier, args, cwd):
        self.calls.append(list(args))
        if "install" in args:
            site = os.path.join(verifier._warm_root, _PYUSER_DIR, "lib",
                                "python3.12", "site-packages")
            os.makedirs(site, exist_ok=True)
            with open(os.path.join(site, "__editable__.pth"), "w") as fh:
                fh.write("/edgeverdict/repo/src\n")
        return subprocess.CompletedProcess(args=list(args), returncode=0,
                                           stdout="", stderr="")


def _verifier(repo, runner):
    v = FindingVerifier(repo, _profile(), "tests/test_x.py",
                        harness=PytestHarness(), log=lambda *_: None)
    v._run = lambda args, cwd: runner(v, args, cwd)  # type: ignore[method-assign]
    return v


def test_python_round_trip_restores_user_site_and_skips_install(tmp_path, monkeypatch):
    repo = _py_repo(tmp_path, **{"pyproject.toml": "[project]\nname='x'\n",
                                 "uv.lock": "v1"})
    os.makedirs(os.path.join(repo, "tests"))
    with open(os.path.join(repo, "tests", "test_x.py"), "w") as fh:
        fh.write("def test_ok():\n    assert True\n")
    monkeypatch.setenv("EDGEVERDICT_WARM_CACHE", "1")
    monkeypatch.setenv("EDGEVERDICT_WARM_CACHE_DIR", str(tmp_path / "cache"))

    # run 1: fresh install populates the user-site and the cache entry
    r1 = _Runner()
    v1 = _verifier(repo, r1)
    v1._ensure_warm()
    assert v1._prep_error == ""
    assert any("install" in c for c in r1.calls)
    entries = os.listdir(str(tmp_path / "cache"))
    assert len(entries) == 1
    cdir = tmp_path / "cache" / entries[0]
    assert (cdir / _PY_CACHE_TREE / "lib" / "python3.12" / "site-packages"
            / "__editable__.pth").is_file()
    assert (cdir / "deps.ok").is_file()
    smoke_markers = [n for n in os.listdir(cdir) if n.startswith("smoke.")]
    assert smoke_markers, "smoke ran on the fresh install and got vouched"
    v1.close()

    # run 2: same dep state -> user-site restored, install AND smoke skipped
    r2 = _Runner()
    v2 = _verifier(repo, r2)
    v2._ensure_warm()
    assert v2._prep_error == ""
    assert not any("install" in c for c in r2.calls), r2.calls
    assert not any("--collect-only" in c for c in r2.calls), r2.calls
    restored = os.path.join(v2._warm_root, _PYUSER_DIR, "lib", "python3.12",
                            "site-packages", "__editable__.pth")
    assert os.path.isfile(restored)
    v2.close()


def test_python_dep_change_misses_cache(tmp_path, monkeypatch):
    repo = _py_repo(tmp_path, **{"pyproject.toml": "[project]\n", "uv.lock": "v1"})
    os.makedirs(os.path.join(repo, "tests"))
    with open(os.path.join(repo, "tests", "test_x.py"), "w") as fh:
        fh.write("def test_ok():\n    assert True\n")
    monkeypatch.setenv("EDGEVERDICT_WARM_CACHE", "1")
    monkeypatch.setenv("EDGEVERDICT_WARM_CACHE_DIR", str(tmp_path / "cache"))
    v1 = _verifier(repo, _Runner())
    v1._ensure_warm()
    v1.close()
    (tmp_path / "repo" / "uv.lock").write_text("v2")
    r2 = _Runner()
    v2 = _verifier(repo, r2)
    v2._ensure_warm()
    assert any("install" in c for c in r2.calls)
    v2.close()


def test_python_smoke_failure_on_restored_site_self_heals(tmp_path, monkeypatch):
    repo = _py_repo(tmp_path, **{"pyproject.toml": "[project]\n"})
    os.makedirs(os.path.join(repo, "tests"))
    with open(os.path.join(repo, "tests", "test_x.py"), "w") as fh:
        fh.write("def test_ok():\n    assert True\n")
    monkeypatch.setenv("EDGEVERDICT_WARM_CACHE", "1")
    monkeypatch.setenv("EDGEVERDICT_WARM_CACHE_DIR", str(tmp_path / "cache"))
    v1 = _verifier(repo, _Runner())
    v1._ensure_warm()
    v1.close()
    # strip the per-project smoke marker so smoke reruns on the restored
    # site, and make that smoke fail once: the entry must be invalidated and
    # a fresh install must follow, same contract as the JS lane
    cdir = tmp_path / "cache" / os.listdir(str(tmp_path / "cache"))[0]
    for n in os.listdir(cdir):
        if n.startswith("smoke."):
            (cdir / n).unlink()

    class _Flaky(_Runner):
        def __init__(self):
            super().__init__()
            self.failed_once = False

        def __call__(self, verifier, args, cwd):
            res = super().__call__(verifier, args, cwd)
            if "--collect-only" in args and not self.failed_once:
                self.failed_once = True
                return subprocess.CompletedProcess(args=list(args), returncode=1,
                                                   stdout="", stderr="boom")
            return res

    r2 = _Flaky()
    v2 = _verifier(repo, r2)
    v2._ensure_warm()
    assert v2._prep_error == ""
    assert any("install" in c for c in r2.calls), "self-heal reinstalled"
    v2.close()
