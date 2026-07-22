"""Gaps that fail in unison are one cause, not N bugs.

Found on supabase/mcp#324: nine "confirmed gaps" in one run all read
"Target cannot be null or undefined." — every generated test unwrapped the
tool response with the wrong shape and hit the same null. An operator who
trusts a 9-gap report files nine bogus findings and torches their
credibility. The deterministic pass these tests pin: verbatim-identical
failure messages across >= 3 confirmed gaps get an artifact_note and a
run-level warning; statuses never change (the tests really did execute and
fail — the gate does not un-say that), and distinct messages are never
merged.
"""

from agentboard.review import ReviewFinding, flag_systematic_artifacts


def _gap(msg: str) -> ReviewFinding:
    return ReviewFinding(behavior="b", status="confirmed_gap", observed=msg)


def test_three_identical_failures_are_flagged():
    fs = [_gap("Target cannot be null or undefined.") for _ in range(3)]
    warnings = flag_systematic_artifacts(fs)
    assert len(warnings) == 1
    assert "3 confirmed gaps" in warnings[0]
    assert all(f.artifact_note for f in fs)
    assert all(f.status == "confirmed_gap" for f in fs)  # verdicts untouched


def test_two_identical_failures_are_not_flagged():
    fs = [_gap("Target cannot be null or undefined.") for _ in range(2)]
    assert flag_systematic_artifacts(fs) == []
    assert all(not f.artifact_note for f in fs)


def test_distinct_messages_are_never_merged():
    fs = [_gap("expected 0 to be greater than 0"),
          _gap("expected 0 to be greater than 1"),
          _gap("expected 2 to be 1")]
    assert flag_systematic_artifacts(fs) == []


def test_grouping_uses_first_line_only():
    # Same assertion, different stack tails — still one cause.
    fs = [_gap("Target cannot be null or undefined.\n  at server.test.ts:10"),
          _gap("Target cannot be null or undefined.\n  at server.test.ts:99"),
          _gap("Target cannot be null or undefined.\n  at server.test.ts:42")]
    assert len(flag_systematic_artifacts(fs)) == 1


def test_non_gap_statuses_are_ignored():
    # broken_test failing in unison is NORMAL (one env error benches many);
    # the heuristic must not fire on it.
    fs = [ReviewFinding(behavior="b", status="broken_test",
                        observed="environment failure") for _ in range(5)]
    assert flag_systematic_artifacts(fs) == []


def test_mixed_run_flags_only_the_unison_group():
    unison = [_gap("Target cannot be null or undefined.") for _ in range(4)]
    real = _gap("expected [] to deeply equal [{id: 3}]")
    warnings = flag_systematic_artifacts(unison + [real])
    assert len(warnings) == 1
    assert not real.artifact_note
    assert all(f.artifact_note for f in unison)
