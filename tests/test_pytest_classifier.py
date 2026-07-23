"""classify_failure regressions, every scenario taken from the gate's own
self-review (run 94669614e770c539), which broke the substring version of
this function two ways in one evening. The invariant under test: only the
RAISED exception classifies. A report that mentions an exception is not a
report of that exception, and an unrecognizable report is always
load_error, never a fabricated assertion gap."""

from agentboard.verifiers.pytest_harness import PytestHarness


H = PytestHarness()


def test_nameerror_mentioning_assertionerror_is_not_an_assertion():
    fm = (
        "test setup failed\n"
        "    def test_x():\n"
        ">       check(AssertionError)\n"
        "NameError: name 'check' is not defined\n"
    )
    kind, _ = H.classify_failure(fm)
    assert kind == "load_error"


def test_assertion_text_mentioning_timeout_is_still_an_assertion():
    fm = (
        "assert failed\n"
        ">       assert msg == 'timed out cleanly'\n"
        "E       AssertionError: assert 'timeout' == 'assertion'\n"
    )
    kind, _ = H.classify_failure(fm)
    assert kind == "assertion"


def test_rewritten_assert_is_an_assertion():
    fm = (
        ">       assert clamp(50) == 50\n"
        "E       AssertionError: assert 49 == 50\n"
    )
    kind, _ = H.classify_failure(fm)
    assert kind == "assertion"


def test_pytest_timeout_plugin_routes_to_ambiguity():
    fm = "Failed: Timeout >30.0s\n"
    kind, _ = H.classify_failure(fm)
    assert kind == "timeout"


def test_plain_crash_names_itself():
    fm = "ImportError: cannot import name 'nope' from 'agentboard.cli'\n"
    kind, _ = H.classify_failure(fm)
    assert kind == "load_error"


def test_chained_exceptions_classify_by_the_final_raise():
    fm = (
        "E       AssertionError: original\n"
        "\n"
        "During handling of the above exception, another exception occurred:\n"
        "\n"
        "TypeError: unsupported operand\n"
    )
    kind, _ = H.classify_failure(fm)
    assert kind == "load_error"


def test_prose_or_quoted_source_never_qualifies():
    fm = (
        "the docs say AssertionError is raised when validation fails\n"
        "    assert isinstance(err, AssertionError)\n"
        "RuntimeError: boom\n"
    )
    kind, _ = H.classify_failure(fm)
    assert kind == "load_error"


def test_empty_report_is_load_error_not_a_gap():
    kind, first = H.classify_failure("")
    assert kind == "load_error"
    assert first == ""


def test_assertion_whose_message_leads_with_timeout_wording_stays_assertion():
    # run ff36b2226dfda3eb: junit message attribute IS the assertion text,
    # so "timed out" can be the report's first line; the raised
    # AssertionError still decides.
    fm = (
        "assert result == 'operation timed out cleanly'\n"
        ">       assert result == 'operation timed out cleanly'\n"
        "E       AssertionError: assert 'boom' == 'operation timed out cleanly'\n"
        "\n"
        "test_s.py:9: AssertionError"
    )
    kind, _ = H.classify_failure(fm)
    assert kind == "assertion"


def test_runner_generated_timeout_with_no_traceback_still_routes_to_timeout():
    assert H.classify_failure("Error: Test timed out in 5000ms")[0] == "timeout"
    assert H.classify_failure("Failed: Timeout >5.0s")[0] == "timeout"
