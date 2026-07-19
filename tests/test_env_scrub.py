"""The gate executes model-written code; that code must never see the model
provider's credentials.

The gate is deterministic by design — no LLM in the pass/fail path — so
nothing it spawns (install, build, smoke, or an injected test) has any
legitimate use for OPENAI_API_KEY or ANTHROPIC_API_KEY. scrubbed_env is the
mechanical enforcement: every subprocess in both verifiers is built from it.
This matters most in CI, where the review step necessarily holds the key and
the checked-out PR code (plus generated tests) runs one step later.
"""

import subprocess
import sys

from agentboard.verifiers.vitest_verifier import scrubbed_env


def test_provider_keys_are_scrubbed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-should-never-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-leak")
    env = scrubbed_env({})
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_everything_else_survives(monkeypatch):
    # Narrow by design: only the model providers' keys are removed. Registry
    # tokens and ordinary env the install step may need are passed through,
    # and the profile's own env still wins over the ambient one.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-should-never-leak")
    monkeypatch.setenv("SOME_REGISTRY_TOKEN", "keep-me")
    env = scrubbed_env({"CI": "true"})
    assert env.get("SOME_REGISTRY_TOKEN") == "keep-me"
    assert env.get("CI") == "true"


def test_child_process_cannot_see_the_key(monkeypatch):
    # End to end at the process boundary: a child spawned with the scrubbed
    # env observes no key, exactly what an injected test would observe.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-should-never-leak")
    probe = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ.get('OPENAI_API_KEY'))"],
        env=scrubbed_env({}),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.stdout.strip() == "None"


def test_both_verifiers_route_through_scrubbed_env():
    # Wiring, not behavior: if a future _run rebuilds env from os.environ
    # directly, the scrub silently stops applying. Pin the call sites.
    import inspect

    from agentboard.verifiers import finding_verifier, vitest_verifier

    for cls in (finding_verifier.FindingVerifier, vitest_verifier.VitestVerifier):
        src = inspect.getsource(cls._run)
        assert "scrubbed_env" in src, f"{cls.__name__}._run bypasses scrubbed_env"
        assert "os.environ" not in src, f"{cls.__name__}._run rebuilds env directly"
