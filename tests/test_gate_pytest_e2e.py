"""End-to-end gate test, Python edition -- "agentboard can gate Python".

The structural twin of tests/test_gate_e2e.py: the REAL FindingVerifier,
driven by the REAL PytestHarness, against a real pytest project
(tests/fixtures/py_target) with one planted clamp bug, producing all four
verdict classes from actual process execution:

    handled        <- a proposed test the tool already satisfies
    confirmed_gap  <- a proposed edge test exposing the planted clamp bug
    broken_test    <- a proposed test that references an undefined name
    timed_out      <- a proposed test that sleeps past the subprocess limit

Then the whole run repeats from scratch (fresh warm base) and the verdict
fingerprint must be byte-identical. Same product claim as the JS file, new
runtime: same suite, same target, same verdicts, any run.

The profile is built through config.build_profile, not by hand, so this
also proves the detection path end to end: pytest.ini -> kind "pytest" ->
python profile -> PytestHarness selected from the profile.

Two timing notes. There is no npm install here (the python profile assumes
the running environment provides pytest -- it is running this very file),
so a run costs seconds. And the hanging proposal exercises the design's
worst case on purpose: pytest has no per-test timeout, so the BATCH hits
the subprocess limit, every finding falls back to the proven serial path,
and the hang then times out its own serial run. The verdicts must come out
identical anyway.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

from agentboard.config import Config, build_profile
from agentboard.fingerprint import verdict_fingerprint
from agentboard.review import ReviewFinding, ReviewRun
from agentboard.verifiers.finding_verifier import FindingVerifier
from agentboard.verifiers.harness import harness_for_profile
from agentboard.verifiers.pytest_harness import PytestHarness

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "py_target")

# The JS twin skips when node/npm are absent. The python gate's toolchain is
# the interpreter running this suite, so this guard can never trip; it
# exists to keep the two e2e files structurally parallel.
pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("pytest") is None, reason="pytest not importable"
)

# generous enough for a serial pytest run (~1s), short enough that the
# deliberate hang costs seconds, not the default half hour
TIMEOUT = 8


def _profile():
    # through build_profile on purpose: pytest.ini in the fixture must be
    # what selects the python profile (detection is part of the proof)
    profile = build_profile(FIXTURE, Config(), "test_order_tool.py")
    assert profile.kind == "pytest"
    return profile


def _findings() -> list[ReviewFinding]:
    return [
        ReviewFinding(
            behavior="in-range page sizes are honored",
            test_code=(
                "def test_honors_in_range_page_size():\n"
                "    assert len(find_orders(ORDERS, 'open', 2)) == 2\n"
            ),
        ),
        ReviewFinding(
            behavior="a request for exactly the maximum page size is honored",
            test_code=(
                # carries its own import: the host file never imports
                # clamp_page_size, and the pytest harness must keep it
                "from order_tool import clamp_page_size\n"
                "\n"
                "def test_clamp_keeps_inclusive_upper_bound():\n"
                "    assert clamp_page_size(50, 1, 50) == 50\n"
            ),
        ),
        ReviewFinding(
            behavior="a defective proposal cannot manufacture a gap",
            test_code=(
                "def test_references_a_missing_name():\n"
                "    assert totally_undefined_helper() is True\n"
            ),
        ),
        ReviewFinding(
            behavior="a hang is reported as ambiguity, not as anything else",
            test_code=(
                "import time\n"
                "\n"
                "def test_never_finishes():\n"
                "    time.sleep(300)\n"
            ),
        ),
    ]


def _run_gate(batch: bool = True) -> ReviewRun:
    profile = _profile()
    run = ReviewRun(intent="demo", target="order_tool.py", findings=_findings())
    verifier = FindingVerifier(
        FIXTURE, profile, tests_file="test_order_tool.py", timeout=TIMEOUT,
        harness=harness_for_profile(profile),
    )
    return verifier.run(run, batch=batch)


def test_profile_selects_the_pytest_harness():
    assert isinstance(harness_for_profile(_profile()), PytestHarness)


def test_gate_end_to_end_all_four_verdicts_and_identical_fingerprints():
    first = _run_gate()

    by_behavior = {f.behavior: f for f in first.findings}
    assert by_behavior["in-range page sizes are honored"].status == "handled"
    gap = by_behavior["a request for exactly the maximum page size is honored"]
    assert gap.status == "confirmed_gap", gap.observed
    assert by_behavior[
        "a defective proposal cannot manufacture a gap"
    ].status == "broken_test"
    hang = by_behavior["a hang is reported as ambiguity, not as anything else"]
    assert hang.status == "timed_out", hang.observed
    assert "did not finish" in hang.observed

    # the whole thing again: fresh warm base, same verdicts
    second = _run_gate()
    assert verdict_fingerprint(first) == verdict_fingerprint(second)


def test_fixture_baseline_is_green():
    """The planted bug must NOT break the shipped suite -- same demo story
    as the JS target: existing tests pass, the gate still finds the edge."""
    profile = _profile()
    run = ReviewRun(
        intent="baseline", target="order_tool.py",
        findings=[ReviewFinding(
            behavior="existing behavior holds",
            test_code=(
                "def test_existing_filter_still_works():\n"
                "    assert [o['id'] for o in find_orders(ORDERS, 'closed', 5)]"
                " == [3]\n"
            ),
        )],
    )
    verifier = FindingVerifier(
        FIXTURE, profile, tests_file="test_order_tool.py", timeout=TIMEOUT,
        harness=harness_for_profile(profile),
    )
    out = verifier.run(run)
    assert out.findings[0].status == "handled", out.findings[0].observed


def test_falsifier_can_never_be_handled():
    """The zod invariant, ported: an injected `assert 1 == 2` must NEVER
    classify as handled. The gate's word is only worth anything if an
    impossible test provably executes and fails."""
    profile = _profile()
    run = ReviewRun(
        intent="falsifier", target="order_tool.py",
        findings=[ReviewFinding(
            behavior="an impossible assertion must fail at runtime",
            test_code="def test_falsifier():\n    assert 1 == 2\n",
        )],
    )
    verifier = FindingVerifier(
        FIXTURE, profile, tests_file="test_order_tool.py", timeout=TIMEOUT,
        harness=harness_for_profile(profile),
    )
    out = verifier.run(run)
    f = out.findings[0]
    assert f.status == "confirmed_gap", (f.status, f.observed)
