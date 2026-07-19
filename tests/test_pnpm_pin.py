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
