"""Environment preflight + first-failure triage.

(sig EDGEVERDICT_ENV_PREFLIGHT_V1)

The failure classes under test each cost real minutes in the wild before
these catches existed: a networked install under a no-network policy burned
~140s of DNS backoff per rung; an OOM-killed monorepo install under default
2g/512m limits burned 90-165s per attempt with the cause swallowed; the
frozen-lockfile retry hid pnpm's actual error every time. Each catch must
fire in effectively zero time and NAME its fix.
"""

from typing import ClassVar

from edgeverdict.verifiers.finding_verifier import (
    FindingVerifier,
    _install_failure_class,
    _limits_at_defaults,
)


class _Proc:
    def __init__(self, rc, stderr="", stdout=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


# -- failure classification ---------------------------------------------------

def test_dns_failures_classified_network():
    assert _install_failure_class(
        _Proc(1, stderr="npm error errno EAI_AGAIN")) == "network"
    assert _install_failure_class(
        _Proc(1, stderr="getaddrinfo ENOTFOUND registry")) == "network"


def test_resource_failures_classified():
    assert _install_failure_class(_Proc(137)) == "resources"
    assert _install_failure_class(
        _Proc(1, stderr="FATAL ERROR: JS heap out of memory")) == "resources"
    assert _install_failure_class(
        _Proc(1, stderr="ENOSPC: no space left on device")) == "resources"


def test_ordinary_failures_keep_retry_ladder():
    assert _install_failure_class(
        _Proc(1, stderr="ERR_PNPM_OUTDATED_LOCKFILE")) == ""
    assert _install_failure_class(_Proc(0)) == ""


def test_default_limits_detected():
    class _L:
        memory = "2g"
        tmpfs_size = "512m"

    class _B:
        limits = _L()

    assert _limits_at_defaults(_B())
    _L.memory = "8g"
    assert not _limits_at_defaults(_B())
    assert not _limits_at_defaults(object())


# -- wiring through _ensure_warm ---------------------------------------------

def _fv(tmp_path, monkeypatch, policy, rc=0, stderr="", workspace=False,
        limits=None):
    monkeypatch.delenv("EDGEVERDICT_WARM_CACHE", raising=False)
    repo_src = tmp_path / "repo"
    repo_src.mkdir()
    (repo_src / "package.json").write_text('{"name":"r"}')
    if workspace:
        (repo_src / "pnpm-workspace.yaml").write_text("packages:\n  - p/*\n")
    tests_dir = repo_src / "src"
    tests_dir.mkdir()
    (tests_dir / "a.test.ts").write_text("import { test } from 'vitest'\n")

    fv = FindingVerifier.__new__(FindingVerifier)
    fv.repo_root = str(repo_src)
    fv.project_dir = "."
    fv.tests_file = "src/a.test.ts"
    fv.timeout = 5
    fv.reuse_warm = False
    fv._warm_repo = None
    fv._warm_root = None
    fv._cache_fp = None
    fv._pristine_tests = None
    fv._materialized_restore = False
    fv._prep_error = ""
    logs: list[str] = []
    fv.log = lambda *a, **k: logs.append(" ".join(str(x) for x in a))

    class _B:
        network_policy = policy

    if limits is not None:
        _B.limits = limits
    fv._execution_backend = _B()

    class _P:
        env: ClassVar[dict[str, str]] = {}
        install_cmd: ClassVar[list[str]] = [
            "npx", "pnpm@11", "install", "--frozen-lockfile"]
        build_cmd = None
        smoke_cmd = None
        smoke_probe = None
        install_fallback_cmd = None

    fv.profile = _P()
    ran: list[list[str]] = []

    def fake_run(args, cwd):
        ran.append(list(args))
        return _Proc(rc, stderr)

    fv._run = fake_run
    return fv, ran, logs


def test_preflight_refuses_install_without_network(tmp_path, monkeypatch):
    fv, ran, _ = _fv(tmp_path, monkeypatch, policy="none")
    fv._ensure_warm()
    assert "EDGEVERDICT_SANDBOX_NETWORK=install" in fv._prep_error
    assert ran == [], "no install command may run under a none policy"


def test_preflight_lets_consented_install_run(tmp_path, monkeypatch):
    fv, ran, _ = _fv(tmp_path, monkeypatch, policy="install")
    fv._ensure_warm()
    assert fv._prep_error == ""
    assert any("install" in c for c in ran)


def test_network_death_skips_retry_ladder(tmp_path, monkeypatch):
    fv, ran, _ = _fv(tmp_path, monkeypatch, policy="install", rc=1,
                     stderr="npm error errno EAI_AGAIN")
    fv._ensure_warm()
    assert "EDGEVERDICT_SANDBOX_NETWORK" in fv._prep_error
    assert len([c for c in ran if "install" in c]) == 1


def test_oom_death_names_memory_vars(tmp_path, monkeypatch):
    fv, ran, _ = _fv(tmp_path, monkeypatch, policy="install", rc=137)
    fv._ensure_warm()
    assert "EDGEVERDICT_SANDBOX_MEMORY" in fv._prep_error
    assert "EDGEVERDICT_TMPFS_SIZE" in fv._prep_error
    assert len([c for c in ran if "install" in c]) == 1


def test_frozen_retry_logs_its_cause(tmp_path, monkeypatch):
    fv, ran, logs = _fv(tmp_path, monkeypatch, policy="install", rc=1,
                        stderr="ERR_PNPM_OUTDATED_LOCKFILE lockfile is stale")
    fv._ensure_warm()
    assert any("frozen failure cause" in ln
               and "ERR_PNPM_OUTDATED_LOCKFILE" in ln for ln in logs)
    assert len([c for c in ran if "install" in c]) >= 2


def test_workspace_on_default_limits_warns_upfront(tmp_path, monkeypatch):
    class _L:
        memory = "2g"
        tmpfs_size = "512m"

    fv, _ran, logs = _fv(tmp_path, monkeypatch, policy="install",
                         workspace=True, limits=_L())
    fv._ensure_warm()
    assert any("EDGEVERDICT_SANDBOX_MEMORY=8g" in ln for ln in logs)
