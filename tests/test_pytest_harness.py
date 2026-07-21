"""The pytest harness: same gate semantics, Python spelling.

Everything here is pure or filesystem-only — no subprocess, no sandbox —
mirroring how test_injection_placement.py and test_failure_classification.py
pin the vitest harness. The classification tests are the load-bearing ones:
a confirmed_gap may only ever come from a report that names AssertionError,
and pytest spells crashes and assertion failures differently than vitest
(the junit longrepr's trailing traceback line carries the exception name).
"""

from __future__ import annotations

import os

from agentboard.verifiers.harness import (
    VitestHarness,
    harness_for_profile,
    harness_for_target,
)
from agentboard.verifiers.pytest_harness import PytestHarness
from agentboard.verifiers.vitest_verifier import RepoProfile

H = PytestHarness()

HOST = (
    "from order_tool import find_orders\n"
    "\n"
    "ORDERS = [{'id': 1, 'status': 'open'}]\n"
    "\n"
    "def test_filters_by_status():\n"
    "    assert find_orders(ORDERS, 'open', 10) == ORDERS\n"
)


# ---------------------------------------------------------------------------
# injection: EOF-append at module level, imports deduped not stripped
# ---------------------------------------------------------------------------

def test_inject_appends_at_module_level():
    out, err = H.inject(HOST, "def test_new():\n    assert True\n")
    assert err == ""
    assert out.rstrip().endswith("def test_new():\n    assert True")
    # host content is untouched ahead of the injection point
    assert out.startswith(HOST.rstrip())


def test_inject_keeps_imports_the_host_lacks():
    # Unlike ES modules, a Python import is legal at the injection point —
    # and a proposal may genuinely need one (clamp_page_size is not
    # imported by the host file).
    proposal = (
        "from order_tool import clamp_page_size\n"
        "def test_clamp():\n"
        "    assert clamp_page_size(50, 1, 50) == 50\n"
    )
    out, err = H.inject(HOST, proposal)
    assert err == ""
    assert "from order_tool import clamp_page_size" in out


def test_inject_drops_imports_the_host_already_has():
    proposal = (
        "from order_tool import find_orders\n"
        "def test_dup():\n"
        "    assert find_orders([], 'open', 1) == []\n"
    )
    out, err = H.inject(HOST, proposal)
    assert err == ""
    assert out.count("from order_tool import find_orders") == 1


def test_inject_rejects_empty_and_imports_only_proposals():
    assert H.inject(HOST, "") == (None, "no test supplied")
    out, err = H.inject(HOST, "from order_tool import find_orders\n")
    assert out is None and err == "test contained only imports"


# ---------------------------------------------------------------------------
# naming: title extraction and gate marks
# ---------------------------------------------------------------------------

def test_title_is_the_first_test_function_name():
    assert H.test_title("def test_edge_case():\n    pass") == "test_edge_case"
    assert H.test_title("async def test_await():\n    pass") == "test_await"
    # helper defs before the test do not confuse it
    code = "def helper():\n    pass\n\ndef test_real():\n    pass"
    assert H.test_title(code) == "test_real"
    assert H.test_title("x = 1") is None


def test_mark_lands_in_the_function_name():
    marked = H.mark_title("def test_edge():\n    assert True", "___ab3___")
    assert "def test_edge___ab3___(" in marked
    assert H.mark_title("no test here", "___ab0___") is None


# ---------------------------------------------------------------------------
# commands: node id for serial, -k mark filter for batch
# ---------------------------------------------------------------------------

def _profile():
    return RepoProfile(name="p", install_cmd=[], test_base=["py", "-m", "pytest"],
                       build_cmd=None, env={}, smoke_cmd=None, kind="pytest")


def test_serial_command_selects_by_node_id():
    cmd = H.serial_command(_profile(), "tests/test_x.py", "test_edge", "/tmp/o.xml")
    assert "tests/test_x.py::test_edge" in cmd
    assert "--junit-xml=/tmp/o.xml" in cmd
    assert "-k" not in cmd  # node id, never a substring expression


def test_batch_command_filters_on_the_mark():
    cmd = H.batch_command(_profile(), "tests/test_x.py", "___ab", "/tmp/o.xml")
    assert "tests/test_x.py" in cmd
    assert cmd[cmd.index("-k") + 1] == "___ab"
    assert "--junit-xml=/tmp/o.xml" in cmd


# ---------------------------------------------------------------------------
# classification: only a named AssertionError is an assertion
# ---------------------------------------------------------------------------

# real shapes captured from pytest 9's junit output (message + longrepr)
_BARE_ASSERT = (
    "assert 1 == 2\n"
    "def test_bare_assert():\n"
    ">       assert 1 == 2\n"
    "E       assert 1 == 2\n\n"
    "test_s.py:5: AssertionError"
)
_NAMED_ASSERT = (
    "AssertionError: explicit message\n"
    "def test_named_assert():\n"
    ">       raise AssertionError('explicit message')\n"
    "E       AssertionError: explicit message\n\n"
    "test_s.py:8: AssertionError"
)
_NAME_ERROR = (
    "NameError: name 'totally_undefined' is not defined\n"
    "def test_nameerror():\n"
    ">       assert totally_undefined() == 1\n"
    "E       NameError: name 'totally_undefined' is not defined\n\n"
    "test_s.py:11: NameError"
)
_DID_NOT_RAISE = (
    "Failed: DID NOT RAISE ValueError\n"
    ">       with pytest.raises(ValueError):\n"
    "E       Failed: DID NOT RAISE ValueError\n\n"
    "test_c.py:4: Failed"
)


def _kind(fm):
    return H.classify_failure(fm)[0]


def test_bare_assert_is_an_assertion():
    # pytest's assert rewriting raises AssertionError; the longrepr's final
    # traceback line names it even when the message attribute does not.
    assert _kind(_BARE_ASSERT) == "assertion"


def test_named_assertion_error_is_an_assertion():
    assert _kind(_NAMED_ASSERT) == "assertion"


def test_nameerror_is_never_an_assertion():
    assert _kind(_NAME_ERROR) == "load_error"


def test_did_not_raise_stays_conservative():
    # pytest.raises reports pytest's own Failed, not AssertionError. Ruled
    # broken_test on purpose: a missed gap, never a minted one.
    assert _kind(_DID_NOT_RAISE) == "load_error"


def test_empty_message_is_load_error():
    assert _kind("") == "load_error"


def test_timeout_wording_routes_to_ambiguity():
    assert _kind("Error: Test timed out in 5000ms") == "timeout"
    assert _kind("Failed: Timeout >5.0s") == "timeout"


# ---------------------------------------------------------------------------
# junit parsing: verdicts from real XML shapes
# ---------------------------------------------------------------------------

def _write_xml(tmp_path, body):
    out = os.path.join(str(tmp_path), "r.xml")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="utf-8"?><testsuites>'
                 '<testsuite name="pytest">' + body + "</testsuite></testsuites>")
    return out


def test_verdict_passed_is_handled(tmp_path):
    out = _write_xml(tmp_path, '<testcase classname="t" name="test_ok"/>')
    assert H.read_verdict(out)[0] == "handled"


def test_verdict_assertion_is_confirmed_gap(tmp_path):
    out = _write_xml(
        tmp_path,
        '<testcase classname="t" name="test_gap">'
        '<failure message="assert 1 == 2">E       assert 1 == 2\n\n'
        "t.py:5: AssertionError</failure></testcase>",
    )
    status, observed = H.read_verdict(out)
    assert status == "confirmed_gap"
    assert observed.startswith("assert 1 == 2")


def test_verdict_nameerror_is_broken_test(tmp_path):
    out = _write_xml(
        tmp_path,
        '<testcase classname="t" name="test_bad">'
        "<failure message=\"NameError: name 'x' is not defined\">"
        "E       NameError\n\nt.py:5: NameError</failure></testcase>",
    )
    assert H.read_verdict(out)[0] == "broken_test"


def test_verdict_collection_error_is_broken_test(tmp_path):
    # the shape pytest emits when the injected file cannot even be imported
    out = _write_xml(
        tmp_path,
        '<testcase classname="" name="test_mod">'
        '<error message="collection failure">SyntaxError: invalid syntax'
        "</error></testcase>",
    )
    status, observed = H.read_verdict(out)
    assert status == "broken_test"
    assert "collection failure" in observed


def test_verdict_empty_run_is_name_match_failure(tmp_path):
    # bad node id / -k matching nothing: XML written, zero testcases
    out = _write_xml(tmp_path, "")
    status, observed = H.read_verdict(out)
    assert status == "broken_test"
    assert "did not run" in observed


def test_verdict_missing_file_is_no_output():
    status, observed = H.read_verdict("/nonexistent/never/r.xml")
    assert status == "broken_test"
    assert "no junit XML output" in observed


def test_gap_outranks_load_error_in_one_run(tmp_path):
    out = _write_xml(
        tmp_path,
        '<testcase classname="t" name="test_a">'
        '<failure message="NameError: nope">t.py:1: NameError</failure>'
        "</testcase>"
        '<testcase classname="t" name="test_b">'
        '<failure message="assert 1 == 2">t.py:2: AssertionError</failure>'
        "</testcase>",
    )
    assert H.read_verdict(out)[0] == "confirmed_gap"


def test_batch_records_carry_marks_and_statuses(tmp_path):
    out = _write_xml(
        tmp_path,
        '<testcase classname="t" name="test_a___ab0___"/>'
        '<testcase classname="t" name="test_b___ab1___">'
        '<failure message="assert 1 == 2">t.py:2: AssertionError</failure>'
        "</testcase>"
        '<testcase classname="t" name="test_c___ab2___">'
        '<error message="setup broke">boom</error></testcase>',
    )
    records = {r.title: r for r in H.read_batch(out)}
    assert records["test_a___ab0___"].status == "passed"
    assert records["test_b___ab1___"].status == "failed"
    # an errored test did not properly run: the verifier must leave it for
    # the serial path, so its status is neither passed nor failed
    assert records["test_c___ab2___"].status == "error"


def test_batch_missing_or_bad_output_attributes_nothing(tmp_path):
    assert H.read_batch("/nonexistent/never/r.xml") is None
    bad = os.path.join(str(tmp_path), "bad.xml")
    open(bad, "w", encoding="utf-8").write("not xml at all <<<")
    assert H.read_batch(bad) is None


# ---------------------------------------------------------------------------
# discovery: .py test-file conventions
# ---------------------------------------------------------------------------

def _touch(root, rel):
    full = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    open(full, "w", encoding="utf-8").write("")


def test_discovery_colocated_test_prefix(tmp_path):
    _touch(tmp_path, "pkg/order_tool.py")
    _touch(tmp_path, "pkg/test_order_tool.py")
    got = PytestHarness.default_tests_for(str(tmp_path), "pkg/order_tool.py")
    assert got == os.path.join("pkg", "test_order_tool.py")


def test_discovery_suffix_convention(tmp_path):
    _touch(tmp_path, "pkg/order_tool.py")
    _touch(tmp_path, "pkg/order_tool_test.py")
    got = PytestHarness.default_tests_for(str(tmp_path), "pkg/order_tool.py")
    assert got == os.path.join("pkg", "order_tool_test.py")


def test_discovery_tests_dir(tmp_path):
    _touch(tmp_path, "src/mylib/parser.py")
    _touch(tmp_path, "tests/test_parser.py")
    got = PytestHarness.default_tests_for(str(tmp_path), "src/mylib/parser.py")
    assert got == os.path.join("tests", "test_parser.py")


def test_discovery_falls_back_to_colocated_name(tmp_path):
    _touch(tmp_path, "src/lonely.py")
    _touch(tmp_path, "src/other.py")  # a second .py so dir_fallback finds no sole test
    got = PytestHarness.default_tests_for(str(tmp_path), "src/lonely.py")
    assert got == os.path.join("src", "test_lonely.py")  # clear error later


def test_api_dispatches_py_targets_to_the_pytest_harness(tmp_path):
    from agentboard.api import _default_tests_for
    _touch(tmp_path, "order_tool.py")
    _touch(tmp_path, "test_order_tool.py")
    assert _default_tests_for(str(tmp_path), "order_tool.py") == "test_order_tool.py"
    # unknown extensions still resolve to nothing, exactly as before
    assert _default_tests_for(str(tmp_path), "notes.txt") == ""


# ---------------------------------------------------------------------------
# selection: profile kind -> harness, target extension -> harness
# ---------------------------------------------------------------------------

def test_harness_for_profile_selects_by_kind():
    assert isinstance(harness_for_profile(_profile()), PytestHarness)
    js = RepoProfile(name="js", install_cmd=["true"], test_base=["true"])
    assert isinstance(harness_for_profile(js), VitestHarness)


def test_harness_for_target_selects_by_extension():
    assert isinstance(harness_for_target("src/x.py"), PytestHarness)
    assert isinstance(harness_for_target("src/x.ts"), VitestHarness)
    assert harness_for_target("src/x.rs") is None


# ---------------------------------------------------------------------------
# config: pytest detection and the python profile
# ---------------------------------------------------------------------------

def test_detect_profile_kind_learns_pytest(tmp_path):
    from agentboard.config import detect_profile_kind
    d = str(tmp_path)
    assert detect_profile_kind(d) == ""
    _touch(tmp_path, "pyproject.toml")
    assert detect_profile_kind(d) == "pytest"
    # a JS lockfile outranks Python markers
    _touch(tmp_path, "package-lock.json")
    assert detect_profile_kind(d) == "npm-vitest"


def test_detect_profile_kind_pytest_ini_and_setup_cfg(tmp_path):
    from agentboard.config import detect_profile_kind
    a = tmp_path / "a"
    a.mkdir()
    _touch(a, "pytest.ini")
    assert detect_profile_kind(str(a)) == "pytest"
    b = tmp_path / "b"
    b.mkdir()
    (b / "setup.cfg").write_text("[tool:pytest]\ntestpaths = tests\n")
    assert detect_profile_kind(str(b)) == "pytest"
    c = tmp_path / "c"
    c.mkdir()
    (c / "setup.cfg").write_text("[metadata]\nname = x\n")
    assert detect_profile_kind(str(c)) == ""


def test_build_profile_python_shape(tmp_path):
    import sys

    from agentboard.config import Config, build_profile
    _touch(tmp_path, "pyproject.toml")
    prof = build_profile(str(tmp_path), Config(), "tests/test_x.py")
    assert prof.kind == "pytest"
    assert prof.install_cmd == []          # env assumed provisioned
    assert prof.build_cmd is None
    assert prof.test_base == [sys.executable, "-m", "pytest"]
    assert prof.smoke_cmd[-3:] == ["--collect-only", "-q", "tests/test_x.py"]


def test_verdict_skipped_says_skipped_not_name_match(tmp_path):
    # Found by the first self-review run: a skipped injected test used to
    # report "did not run (name match failed)", which is the wrong story.
    # The verdict stays broken_test (a skip can never mint a gap); the
    # message now says what happened.
    out = _write_xml(
        tmp_path,
        '<testcase classname="t" name="test_x">'
        '<skipped message="unconditional skip"/></testcase>',
    )
    status, msg = H.read_verdict(out)
    assert status == "broken_test"
    assert "skipped" in msg
    assert "unconditional skip" in msg
    assert "name match failed" not in msg


def test_verdict_skipped_with_no_message_still_says_skipped(tmp_path):
    out = _write_xml(
        tmp_path,
        '<testcase classname="t" name="test_x"><skipped/></testcase>',
    )
    status, msg = H.read_verdict(out)
    assert status == "broken_test"
    assert "skipped" in msg
