"""pnpm --filter path strip — the fix for the whole-workspace-scan hang.

sig EDGEVERDICT_FILTER_PATH_STRIP_V1

`pnpm --filter <pkg> exec vitest` runs vitest with cwd INSIDE the package dir.
A positional test path handed to that command must be package-relative; a
repo-relative path (apps/studio/data/x) doubles the prefix
(apps/studio/apps/studio/data/x), vitest finds nothing, and silently scans the
whole workspace (~170 workers, ~475s). The verifier strips profile.pkg_dir from
the command path; injection still uses the full repo-relative path.
"""
from __future__ import annotations

from edgeverdict.verifiers.finding_verifier import FindingVerifier
from edgeverdict.verifiers.harness import harness_for_profile
from edgeverdict.verifiers.vitest_verifier import RepoProfile


def test_filter_strips_pkg_dir_from_command_path():
    prof = RepoProfile.pnpm_vitest("supabase", filter="studio",
                                   pnpm_version="11", frozen=True,
                                   pkg_dir="apps/studio")
    tf = "apps/studio/data/content/notebooks/notebook-operations.test.ts"
    v = FindingVerifier("/tmp/x", prof, tf,
                        harness=harness_for_profile(prof), log=lambda *a: None)
    # injection path stays repo-relative (write base is the repo root)
    assert v.tests_file == tf
    # command path is package-relative (cwd is inside apps/studio)
    assert v._cmd_tests_file == "data/content/notebooks/notebook-operations.test.ts"


def test_command_contains_stripped_path():
    prof = RepoProfile.pnpm_vitest("supabase", filter="studio",
                                   pnpm_version="11", frozen=True,
                                   pkg_dir="apps/studio")
    tf = "apps/studio/data/x.test.ts"
    h = harness_for_profile(prof)
    v = FindingVerifier("/tmp/x", prof, tf, harness=h, log=lambda *a: None)
    cmd = h.batch_command(prof, v._cmd_tests_file, "___ab", "/tmp/o")
    assert "data/x.test.ts" in cmd
    assert "apps/studio/data/x.test.ts" not in cmd


def test_no_filter_keeps_repo_relative_path():
    # no filter, no pkg_dir -> path unchanged (root-exec repos like zod)
    prof = RepoProfile.pnpm_vitest("zod", pnpm_version="11", frozen=True)
    tf = "src/x.test.ts"
    v = FindingVerifier("/tmp/x", prof, tf,
                        harness=harness_for_profile(prof), log=lambda *a: None)
    assert v._cmd_tests_file == "src/x.test.ts"


def test_file_outside_pkg_dir_not_stripped():
    # defensive: a test path not under pkg_dir must not become ../.. garbage
    prof = RepoProfile.pnpm_vitest("supabase", filter="studio",
                                   pnpm_version="11", frozen=True,
                                   pkg_dir="apps/studio")
    tf = "packages/other/x.test.ts"
    v = FindingVerifier("/tmp/x", prof, tf,
                        harness=harness_for_profile(prof), log=lambda *a: None)
    # not under apps/studio -> left as the repo-relative path, not ../../packages
    assert v._cmd_tests_file == "packages/other/x.test.ts"
