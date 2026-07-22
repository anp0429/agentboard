"""The pnpm version the gate installs with must be one the repo's own config
parses under.

Found on pathe (benchmark row 4): pnpm 10/11 repos use pnpm-workspace.yaml as
a plain config file with no `packages` field, and pnpm 9 rejects that as a
broken workspace ("ERR packages field missing or empty") — the hardcoded
pnpm@9 pin that once SAVED runs from ancient pnpm 7 (ERR_INVALID_THIS on
Node 22) had aged into the same class of failure it fixed. The rule these
tests pin: honor the repo's packageManager when it is modern (>= 9), fall
back to 9 otherwise.
"""

import json
import os

from agentboard.config import Config, build_profile, detect_pnpm_version


def _repo(tmp_path, package_json: dict, lockfile: bool = True):
    (tmp_path / "package.json").write_text(json.dumps(package_json))
    if lockfile:
        (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    return str(tmp_path)


def test_modern_pin_is_honored(tmp_path):
    root = _repo(tmp_path, {"packageManager": "pnpm@11.9.0"})
    assert detect_pnpm_version(root) == "11.9.0"


def test_integrity_hash_suffix_is_stripped(tmp_path):
    # jotai pins pnpm@11.3.0+sha512...; npx wants a plain version.
    root = _repo(tmp_path, {"packageManager": "pnpm@11.3.0+sha512.2c403d659452"})
    assert detect_pnpm_version(root) == "11.3.0"


def test_ancient_pin_falls_back_to_9(tmp_path):
    # pnpm 7 cannot run on Node 22 (ERR_INVALID_THIS) — the original reason
    # the pin exists. Old pins are NOT honored.
    root = _repo(tmp_path, {"packageManager": "pnpm@7.9.5"})
    assert detect_pnpm_version(root) == "9"


def test_no_pin_falls_back_to_9(tmp_path):
    assert detect_pnpm_version(_repo(tmp_path, {"name": "x"})) == "9"


def test_non_pnpm_pin_falls_back_to_9(tmp_path):
    root = _repo(tmp_path, {"packageManager": "yarn@4.5.0"})
    assert detect_pnpm_version(root) == "9"


def test_missing_package_json_falls_back_to_9(tmp_path):
    assert detect_pnpm_version(str(tmp_path)) == "9"


def test_build_profile_threads_the_version_through(tmp_path):
    root = _repo(tmp_path, {"packageManager": "pnpm@11.9.0"})
    prof = build_profile(root, Config(), tests_file="x.test.ts")
    assert "pnpm@11.9.0" in " ".join(prof.install_cmd)
    assert "pnpm@11.9.0" in " ".join(prof.test_base)
    # the smoke probe must use the same toolchain as the real runs
    assert prof.smoke_cmd is None or "pnpm@11.9.0" in " ".join(prof.smoke_cmd)


def test_build_profile_default_is_unchanged(tmp_path):
    root = _repo(tmp_path, {"name": "plain"})
    prof = build_profile(root, Config(), tests_file="x.test.ts")
    assert "pnpm@9" in " ".join(prof.install_cmd)


# ---------------------------------------------------------------------------
# lockfile honesty: a shipped lockfile is installed frozen, drift is loud
# ---------------------------------------------------------------------------

def test_lockfile_repo_installs_frozen(tmp_path):
    # A repo that ships pnpm-lock.yaml gets the dependency set it pins;
    # --no-frozen-lockfile silently resolved whatever the registry served
    # that day, so verdicts could drift with upstream releases.
    root = _repo(tmp_path, {"name": "x"})
    prof = build_profile(root, Config(), tests_file="x.test.ts")
    assert "--frozen-lockfile" in prof.install_cmd
    assert "--no-frozen-lockfile" not in prof.install_cmd


def test_no_lockfile_repo_installs_unfrozen(tmp_path):
    root = _repo(tmp_path, {"name": "x"}, lockfile=False)
    prof = build_profile(root, Config(), tests_file="x.test.ts")
    assert "--no-frozen-lockfile" in prof.install_cmd


def test_unfrozen_install_swaps_only_the_flag():
    from agentboard.verifiers.vitest_verifier import unfrozen_install

    frozen = ["npx", "-y", "pnpm@9", "install", "--frozen-lockfile"]
    assert unfrozen_install(frozen) == [
        "npx", "-y", "pnpm@9", "install", "--no-frozen-lockfile"]
    # nothing to fall back to when the install was never frozen
    assert unfrozen_install(["npm", "ci"]) is None
    assert unfrozen_install(["npx", "-y", "pnpm@9", "install",
                             "--no-frozen-lockfile"]) is None


def test_frozen_install_failure_falls_back_with_a_note(tmp_path):
    # A stale lockfile must degrade the run to the permissive install (with
    # one printed line saying so), never bench it as an env failure.
    import subprocess

    from agentboard.verifiers.finding_verifier import FindingVerifier
    from agentboard.verifiers.vitest_verifier import RepoProfile

    repo = str(tmp_path / "repo")
    os.makedirs(os.path.join(repo, "tests"))
    with open(os.path.join(repo, "tests", "suite.test.ts"), "w") as fh:
        fh.write("describe('d', () => {\n});\n")
    profile = RepoProfile(
        name="fixture",
        install_cmd=["pnpm", "install", "--frozen-lockfile"],
        test_base=["true"], build_cmd=None, env={}, smoke_cmd=None,
    )
    calls, lines = [], []
    v = FindingVerifier(repo, profile, "tests/suite.test.ts", log=lines.append)

    def fake_run(args, cwd):
        calls.append(list(args))
        rc = 1 if "--frozen-lockfile" in args else 0
        return subprocess.CompletedProcess(
            args, rc, stdout="", stderr="ERR_PNPM_OUTDATED_LOCKFILE")

    v._run = fake_run
    try:
        v._ensure_warm()
        assert v._prep_error == ""
        assert calls[0] == ["pnpm", "install", "--frozen-lockfile"]
        assert calls[1] == ["pnpm", "install", "--no-frozen-lockfile"]
        assert any("retrying with --no-frozen-lockfile" in ln for ln in lines)
    finally:
        v.close()


# --- toolchain-manager pins (mise.toml / .tool-versions) ---------------------
# Found on supabase/mcp: the repo dropped packageManager entirely and pins
# pnpm in mise.toml ([tools] pnpm = "10") while its pnpm-workspace.yaml uses
# pnpm-10 fields. Falling back to 9 ran the repo under a pnpm its config was
# never written for. Rule: packageManager wins when present; otherwise a
# modern mise/.tool-versions pin is honored; otherwise 9.


def test_mise_pin_is_honored_when_package_json_has_no_pin(tmp_path):
    root = _repo(tmp_path, {"name": "x"})
    (tmp_path / "mise.toml").write_text('[tools]\nnode = "lts"\npnpm = "10"\n')
    assert detect_pnpm_version(root) == "10"


def test_mise_table_value_is_honored(tmp_path):
    root = _repo(tmp_path, {"name": "x"})
    (tmp_path / "mise.toml").write_text('[tools]\npnpm = { version = "10.12.1" }\n')
    assert detect_pnpm_version(root) == "10.12.1"


def test_mise_list_value_takes_first_entry(tmp_path):
    root = _repo(tmp_path, {"name": "x"})
    (tmp_path / "mise.toml").write_text('[tools]\npnpm = ["10.4.0", "9.15.0"]\n')
    assert detect_pnpm_version(root) == "10.4.0"


def test_mise_channel_pin_falls_back_to_9(tmp_path):
    # "latest"/"lts" are moving targets npx can't resolve as pnpm@<v>
    root = _repo(tmp_path, {"name": "x"})
    (tmp_path / "mise.toml").write_text('[tools]\npnpm = "latest"\n')
    assert detect_pnpm_version(root) == "9"


def test_mise_ancient_pin_falls_back_to_9(tmp_path):
    root = _repo(tmp_path, {"name": "x"})
    (tmp_path / "mise.toml").write_text('[tools]\npnpm = "7"\n')
    assert detect_pnpm_version(root) == "9"


def test_package_manager_pin_beats_mise(tmp_path):
    root = _repo(tmp_path, {"packageManager": "pnpm@11.9.0"})
    (tmp_path / "mise.toml").write_text('[tools]\npnpm = "10"\n')
    assert detect_pnpm_version(root) == "11.9.0"


def test_tool_versions_pin_is_honored(tmp_path):
    root = _repo(tmp_path, {"name": "x"})
    (tmp_path / ".tool-versions").write_text("node 22.11.0\npnpm 10.12.1\n")
    assert detect_pnpm_version(root) == "10.12.1"


def test_mise_beats_tool_versions(tmp_path):
    root = _repo(tmp_path, {"name": "x"})
    (tmp_path / "mise.toml").write_text('[tools]\npnpm = "10"\n')
    (tmp_path / ".tool-versions").write_text("pnpm 9.1.0\n")
    assert detect_pnpm_version(root) == "10"


def test_broken_mise_toml_falls_back_to_9(tmp_path):
    root = _repo(tmp_path, {"name": "x"})
    (tmp_path / "mise.toml").write_text("[tools\npnpm = ")
    assert detect_pnpm_version(root) == "9"
