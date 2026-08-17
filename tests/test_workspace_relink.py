# sig EDGEVERDICT_WORKSPACE_RELINK_TESTS_V1
# v11: a warm-cache restore carries the ROOT node_modules only; workspace
# repos also need per-package trees. After a restore on a workspace repo the
# gate re-runs the install with --offline (the store rode along, zero
# network); a failed relink invalidates the entry and falls through to a
# fresh install. 9 tests: detection (3), command shape (3), wiring (3).
from __future__ import annotations

import json

from edgeverdict.verifiers.finding_verifier import (
    _is_workspace_repo,
    _offline_relink_cmd,
    _strip_pm_noise,
)


# -- workspace detection ------------------------------------------------------

def test_pnpm_workspace_yaml_detected(tmp_path):
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n")
    assert _is_workspace_repo(str(tmp_path)) is True


def test_package_json_workspaces_detected(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "root", "workspaces": ["packages/*"]}))
    assert _is_workspace_repo(str(tmp_path)) is True


def test_plain_repo_is_not_workspace(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "plain"}))
    assert _is_workspace_repo(str(tmp_path)) is False
    assert _is_workspace_repo(str(tmp_path / "missing")) is False


# -- relink command shape -----------------------------------------------------

def test_relink_appends_offline_to_install():
    assert _offline_relink_cmd(["pnpm", "install", "--frozen-lockfile"]) == [
        "pnpm", "install", "--frozen-lockfile", "--offline"]


def test_relink_is_idempotent_when_already_offline():
    assert _offline_relink_cmd(["pnpm", "install", "--offline"]) is None


def test_relink_refuses_non_install_commands():
    assert _offline_relink_cmd(["pnpm", "run", "build"]) is None
    assert _offline_relink_cmd([]) is None


# -- wiring: restore -> relink -> (heal on failure) ---------------------------

class _Proc:
    def __init__(self, rc):
        self.returncode = rc
        self.stdout = ""
        self.stderr = ""


def _warm_env(tmp_path, monkeypatch, relink_rc):
    """A FindingVerifier driven straight through _ensure_warm with a
    pre-populated cache entry for a WORKSPACE repo, install/smoke stubbed.
    Returns (fv, ran_cmds, cache_entry_dir)."""
    from edgeverdict.verifiers.finding_verifier import (
        FindingVerifier,
        _dep_fingerprint,
    )
    monkeypatch.setenv("EDGEVERDICT_WARM_CACHE", "1")
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("EDGEVERDICT_WARM_CACHE_DIR", str(cache_root))
    repo_src = tmp_path / "repo"
    repo_src.mkdir()
    (repo_src / "pnpm-workspace.yaml").write_text("packages:\n  - 'p/*'\n")
    (repo_src / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
    (repo_src / "package.json").write_text('{"name":"root"}')
    tests_dir = repo_src / "p" / "a"
    tests_dir.mkdir(parents=True)
    (tests_dir / "a.test.ts").write_text("import { test } from 'vitest'\n")
    install_cmd = ["pnpm", "install", "--frozen-lockfile"]
    fp = _dep_fingerprint(str(repo_src), ".", install_cmd)
    assert fp
    cdir = cache_root / fp
    (cdir / "node_modules" / "pkg").mkdir(parents=True)
    (cdir / "node_modules" / "pkg" / "index.js").write_text("ok")
    (cdir / "deps.ok").write_text(fp)

    fv = FindingVerifier.__new__(FindingVerifier)
    fv.repo_root = str(repo_src)
    fv.project_dir = "."
    fv.tests_file = "p/a/a.test.ts"
    fv.timeout = 5
    fv.reuse_warm = False
    fv._warm_repo = None
    fv._warm_root = None
    fv._cache_fp = None
    fv._pristine_tests = None
    fv._prep_error = ""
    fv.log = lambda *a, **k: None

    class _P:
        env: dict[str, str] = {}
        install_cmd = ["pnpm", "install", "--frozen-lockfile"]
        build_cmd = None
        smoke_cmd = None
        smoke_probe = None
        install_fallback_cmd = None

    fv.profile = _P()
    ran: list[list[str]] = []

    def fake_run(args, cwd):
        ran.append(list(args))
        if "--offline" in args:
            return _Proc(relink_rc)
        return _Proc(0)

    fv._run = fake_run
    return fv, ran, cdir


def test_workspace_restore_triggers_offline_relink(tmp_path, monkeypatch):
    fv, ran, cdir = _warm_env(tmp_path, monkeypatch, relink_rc=0)
    fv._ensure_warm()
    assert fv._prep_error == ""
    assert any("--offline" in c for c in ran), "relink never ran"
    assert cdir.exists(), "healthy entry must not be invalidated"
    # cache hit held: no fresh (non-offline) install ran
    assert not any("--offline" not in c and "install" in c for c in ran)


def test_failed_relink_invalidates_and_falls_back(tmp_path, monkeypatch):
    fv, ran, cdir = _warm_env(tmp_path, monkeypatch, relink_rc=1)
    fv._ensure_warm()
    assert fv._prep_error == ""
    assert any("--offline" in c for c in ran)
    assert not (cdir / "node_modules").exists() or not (cdir / "deps.ok").exists(), \
        "failed relink must invalidate the cache entry"
    # fresh install ran after the fallback
    assert any("--offline" not in c and "install" in c for c in ran)


def test_init_noise_middle_line_is_filtered(tmp_path):
    # the v11 companion fix: a tini warning in the MIDDLE of a tail must not
    # mask the real cause (v1 only filtered npm warn/notice prefixes)
    tail = ("stderr: npm warn deprecated x\n"
            "[WARN  tini (7)] Tini is not running as PID 1\n"
            "Error: Cannot find module 'vitest'")
    out = _strip_pm_noise(tail)
    assert "tini" not in out.lower()
    assert "Cannot find module 'vitest'" in out
