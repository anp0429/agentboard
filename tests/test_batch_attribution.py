"""Batch attribution must be spoof-proof.

The batched gate marks each proposal's title with a predictable token
(___ab<i>___) and maps results back by substring match on the executed
title. The marks are guessable, so a proposal could carry another finding's
mark in its OWN title and hijack that finding's verdict — e.g. a failing
test titled "... ___ab0___ ..." minting a confirmed_gap for finding 0. The
gate strips anything mark-shaped from proposal code before adding its own
marks, so the only mark in any executed title is gate-injected. These tests
pin that with a simulated vitest run (no node, no sandbox install)."""

from __future__ import annotations

import json
import os
import subprocess

from agentboard.review import ReviewFinding, ReviewRun
from agentboard.verifiers.finding_verifier import FindingVerifier
from agentboard.verifiers.vitest_verifier import RepoProfile

_ASSERT_FAIL = "AssertionError: expected 1 to be 2 // Object.is equality"


def _verifier(tmp_path, log_lines):
    repo = str(tmp_path / "repo")
    os.makedirs(os.path.join(repo, "tests"))
    with open(os.path.join(repo, "tests", "suite.test.ts"), "w") as fh:
        fh.write("test('existing', () => {});\n")
    profile = RepoProfile(
        name="fixture", install_cmd=["true"], test_base=["true"],
        build_cmd=None, env={}, smoke_cmd=None,
    )
    v = FindingVerifier(repo, profile, "tests/suite.test.ts",
                        log=log_lines.append)

    def fake_run(args, cwd):
        """Simulated vitest: read the injected tests file, report every test
        whose title carries a gate mark — failing when the title says so."""
        if args == ["true"]:  # install
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        with open(os.path.join(cwd, "tests", "suite.test.ts")) as fh:
            content = fh.read()
        import re
        results = []
        for m in re.finditer(r"(?:test|it)\('([^']*)'", content):
            title = m.group(1)
            if "___ab" not in title:
                continue  # -t ___ab filter: unmarked tests never run
            failed = "SHOULD-FAIL" in title
            results.append({
                "title": title, "fullName": title,
                "status": "failed" if failed else "passed",
                "failureMessages": [_ASSERT_FAIL] if failed else [],
            })
        out = next(a for a in args if a.startswith("--outputFile=")
                   ).split("=", 1)[1]
        with open(out, "w") as fh:
            json.dump({"testResults": [{"assertionResults": results}]}, fh)
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

    v._run = fake_run
    return v


def test_spoofed_mark_cannot_hijack_another_finding(tmp_path):
    """The regression: finding 1's title contains ___ab0___ and its test
    fails. Finding 0's own test passes. Without sanitization the substring
    match attributes finding 1's failure to finding 0 too, minting a gap
    for a behavior whose test passed."""
    lines: list[str] = []
    v = _verifier(tmp_path, lines)
    run = ReviewRun(
        intent="i", target="t",
        findings=[
            ReviewFinding(behavior="honest",
                          test_code="test('honest case', () => {});"),
            ReviewFinding(behavior="spoofer",
                          test_code="test('spoof ___ab0___ SHOULD-FAIL', "
                                    "() => {});"),
        ],
    )
    try:
        leftover = v._classify_batch(run.findings)
        assert leftover == set()
        assert run.findings[0].status == "handled"
        assert run.findings[1].status == "confirmed_gap"
    finally:
        v.close()


def test_clean_titles_attribute_normally(tmp_path):
    """Control: with no mark-shaped junk in any title, verdicts land on
    their own findings exactly as before."""
    lines: list[str] = []
    v = _verifier(tmp_path, lines)
    run = ReviewRun(
        intent="i", target="t",
        findings=[
            ReviewFinding(behavior="passes",
                          test_code="test('passes', () => {});"),
            ReviewFinding(behavior="fails",
                          test_code="test('SHOULD-FAIL here', () => {});"),
        ],
    )
    try:
        leftover = v._classify_batch(run.findings)
        assert leftover == set()
        assert run.findings[0].status == "handled"
        assert run.findings[1].status == "confirmed_gap"
    finally:
        v.close()
