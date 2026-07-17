"""End-to-end gate test — the classifier meets a real test runner.

tests/test_determinism.py proves the JUDGMENT path is pure against frozen
runner output. This file closes the remaining gap: the REAL FindingVerifier,
against a REAL vitest project (tests/fixtures/demo_target), producing all
four verdict classes from actual process execution:

    handled        <- a proposed test the tool already satisfies
    confirmed_gap  <- a proposed edge test exposing the planted clamp bug
    broken_test    <- a proposed test that references an undefined name
    timed_out      <- a proposed test that never resolves (1s vitest limit)

Then the whole run repeats from scratch (fresh warm base, fresh install)
and the verdict fingerprint must be byte-identical. That is the product
claim, executed: same suite, same target, same verdicts, any run.

Requires node + npm on PATH and network for one `npm install` per run;
skipped cleanly where the toolchain is absent.
"""

from __future__ import annotations

import shutil

import pytest

from agentboard.fingerprint import verdict_fingerprint
from agentboard.review import ReviewFinding, ReviewRun
from agentboard.verifiers.finding_verifier import FindingVerifier
from agentboard.verifiers.vitest_verifier import RepoProfile

from agentboard.demo import TARGET_DIR as FIXTURE  # packaged demo target

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or shutil.which("npm") is None,
    reason="node/npm not on PATH",
)


def _profile() -> RepoProfile:
    return RepoProfile(
        name="demo_target",
        # plain install (fixture ships no lockfile; `npm ci` requires one)
        install_cmd=["npm", "install", "--no-audit", "--no-fund"],
        test_base=["npx", "vitest", "run"],
        build_cmd=None,
        env={"CI": "true"},
        smoke_cmd=["npx", "vitest", "run", "--passWithNoTests",
                   "-t", "___agentboard_env_probe___"],
    )


def _findings() -> list[ReviewFinding]:
    return [
        ReviewFinding(
            behavior="in-range page sizes are honored",
            test_code=(
                "test('honors an in-range page size', () => {\n"
                "  expect(findOrders(ORDERS, 'open', 2).length).toBe(2);\n"
                "});"
            ),
        ),
        ReviewFinding(
            behavior="a request for exactly the maximum page size is honored",
            test_code=(
                "test('clamp keeps the inclusive upper bound', async () => {\n"
                "  const { clampPageSize } = await import('./order_tool.js');\n"
                "  expect(clampPageSize(50, 1, 50)).toBe(50);\n"
                "});"
            ),
        ),
        ReviewFinding(
            behavior="a defective proposal cannot manufacture a gap",
            test_code=(
                "test('references a name that does not exist', () => {\n"
                "  expect(totallyUndefinedHelper()).toBe(true);\n"
                "});"
            ),
        ),
        ReviewFinding(
            behavior="a hang is reported as ambiguity, not as anything else",
            test_code=(
                "test('never resolves', async () => {\n"
                "  await new Promise(() => {});\n"
                "}, 1000);"
            ),
        ),
    ]


def _run_gate(batch: bool = True) -> ReviewRun:
    run = ReviewRun(intent="demo", target="order_tool.js", findings=_findings())
    verifier = FindingVerifier(
        FIXTURE, _profile(), tests_file="demo.test.js", timeout=300
    )
    return verifier.run(run, batch=batch)


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
    assert "timed out" in hang.observed.lower()

    # the whole thing again: fresh warm base, fresh install, same verdicts
    second = _run_gate()
    assert verdict_fingerprint(first) == verdict_fingerprint(second)


def test_batched_and_serial_gates_are_verdict_identical():
    """The speed path may only ever be faster — never different. This is the
    contract every future optimization signs: same suite, same target, same
    fingerprint, batched or not."""
    batched = _run_gate(batch=True)
    serial = _run_gate(batch=False)
    assert verdict_fingerprint(batched) == verdict_fingerprint(serial)


def test_fixture_baseline_is_green():
    """The planted bug must NOT break the shipped suite — the demo story is
    'existing tests pass, the gate still finds the edge.'"""
    run = ReviewRun(
        intent="baseline", target="order_tool.js",
        findings=[ReviewFinding(
            behavior="existing behavior holds",
            test_code=(
                "test('existing filter still works', () => {\n"
                "  expect(findOrders(ORDERS, 'closed', 5).map(o => o.id))"
                ".toEqual([3]);\n"
                "});"
            ),
        )],
    )
    verifier = FindingVerifier(
        FIXTURE, _profile(), tests_file="demo.test.js", timeout=300
    )
    out = verifier.run(run)
    assert out.findings[0].status == "handled", out.findings[0].observed


def test_falsifier_can_never_be_handled():
    """The zod invariant: an injected expect(1).toBe(2) must NEVER classify
    as handled. On zod, describe-less injection + a typecheck project
    laundered exactly this into a false green. The gate's word is only worth
    anything if an impossible test provably executes and fails."""
    run = ReviewRun(
        intent="falsifier", target="order_tool.js",
        findings=[ReviewFinding(
            behavior="an impossible assertion must fail at runtime",
            test_code="test('falsifier', () => {\n  expect(1).toBe(2);\n});",
        )],
    )
    verifier = FindingVerifier(
        FIXTURE, _profile(), tests_file="demo.test.js", timeout=300
    )
    out = verifier.run(run)
    f = out.findings[0]
    assert f.status == "confirmed_gap", (f.status, f.observed)
