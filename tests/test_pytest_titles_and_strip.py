"""Title extraction, mark stamping, and import stripping regressions, all
from the gate's second self-review (run ff36b2226dfda3eb). The invariants:
the REAL test function is found by parsing, never by regex over raw text;
class-nested tests get class-qualified node titles so serial execution
selects the exact collected testcase; and injection may deduplicate only
column-0 imports, because an indented import is part of a test body."""

from agentboard.verifiers.pytest_harness import PytestHarness


H = PytestHarness()


def test_docstring_test_def_lookalike_is_not_the_title():
    code = (
        '"""Example:\n'
        "    def test_not_collected():\n"
        "        pass\n"
        '"""\n'
        "def test_real_proposal():\n"
        "    assert 1\n"
    )
    assert H.test_title(code) == "test_real_proposal"


def test_class_based_proposal_gets_class_qualified_node_title():
    code = (
        "class TestProof:\n"
        "    def test_rejects_bad_total(self):\n"
        "        assert 1\n"
    )
    assert H.test_title(code) == "TestProof::test_rejects_bad_total"


def test_mark_stamps_the_real_def_not_the_docstring_lookalike():
    code = (
        '"""def test_fake(): ...\n"""\n'
        "def test_real():\n"
        "    assert 1\n"
    )
    marked = H.mark_title(code, "___ab0___")
    assert "def test_real___ab0___(" in marked
    assert "test_fake___ab0___" not in marked


def test_unparsable_code_falls_back_to_the_old_regex():
    code = "def test_broken(:\n    pass\n"
    assert H.test_title(code) == "test_broken"


def test_indented_import_in_test_body_is_preserved():
    pristine = "from order_tool import find_orders\n\nORDERS = []\n"
    proposal = (
        "def test_local_import_is_still_a_statement():\n"
        "    from order_tool import find_orders\n"
        "    assert find_orders([], 'open', 1) == []\n"
    )
    out = H.strip_imports(proposal, pristine)
    assert "    from order_tool import find_orders" in out


def test_column_zero_duplicate_import_is_still_stripped():
    pristine = "from order_tool import find_orders\n"
    proposal = (
        "from order_tool import find_orders\n"
        "def test_x():\n"
        "    assert find_orders\n"
    )
    out = H.strip_imports(proposal, pristine)
    # the output is the proposal alone, so a stripped duplicate is absent
    assert "from order_tool import find_orders" not in out
    assert out.lstrip().startswith("def test_x")
