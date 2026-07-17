"""The determinism harness.

agentboard's product claim is that no model — and nothing else
nondeterministic — sits in the accept/reject path. This file is where that
claim stops being prose:

  1. The CLASSIFIER is a pure function of the test-runner's JSON output.
     We run it 1000x over fixed fixtures covering every verdict class and
     assert one unique result per fixture. Milliseconds in CI.

  2. The FINGERPRINT is a pure, order-insensitive function of the verdicts.
     Shuffling finding order must not change it; changing one status must.

Anything that would make a verdict depend on time, ordering, machine, or
luck has to get past this file first. Every future optimization (parallel
gate, caching, batching) must leave it green.
"""

from __future__ import annotations

import json
import os
import random
import tempfile

from agentboard.fingerprint import verdict_fingerprint, verdict_summary
from agentboard.review import ReviewFinding, ReviewRun
from agentboard.verifiers.finding_verifier import FindingVerifier

N = 1000


# ---------------------------------------------------------------------------
# fixtures: one frozen vitest-JSON payload per verdict class
# ---------------------------------------------------------------------------

def _vitest_json(assertions, suite_status="passed", suite_message=""):
    return {
        "testResults": [
            {
                "status": suite_status,
                "message": suite_message,
                "assertionResults": assertions,
            }
        ]
    }


FIXTURES = {
    "handled": _vitest_json(
        [{"status": "passed", "failureMessages": []}]
    ),
    "confirmed_gap": _vitest_json(
        [{
            "status": "failed",
            "failureMessages": [
                "AssertionError: expected [ 'a' ] to deeply equal [ 'a', 'b' ]\n"
                "    at /some/machine/path/server.test.ts:3294:19"
            ],
        }]
    ),
    "timed_out": _vitest_json(
        [{
            "status": "failed",
            "failureMessages": [
                "Error: Test timed out in 30000ms.\n"
                "    at runWithTimeout (/tmp/x/node_modules/vitest/dist/run.js:1:1)"
            ],
        }]
    ),
    "broken_test": _vitest_json(
        [{
            "status": "failed",
            "failureMessages": [
                "ReferenceError: target_schema is not defined\n"
                "    at /some/machine/path/tools.ts:247:11"
            ],
        }]
    ),
    # suite failed to collect at all -> broken test
    "broken_test_collect": _vitest_json(
        [], suite_status="failed",
        suite_message="Failed to resolve entry for package '@x/y'",
    ),
}

EXPECTED = {
    "handled": "handled",
    "confirmed_gap": "confirmed_gap",
    "timed_out": "timed_out",
    "broken_test": "broken_test",
    "broken_test_collect": "broken_test",
}


def _classify_fixture(payload) -> tuple[str, str]:
    """Write the payload to a temp file and run the real _read on it."""
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "result.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return FindingVerifier._read(out)


# ---------------------------------------------------------------------------
# 1. classifier purity: 1000 runs per verdict class, one unique outcome each
# ---------------------------------------------------------------------------

def test_classifier_is_deterministic_1000x():
    for name, payload in FIXTURES.items():
        outcomes = {_classify_fixture(payload) for _ in range(N)}
        assert len(outcomes) == 1, (
            f"fixture {name!r}: classifier produced {len(outcomes)} distinct "
            f"outcomes over {N} runs — the gate is nondeterministic"
        )
        (status, _observed) = outcomes.pop()
        assert status == EXPECTED[name], (
            f"fixture {name!r}: classified as {status!r}, "
            f"expected {EXPECTED[name]!r}"
        )


def test_missing_output_is_broken_test_not_a_crash():
    status, observed = FindingVerifier._read("/nonexistent/never/result.json")
    assert status == "broken_test"
    assert "no JSON output" in observed


def test_gap_outranks_timeout_outranks_load_error():
    """Priority when one run reports several failures: a real assertion
    failure is the strongest evidence, then ambiguity, then test breakage."""
    both = _vitest_json([
        {"status": "failed",
         "failureMessages": ["Error: Test timed out in 30000ms."]},
        {"status": "failed",
         "failureMessages": ["AssertionError: expected 1 to equal 2"]},
    ])
    status, _ = _classify_fixture(both)
    assert status == "confirmed_gap"

    timeout_and_load = _vitest_json([
        {"status": "failed",
         "failureMessages": ["ReferenceError: x is not defined"]},
        {"status": "failed",
         "failureMessages": ["Error: Test timed out in 30000ms."]},
    ])
    status, _ = _classify_fixture(timeout_and_load)
    assert status == "timed_out"


# ---------------------------------------------------------------------------
# 2. fingerprint: order-insensitive, verdict-sensitive, 1000x stable
# ---------------------------------------------------------------------------

def _sample_run() -> ReviewRun:
    behaviors = [
        ("composite FK grouped as one constraint", "correctness", "handled"),
        ("arrays ordered by constraint ordinality", "correctness", "handled"),
        ("repeated calls return identical output", "consistency", "handled"),
        ("self-referential FK reported once", "correctness", "timed_out"),
        ("no-FK table fabricates nothing", "correctness", "confirmed_gap"),
    ]
    run = ReviewRun(intent="i", target="t")
    for b, axis, status in behaviors:
        run.findings.append(ReviewFinding(behavior=b, axis=axis, status=status))
    return run


def test_fingerprint_is_stable_1000x():
    fps = {verdict_fingerprint(_sample_run()) for _ in range(N)}
    assert len(fps) == 1


def test_fingerprint_ignores_finding_order():
    a = _sample_run()
    b = _sample_run()
    rng = random.Random(42)
    for _ in range(50):
        rng.shuffle(b.findings)
        assert verdict_fingerprint(a) == verdict_fingerprint(b)


def test_fingerprint_ignores_nonverdict_noise():
    a = _sample_run()
    b = _sample_run()
    # machine-specific evidence and advisory-LLM output must not matter
    b.findings[0].observed = "passed in 31337ms on /Users/somebody/tmp/xyz"
    b.findings[1].audit = "likely_real"
    b.findings[2].test_code = "test('phrased differently', () => {})"
    assert verdict_fingerprint(a) == verdict_fingerprint(b)


def test_fingerprint_changes_when_a_verdict_changes():
    a = _sample_run()
    b = _sample_run()
    b.findings[0].status = "confirmed_gap"
    assert verdict_fingerprint(a) != verdict_fingerprint(b)


def test_summary_line_mentions_fingerprint():
    line = verdict_summary(_sample_run())
    assert "fingerprint:" in line and "handled=3" in line
