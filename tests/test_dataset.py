"""The dataset collector turns each finding into a labeled training row:
the proposal the model made, and the executed verdict as the label. These
pin the two rules that keep the corpus honest: `ran` is a model-free fact
derived only from the executed status, and the advisory audit is stored but
never allowed to overwrite that fact.
"""

import json

from agentboard.dataset import (
    _ran,
    append_run,
    backfill_from_json_out,
    finding_row,
)
from agentboard.review import ReviewFinding, ReviewRun


def test_ran_is_the_execution_fact():
    assert _ran("handled") is True          # ran + passed
    assert _ran("confirmed_gap") is True    # ran + failed assertion
    assert _ran("broken_test") is False     # did not run
    assert _ran("skipped_covered") is None  # no test generated
    assert _ran("timed_out") is None        # started, never finished
    assert _ran("pending") is None


def test_row_carries_proposal_and_label():
    f = ReviewFinding(
        behavior="handles empty input", status="confirmed_gap",
        test_code="test('x', () => {})", observed="AssertionError: nope",
        source_file="src/a.ts",
    )
    f.audit = "likely_real"
    f.audit_reason = "drops the value"
    row = finding_row(
        f, repo="/r", base="main", head="HEAD", target="src/a.ts",
        tests="src/a.test.ts", intent="parse input", axis="default",
        reviewer_model="gpt-5.5", critic_model="gpt-5.5", ts="2026-07-19T00:00:00Z",
    )
    assert row["proposed_test"] == "test('x', () => {})"   # the model output
    assert row["status"] == "confirmed_gap"                # the gate label
    assert row["ran"] is True
    assert row["audit"] == "likely_real"                   # advisory, stored
    assert row["intent"] == "parse input"


def test_audit_never_overwrites_the_ran_label():
    # A false-positive audit must NOT flip `ran`: the test still executed.
    f = ReviewFinding(behavior="b", status="confirmed_gap",
                      test_code="t", observed="AssertionError: x")
    f.audit = "likely_false_positive"
    row = finding_row(
        f, repo="r", base="b", head="h", target="t", tests="tt",
        intent="i", axis="default", reviewer_model="m", critic_model="m",
        ts="2026-07-19T00:00:00Z",
    )
    assert row["ran"] is True                 # executed fact stands
    assert row["audit"] == "likely_false_positive"   # opinion recorded alongside


def test_append_writes_one_row_per_finding(tmp_path):
    run = ReviewRun(intent="i", target="src/a.ts")
    run.findings = [
        ReviewFinding(behavior="one", status="handled", source_file="src/a.ts"),
        ReviewFinding(behavior="two", status="confirmed_gap",
                      test_code="t", source_file="src/a.ts"),
        ReviewFinding(behavior="three", status="skipped_covered",
                      source_file="src/a.ts"),
    ]
    path = tmp_path / "corpus.jsonl"
    n = append_run(
        run, path=str(path), repo="/r", base="main", head="HEAD",
        pairs=[("src/a.ts", "src/a.test.ts")], intent="i", axis="default",
        reviewer_model="gpt-5.5", critic_model="gpt-5.5",
    )
    assert n == 3
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 3
    rows = [json.loads(x) for x in lines]
    assert [r["ran"] for r in rows] == [True, True, None]


def test_append_is_additive(tmp_path):
    run = ReviewRun(intent="i", target="t")
    run.findings = [ReviewFinding(behavior="b", status="handled", source_file="t")]
    path = tmp_path / "corpus.jsonl"
    for _ in range(3):
        append_run(run, path=str(path), repo="r", base="b", head="h",
                   pairs=[("t", "tt")], intent="i", axis="default",
                   reviewer_model="m", critic_model="m")
    assert len(path.read_text().strip().splitlines()) == 3   # appended, not clobbered


def test_backfill_from_json_out(tmp_path):
    doc = {
        "schema_version": 1, "repo": "/r", "base": "main", "head": "HEAD",
        "intent": "parse input",
        "targets": [{"target": "src/a.ts", "tests": "src/a.test.ts"}],
        "findings": [
            {"behavior": "one", "status": "confirmed_gap", "observed": "x",
             "source_file": "src/a.ts", "test_code": "t",
             "audit": "likely_real", "audit_reason": "r", "audit_evidence": "e"},
            {"behavior": "two", "status": "broken_test", "observed": "y",
             "source_file": "src/a.ts", "test_code": "t2",
             "audit": None, "audit_reason": None, "audit_evidence": None},
        ],
    }
    jf = tmp_path / "run.json"
    jf.write_text(json.dumps(doc))
    out = tmp_path / "corpus.jsonl"
    n = backfill_from_json_out(str(jf), str(out))
    assert n == 2
    rows = [json.loads(x) for x in out.read_text().strip().splitlines()]
    assert rows[0]["intent"] == "parse input"
    assert rows[0]["ran"] is True
    assert rows[1]["ran"] is False
    assert rows[0]["reviewer_model"] == "unknown"   # not in the artifact, not guessed
