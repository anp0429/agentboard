"""The GapAuditor is wired into the review path as the advisory precision
layer: a second model reads the source plus each confirmed gap's failing test
and flags likely false positives (wrong assertions). Two invariants these
tests pin: the gate's verdict NEVER changes, and the annotations flow all the
way out — into the json artifact and the PR comment — because triage the
human can't see is triage that didn't happen.
"""

import json
import subprocess
import sys
import types

from agentboard.agents.gap_auditor import GapAuditor
from agentboard.review import ReviewFinding, ReviewRun


def _fake_client(payload, calls):
    """OpenAI-shaped stub: records each call, returns the given JSON."""

    def create(**kwargs):
        calls.append(kwargs)
        msg = types.SimpleNamespace(content=json.dumps(payload))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    return types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=create)
        )
    )


def test_audit_annotates_but_never_changes_the_verdict():
    calls = []
    auditor = GapAuditor(
        model="gpt-5.5",
        client=_fake_client(
            {"assessment": "likely_false_positive",
             "reason": "the code never promised key insertion",
             "evidence": "invalid_element recurses with [...path, ...issue.path]"},
            calls,
        ),
    )
    f = ReviewFinding(behavior="x", status="confirmed_gap",
                      test_code="test('t', () => {})", observed="AssertionError: nope")
    auditor.audit("const source = 1;", f)
    assert f.status == "confirmed_gap"          # the gate's word stands
    assert f.audit == "likely_false_positive"
    assert f.audit_reason
    assert f.audit_evidence
    assert len(calls) == 1


def test_audit_only_touches_confirmed_gaps():
    calls = []
    auditor = GapAuditor(model="gpt-5.5",
                         client=_fake_client({"assessment": "likely_real"}, calls))
    findings = [
        ReviewFinding(behavior="a", status="handled"),
        ReviewFinding(behavior="b", status="broken_test"),
        ReviewFinding(behavior="c", status="skipped_covered"),
    ]
    auditor.audit_all("src", findings)
    assert calls == []                          # no gap, no model call, no cost
    assert all(f.audit == "" for f in findings)


def test_audit_failure_is_loud_not_silent():
    def boom(**kwargs):
        raise RuntimeError("provider down")

    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=boom))
    )
    auditor = GapAuditor(model="gpt-5.5", client=client)
    f = ReviewFinding(behavior="x", status="confirmed_gap", test_code="t")
    auditor.audit("src", f)
    assert f.status == "confirmed_gap"
    assert f.audit == "not_audited"
    assert "UNVERIFIED" in f.audit_reason


def test_json_out_carries_audit_fields(tmp_path):
    from agentboard.cli import _write_json_out

    run = ReviewRun(intent="i", target="t")
    gap = ReviewFinding(behavior="g", status="confirmed_gap",
                        observed="AssertionError: x", test_code="test('x')")
    gap.audit = "likely_false_positive"
    gap.audit_reason = "wrong assumption"
    gap.audit_evidence = "lines 300-302"
    run.findings = [gap, ReviewFinding(behavior="h", status="handled")]

    out = tmp_path / "run.json"
    _write_json_out(str(out), run, repo="r", base="b", head="h",
                    pairs=[("t", "t.test.ts")], board="board.html")
    doc = json.loads(out.read_text())
    audited, unaudited = doc["findings"]
    assert audited["audit"] == "likely_false_positive"
    assert audited["audit_reason"] == "wrong assumption"
    assert audited["audit_evidence"] == "lines 300-302"
    assert unaudited["audit"] is None           # nullable, additive: schema v1


def test_pr_comment_renders_the_audit_line(tmp_path):
    doc = {
        "schema_version": 1,
        "summary": "verdicts: confirmed_gap=1",
        "env_error": "",
        "findings": [{
            "behavior": "the gap",
            "status": "confirmed_gap",
            "observed": "AssertionError: expected 1 to be 2",
            "test_code": "test('x', () => {})",
            "audit": "likely_false_positive",
            "audit_reason": "asserted a rule the code never promised",
        }],
    }
    p = tmp_path / "run.json"
    p.write_text(json.dumps(doc))
    out = subprocess.run(
        [sys.executable, "scripts/render_pr_comment.py", str(p)],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0
    assert "Auditor (advisory)" in out.stdout
    assert "likely false positive" in out.stdout
    assert "never promised" in out.stdout


def test_fp_without_evidence_is_downgraded():
    # A dismissal that cites no source line has not earned the FP call.
    # This guards the r2-inversion class: a real strict catch was flagged FP.
    import json as _json
    import types
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        payload = {"assessment": "likely_false_positive",
                   "reason": "the agent assumed a different rule", "evidence": ""}
        msg = types.SimpleNamespace(content=_json.dumps(payload))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])
    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    from agentboard.agents.gap_auditor import GapAuditor
    from agentboard.review import ReviewFinding
    a = GapAuditor(model="gpt-5.5", client=client)
    f = ReviewFinding(behavior="x", status="confirmed_gap",
                      test_code="test('t',()=>{})", observed="AssertionError: expected /foobar to be /foo/foobar")
    a.audit("const _base = 1;", f)
    assert f.audit == "uncertain"                     # NOT false positive
    assert "downgraded" in f.audit_reason


def test_empty_reason_triggers_one_retry():
    import json as _json
    import types
    from agentboard.agents.gap_auditor import GapAuditor
    from agentboard.review import ReviewFinding
    seq = [
        {"assessment": "uncertain", "reason": "", "evidence": ""},
        {"assessment": "likely_real", "reason": "drops the field", "evidence": "line 12 skips it"},
    ]
    box = {"i": 0}
    def create(**kwargs):
        p = seq[min(box["i"], len(seq) - 1)]
        box["i"] += 1
        msg = types.SimpleNamespace(content=_json.dumps(p))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])
    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    a = GapAuditor(model="gpt-5.5", client=client)
    f = ReviewFinding(behavior="x", status="confirmed_gap", test_code="t", observed="AssertionError: x")
    a.audit("src", f)
    assert box["i"] == 2                              # retried once
    assert f.audit == "likely_real"
    assert f.audit_reason == "drops the field"
