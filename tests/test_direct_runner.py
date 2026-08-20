"""Direct vitest runner — bypass pnpm-exec per-call overhead.

sig EDGEVERDICT_DIRECT_RUNNER_V2

`pnpm exec vitest` re-runs pnpm's workspace/lockfile resolution on every call
(~76s on supabase/studio, paid by the batch AND each confirm lane). After
install links node_modules/.bin/vitest, the verifier swaps test_base to call
that binary directly (~0.8s). With a --filter, the binary lives in the package
dir and the run happens there (cwd = pkg_dir, package-relative path). Install
stays via pnpm; only the per-test RUN goes direct.
"""
from __future__ import annotations

import os

from edgeverdict.verifiers.finding_verifier import FindingVerifier
from edgeverdict.verifiers.harness import harness_for_profile
from edgeverdict.verifiers.vitest_verifier import RepoProfile


def _bin(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\necho vitest\n")
    os.chmod(path, 0o755)


def test_filter_case_binary_in_package_dir(tmp_path):
    repo = str(tmp_path / "repo")
    _bin(os.path.join(repo, "apps", "studio", "node_modules", ".bin", "vitest"))
    prof = RepoProfile.pnpm_vitest("supabase", filter="studio",
                                   pnpm_version="11", frozen=True,
                                   pkg_dir="apps/studio")
    v = FindingVerifier(repo, prof, "apps/studio/x.test.ts",
                        harness=harness_for_profile(prof), log=lambda *a: None)
    assert prof.test_base[:1] == ["npx"]
    v._resolve_direct_runner(repo)
    # test_base now the direct binary, no pnpm/exec/filter wrapper
    assert prof.test_base[0].endswith("node_modules/.bin/vitest")
    assert prof.test_base[1] == "run"
    assert "npx" not in prof.test_base
    assert "exec" not in prof.test_base
    assert "--filter" not in prof.test_base
    # and the run happens FROM the package dir
    assert v._direct_runner_active is True
    assert os.path.relpath(v._workdir(repo), repo) == "apps/studio"


def test_full_command_matches_manual_fast_run(tmp_path):
    # the exact shape proven at 0.8s by hand: <bin> run <pkg-rel path> -t mark
    repo = str(tmp_path / "repo")
    _bin(os.path.join(repo, "apps", "studio", "node_modules", ".bin", "vitest"))
    prof = RepoProfile.pnpm_vitest("supabase", filter="studio",
                                   pnpm_version="11", frozen=True,
                                   pkg_dir="apps/studio")
    tf = "apps/studio/data/content/notebooks/notebook-operations.test.ts"
    h = harness_for_profile(prof)
    v = FindingVerifier(repo, prof, tf, harness=h, log=lambda *a: None)
    v._resolve_direct_runner(repo)
    cmd = h.batch_command(prof, v._cmd_tests_file, "___ab", "/tmp/o")
    assert cmd[0].endswith("node_modules/.bin/vitest")
    assert cmd[1] == "run"
    assert "data/content/notebooks/notebook-operations.test.ts" in cmd
    assert "apps/studio/data/content" not in " ".join(cmd)


def test_preserves_project_flag(tmp_path):
    repo = str(tmp_path / "repo")
    _bin(os.path.join(repo, "pkg", "node_modules", ".bin", "vitest"))
    prof = RepoProfile.pnpm_vitest("z", filter="z", project="unit",
                                   pnpm_version="9", frozen=True, pkg_dir="pkg")
    v = FindingVerifier(repo, prof, "pkg/a.test.ts",
                        harness=harness_for_profile(prof), log=lambda *a: None)
    v._resolve_direct_runner(repo)
    assert prof.test_base[-2:] == ["--project", "unit"]


def test_no_binary_keeps_pnpm_fallback(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(os.path.join(repo, "pkg"))
    prof = RepoProfile.pnpm_vitest("z", filter="z", pnpm_version="11",
                                   frozen=True, pkg_dir="pkg")
    before = list(prof.test_base)
    v = FindingVerifier(repo, prof, "pkg/a.test.ts",
                        harness=harness_for_profile(prof), log=lambda *a: None)
    v._resolve_direct_runner(repo)
    assert prof.test_base == before  # unchanged, still runnable via pnpm
    assert getattr(v, "_direct_runner_active", False) is False


def test_install_cmd_unchanged(tmp_path):
    # install stays via pnpm — only the per-test RUN goes direct
    repo = str(tmp_path / "repo")
    _bin(os.path.join(repo, "pkg", "node_modules", ".bin", "vitest"))
    prof = RepoProfile.pnpm_vitest("z", filter="z", pnpm_version="11",
                                   frozen=True, pkg_dir="pkg")
    install_before = list(prof.install_cmd)
    v = FindingVerifier(repo, prof, "pkg/a.test.ts",
                        harness=harness_for_profile(prof), log=lambda *a: None)
    v._resolve_direct_runner(repo)
    assert prof.install_cmd == install_before
    assert prof.install_cmd[:1] == ["npx"]


def test_non_filter_repo_resolves_from_workdir(tmp_path):
    # root-exec repos (zod): no filter, binary at repo root node_modules
    repo = str(tmp_path / "repo")
    _bin(os.path.join(repo, "node_modules", ".bin", "vitest"))
    prof = RepoProfile.pnpm_vitest("zod", pnpm_version="11", frozen=True)
    v = FindingVerifier(repo, prof, "src/a.test.ts",
                        harness=harness_for_profile(prof), log=lambda *a: None)
    v._resolve_direct_runner(repo)
    assert prof.test_base[0].endswith("node_modules/.bin/vitest")
    assert v._direct_runner_active is True


def test_confirm_copy_uses_its_own_binary(tmp_path):
    # the confirm-lane fix: each private repo copy must run ITS OWN vitest
    # binary, not the original warm repo's (absolute-path cross-repo bug).
    repo = str(tmp_path / "warm")
    _bin(os.path.join(repo, "apps", "studio", "node_modules", ".bin", "vitest"))
    prof = RepoProfile.pnpm_vitest("supabase", filter="studio",
                                   pnpm_version="11", frozen=True,
                                   pkg_dir="apps/studio")
    v = FindingVerifier(repo, prof, "apps/studio/x.test.ts",
                        harness=harness_for_profile(prof), log=lambda *a: None)
    v._resolve_direct_runner(repo)
    # stored relative, so it can rebind per repo
    assert v._direct_runner_rel == "apps/studio/node_modules/.bin/vitest"
    # a confirm lane's copy binds the binary UNDER the copy, not the warm repo
    copy = str(tmp_path / "confirm_lane")
    tb = v._direct_test_base(copy)
    assert tb[0].startswith(copy)
    assert not tb[0].startswith(repo)
    assert tb[0].endswith("apps/studio/node_modules/.bin/vitest")
