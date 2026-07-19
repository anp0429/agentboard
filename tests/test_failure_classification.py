"""The gate's failure classifier must never let a non-assertion mint a gap.

A confirmed_gap is the product; its worth rests entirely on "a real test ran
and its assertion failed." The old heuristic classified any failure message
containing the substring "expected" as an assertion failure — and
"Unexpected token" (a parse error) contains "expected", so a garbage test
could be scored as a confirmed gap. These tests pin the conservative rule:
only a named AssertionError is an assertion; everything else that isn't a
timeout is the test's problem, not the tool's.
"""

from agentboard.verifiers.finding_verifier import FindingVerifier


def _kind(message: str) -> str:
    return FindingVerifier._classify_failure(message)[0]


def test_assertion_error_is_an_assertion():
    assert _kind("AssertionError: expected 1 to be 2 // Object.is equality") == "assertion"


def test_node_assert_flavor_is_an_assertion():
    assert _kind("AssertionError [ERR_ASSERTION]: values are not deep-equal") == "assertion"


def test_unexpected_token_is_not_an_assertion():
    # The regression this file exists for: "Unexpected token" contains the
    # substring "expected". A parse error must classify as load_error
    # (-> broken_test), never as a confirmed gap.
    msg = "SyntaxError: Unexpected token 'o', \"not json\" is not valid JSON"
    assert _kind(msg) == "load_error"


def test_runtime_error_saying_expected_is_not_an_assertion():
    # Library code often throws messages like this; a throw is not a failed
    # assertion, and the conservative gate refuses to upgrade it to a gap.
    assert _kind("TypeError: expected string, got undefined") == "load_error"


def test_plain_crash_is_not_an_assertion():
    assert _kind("TypeError: Cannot read properties of undefined (reading 'push')") == "load_error"


def test_timeout_wins_over_everything():
    assert _kind("Error: Test timed out in 5000ms") == "timeout"


def test_empty_message_is_load_error():
    assert _kind("") == "load_error"


def test_first_line_only_and_bounded():
    kind, first = FindingVerifier._classify_failure(
        "AssertionError: expected a to equal b\n    at stack frame one\n    at two"
    )
    assert kind == "assertion"
    assert first == "AssertionError: expected a to equal b"


def test_vitest3_timeout_placeholder_is_timeout():
    # vitest 3's JSON reporter loses the human timeout message: the
    # failureMessage is the internal stack-donor's stack, headed by its
    # placeholder name. That header is the timeout signal.
    msg = (
        "Error: STACK_TRACE_ERROR\n"
        "    at task (node_modules/@vitest/runner/dist/chunk-hooks.js:638:27)"
    )
    assert _kind(msg) == "timeout"


def test_vitest3_placeholder_must_head_the_message():
    # The placeholder classifies as timeout only as the FIRST line. A test
    # that merely mentions the string deeper in a stack keeps its own head.
    msg = (
        "AssertionError: expected 1 to be 2\n"
        "    at Error: STACK_TRACE_ERROR somewhere deep"
    )
    assert _kind(msg) == "assertion"
