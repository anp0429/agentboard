"""The import-matching picker, ported from the self-review workflow into
the pytest harness. The link under test: a test-shaped file that imports
the target's module IS its suite, even when no basename convention holds.
Selection must be deterministic: unique hit wins, stem-in-filename beats
plain importers, closest directory beats distant ones."""

import os

from agentboard.verifiers.pytest_harness import PytestHarness


def _mk(root, rel, text=""):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def test_basename_convention_still_wins_first(tmp_path):
    r = str(tmp_path)
    _mk(r, "src/pkg/cli.py")
    _mk(r, "tests/test_cli.py", "from pkg.cli import main\n")
    _mk(r, "tests/test_zeta.py", "from pkg.cli import main\n")
    got = PytestHarness.default_tests_for(r, "src/pkg/cli.py")
    assert got == os.path.join("tests", "test_cli.py")


def test_import_match_rescues_behavior_named_suites(tmp_path):
    r = str(tmp_path)
    _mk(r, "src/pkg/cli.py")
    _mk(r, "tests/test_parity_rules.py", "from pkg.cli import main\n")
    _mk(r, "tests/test_other.py", "from pkg.config import load\n")
    got = PytestHarness.default_tests_for(r, "src/pkg/cli.py")
    assert got == os.path.join("tests", "test_parity_rules.py")


def test_several_importers_prefer_stem_in_filename(tmp_path):
    r = str(tmp_path)
    _mk(r, "src/pkg/cli.py")
    _mk(r, "tests/test_aardvark.py", "import pkg.cli\n")
    _mk(r, "tests/test_cli_extras.py", "import pkg.cli\n")
    got = PytestHarness.default_tests_for(r, "src/pkg/cli.py")
    assert got == os.path.join("tests", "test_cli_extras.py")


def test_commented_imports_do_not_count(tmp_path):
    r = str(tmp_path)
    _mk(r, "src/pkg/cli.py")
    _mk(r, "tests/test_a.py", "# from pkg.cli import main\nX = 1\n")
    got = PytestHarness.default_tests_for(r, "src/pkg/cli.py")
    assert got == os.path.join("src", "pkg", "test_cli.py")  # error contract


def test_dot_dirs_never_supply_the_suite(tmp_path):
    r = str(tmp_path)
    _mk(r, "src/pkg/cli.py")
    _mk(r, ".venv/lib/test_cli.py", "import pkg.cli\n")
    got = PytestHarness.default_tests_for(r, "src/pkg/cli.py")
    assert got == os.path.join("src", "pkg", "test_cli.py")


def test_docstring_import_text_cannot_fabricate_a_match(tmp_path):
    r = str(tmp_path)
    _mk(r, "src/pkg/widget.py")
    _mk(r, "tests/test_docs.py",
        '"""Examples:\n    from pkg.widget import spin\n"""\nX = 1\n')
    _mk(r, "tests/test_widget_real.py", "from pkg.widget import spin\n")
    got = PytestHarness.default_tests_for(r, "src/pkg/widget.py")
    assert got == os.path.join("tests", "test_widget_real.py")


def test_multiline_aliased_from_import_is_seen(tmp_path):
    r = str(tmp_path)
    _mk(r, "src/pkg/checkout.py")
    _mk(r, "tests/test_payments.py",
        "from pkg import (\n    checkout as co,\n)\n")
    got = PytestHarness.default_tests_for(r, "src/pkg/checkout.py")
    assert got == os.path.join("tests", "test_payments.py")


def test_package_init_matches_tests_importing_the_package(tmp_path):
    r = str(tmp_path)
    _mk(r, "src/acme/__init__.py")
    _mk(r, "tests/test_acme_package.py", "import acme\n")
    got = PytestHarness.default_tests_for(r, "src/acme/__init__.py")
    assert got == os.path.join("tests", "test_acme_package.py")


def test_syntax_error_candidates_are_skipped_not_fatal(tmp_path):
    r = str(tmp_path)
    _mk(r, "src/pkg/cli.py")
    _mk(r, "tests/test_broken.py", "def broken(:\n")
    _mk(r, "tests/test_fine.py", "import pkg.cli\n")
    got = PytestHarness.default_tests_for(r, "src/pkg/cli.py")
    assert got == os.path.join("tests", "test_fine.py")
