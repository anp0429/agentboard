# sig EDGEVERDICT_SCM_PRETEND_TESTS_V2
# HERMETIC by construction: every test clears SETUPTOOLS_SCM_PRETEND_VERSION
# from the process env before asserting anything about defaults. The v1 of
# this file asserted the default against a raw scrubbed_env() and passed in
# the container but FAILED on a machine whose shell exported the variable —
# the test was reading the shell, not the code. Never assert an env default
# without first deleting the ambient var.
from __future__ import annotations

from edgeverdict.execution import filtered_environment
from edgeverdict.verifiers.vitest_verifier import scrubbed_env

_VAR = "SETUPTOOLS_SCM_PRETEND_VERSION"


def test_default_pretend_version_is_set(monkeypatch):
    monkeypatch.delenv(_VAR, raising=False)
    env = scrubbed_env({})
    assert env[_VAR] == "0.0.0"


def test_ambient_shell_value_wins_over_default(monkeypatch):
    monkeypatch.setenv(_VAR, "9.9.9")
    env = scrubbed_env({})
    assert env[_VAR] == "9.9.9"


def test_profile_env_value_wins_over_default(monkeypatch):
    monkeypatch.delenv(_VAR, raising=False)
    env = scrubbed_env({_VAR: "1.2.3"})
    assert env[_VAR] == "1.2.3"


def test_sandbox_allowlist_passes_pretend_version(monkeypatch):
    monkeypatch.delenv(_VAR, raising=False)
    out = filtered_environment({_VAR: "0.0.0"})
    assert out[_VAR] == "0.0.0"


def test_provider_credentials_still_scrubbed(monkeypatch):
    # the scm default must not have loosened the credential scrub
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    env = scrubbed_env({})
    assert "OPENAI_API_KEY" not in env
