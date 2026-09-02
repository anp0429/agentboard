# EDGEVERDICT_POSTHOG_SMOKE_TESTS_V1
"""Three fixes from the first PostHog collection attempt.

1. Diff-informed hosts (sig EDGEVERDICT_DIFF_HOSTS_V1): the change touched
   posthog/clickhouse/test/__snapshots__/test_schema.ambr, so test_schema.py
   is where logs32.py is exercised; the basename search picked
   posthog/settings/test_logs.py (logging settings) instead.
2. USER/LOGNAME in the sandbox env: pymysql calls getpass.getuser() at
   import and the sandbox uid has no passwd entry.
3. Cache on install success (sig EDGEVERDICT_CACHE_ON_INSTALL_V1): a smoke
   failure must not throw away a 355s install; heal at most once per entry.
"""
from __future__ import annotations

import os
import subprocess

from edgeverdict.config import host_for_target
from edgeverdict.config import tests_from_diff as _tests_from_diff
from edgeverdict.execution import filtered_environment
from edgeverdict.verifiers.finding_verifier import _PYUSER_DIR, FindingVerifier
from edgeverdict.verifiers.pytest_harness import PytestHarness
from edgeverdict.verifiers.vitest_verifier import RepoProfile


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True,
                   capture_output=True, text=True)


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(str(repo), "init", "-q", "-b", "master")
    _git(str(repo), "config", "user.email", "t@t")
    _git(str(repo), "config", "user.name", "t")
    for rel in ("posthog/clickhouse/logs/logs32.py",
                "posthog/clickhouse/test/test_schema.py",
                "posthog/clickhouse/test/__snapshots__/test_schema.ambr",
                "posthog/hogql/database/schema/logs.py",
                "posthog/hogql/database/test/test_database.py",
                "posthog/hogql/database/test/__snapshots__/test_database.ambr",
                "posthog/settings/test_logs.py",
                "frontend/src/a.ts", "frontend/src/a.test.ts",
                "frontend/src/__snapshots__/a.test.ts.snap"):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    _git(str(repo), "add", ".")
    _git(str(repo), "commit", "-q", "-m", "base")
    _git(str(repo), "checkout", "-q", "-b", "pr")
    for rel in ("posthog/clickhouse/logs/logs32.py",
                "posthog/clickhouse/test/__snapshots__/test_schema.ambr",
                "posthog/hogql/database/schema/logs.py",
                "posthog/hogql/database/test/__snapshots__/test_database.ambr",
                "frontend/src/a.ts", "frontend/src/__snapshots__/a.test.ts.snap"):
        (repo / rel).write_text("y\n")
    _git(str(repo), "commit", "-qam", "change")
    return str(repo)


def test_tests_from_diff_maps_snapshots_to_their_test_modules(tmp_path):
    repo = _repo(tmp_path)
    hosts = _tests_from_diff(repo, "master", "pr")
    assert hosts == [
        "frontend/src/a.test.ts",
        "posthog/clickhouse/test/test_schema.py",
        "posthog/hogql/database/test/test_database.py",
    ]


def test_host_for_target_prefers_nearest_diff_host_over_far_basename_match():
    hosts = ["posthog/clickhouse/test/test_schema.py",
             "posthog/hogql/database/test/test_database.py"]
    assert host_for_target("posthog/clickhouse/logs/logs32.py", hosts,
                           "posthog/settings/test_logs.py") == \
        "posthog/clickhouse/test/test_schema.py"
    assert host_for_target("posthog/hogql/database/schema/logs.py", hosts,
                           "posthog/settings/test_logs.py") == \
        "posthog/hogql/database/test/test_database.py"


def test_host_for_target_keeps_default_on_tie_or_when_diff_host_is_far():
    # co-located default shares everything: it wins over any diff host
    assert host_for_target("pkg/a/x.py", ["pkg/b/test_y.py"], "pkg/a/test_x.py") \
        == "pkg/a/test_x.py"
    # a diff host sharing fewer than two components never captures a target
    assert host_for_target("pkg/a/x.py", ["tests/test_everything.py"], "") == ""
    assert host_for_target("pkg/a/x.py", [], "") == ""


def test_sandbox_env_carries_a_username(monkeypatch):
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    env = filtered_environment({})
    assert env["USER"] == "edgeverdict"
    assert env["LOGNAME"] == "edgeverdict"


# -- cache on install success ------------------------------------------------

def _profile():
    return RepoProfile(
        name="pyrepo",
        install_cmd=["python", "-m", "pip", "install", "--user", "-e", "."],
        test_base=["python", "-m", "pytest"],
        smoke_cmd=["python", "-m", "pytest", "--collect-only", "-q", "tests/test_x.py"],
        kind="pytest",
    )


class _Runner:
    def __init__(self, smoke_rc=0):
        self.calls: list[list[str]] = []
        self.smoke_rc = smoke_rc

    def __call__(self, verifier, args, cwd):
        self.calls.append(list(args))
        if "install" in args:
            site = os.path.join(verifier._warm_root, _PYUSER_DIR, "lib",
                                "python3.13", "site-packages")
            os.makedirs(site, exist_ok=True)
            open(os.path.join(site, "x.pth"), "w").write("/edgeverdict/repo\n")
            return subprocess.CompletedProcess(args, 0, "", "")
        if "--collect-only" in args:
            return subprocess.CompletedProcess(args, self.smoke_rc, "",
                                               "boom" if self.smoke_rc else "")
        return subprocess.CompletedProcess(args, 0, "", "")


def _verifier(repo, runner):
    v = FindingVerifier(repo, _profile(), "tests/test_x.py",
                        harness=PytestHarness(), log=lambda *_: None)
    v._run = lambda args, cwd: runner(v, args, cwd)  # type: ignore[method-assign]
    return v


def _pyrepo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    (repo / "uv.lock").write_text("v1\n")
    (repo / "tests" / "test_x.py").write_text("def test_ok():\n    assert True\n")
    monkeypatch.setenv("EDGEVERDICT_WARM_CACHE", "1")
    monkeypatch.setenv("EDGEVERDICT_WARM_CACHE_DIR", str(tmp_path / "cache"))
    return str(repo)


def test_install_is_cached_even_when_smoke_fails_and_heals_once(tmp_path, monkeypatch):
    repo = _pyrepo(tmp_path, monkeypatch)
    # run 1: install ok, smoke fails (env) -> deps still banked, heal marker set
    r1 = _Runner(smoke_rc=1)
    v1 = _verifier(repo, r1)
    v1._ensure_warm()
    assert v1._prep_error.startswith("environment smoke probe failed")
    entries = os.listdir(str(tmp_path / "cache"))
    assert len(entries) == 1
    cdir = tmp_path / "cache" / entries[0]
    assert (cdir / "deps.ok").is_file()
    assert (cdir / "heal.tried").is_file()
    assert not [n for n in os.listdir(cdir) if n.startswith("smoke.")]
    v1.close()
    # run 2: restored, smoke fails again -> NO reinstall (heal already tried)
    r2 = _Runner(smoke_rc=1)
    v2 = _verifier(repo, r2)
    v2._ensure_warm()
    assert v2._prep_error.startswith("environment smoke probe failed")
    assert not any("install" in c for c in r2.calls), r2.calls
    v2.close()
    # run 3: env fixed, smoke passes on the restored site -> vouched, marker cleared
    r3 = _Runner(smoke_rc=0)
    v3 = _verifier(repo, r3)
    v3._ensure_warm()
    assert v3._prep_error == ""
    assert not any("install" in c for c in r3.calls)
    assert [n for n in os.listdir(cdir) if n.startswith("smoke.")]
    v3.close()


def test_fresh_entry_still_heals_once_on_restored_smoke_failure(tmp_path, monkeypatch):
    repo = _pyrepo(tmp_path, monkeypatch)
    v1 = _verifier(repo, _Runner(smoke_rc=0))
    v1._ensure_warm()
    assert v1._prep_error == ""
    v1.close()
    cdir = tmp_path / "cache" / os.listdir(str(tmp_path / "cache"))[0]
    for n in os.listdir(cdir):
        if n.startswith("smoke."):
            (cdir / n).unlink()
    # restored entry, smoke fails: no heal marker -> one reinstall attempt
    r2 = _Runner(smoke_rc=1)
    v2 = _verifier(repo, r2)
    v2._ensure_warm()
    assert sum(1 for c in r2.calls if "install" in c) == 1
    assert (cdir / "heal.tried").is_file()
    v2.close()
