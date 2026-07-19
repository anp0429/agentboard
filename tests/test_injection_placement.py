"""Injection placement is decided by the LAST top-level opener, not by
whether a describe exists anywhere in the file.

Benchmark row 6 (jotai tests/vanilla/store.test.tsx) is the regression this
file pins: describes early in the file, top-level it() calls at the end. The
old rule ("any describe anywhere -> insert before the final close") nested
all 10 proposals inside the final it's body, where `-t` filtering meant they
never registered — 10/10 "injected test did not run (name match failed)".
EOF-append at module scope was proven against jotai's real environment
before the rule was changed.
"""

from agentboard.verifiers.finding_verifier import _inject

TEST = "test('probe', () => {\n  expect(1).toBe(1);\n});"


def test_wrapping_describe_injects_inside_it():
    # zod shape: one describe wraps the whole file; proposals must land
    # inside it to inherit describe-scoped helpers.
    pristine = (
        "import { z } from 'zod';\n\n"
        "describe('errors', () => {\n"
        "  it('a', () => {});\n"
        "  it('b', () => {});\n"
        "});\n"
    )
    out, err = _inject(pristine, TEST)
    assert err == ""
    assert out.index("probe") < out.rindex("});")          # inside the describe
    assert out.rstrip().endswith("});")


def test_top_level_tests_append_at_eof():
    pristine = (
        "import { f } from '../src';\n\n"
        "test('a', () => {});\n\n"
        "test('b', () => {});\n"
    )
    out, err = _inject(pristine, TEST)
    assert err == ""
    assert out.rstrip().endswith(TEST.rstrip())            # after everything


def test_mixed_file_ending_in_top_level_it_appends_at_eof():
    # The jotai row-6 regression: describes exist, but the file ENDS with
    # top-level it() calls. Inserting before the final close would nest the
    # proposal inside the last it.
    pristine = (
        "import { atom } from 'jotai';\n\n"
        "describe('group one', () => {\n"
        "  it('x', () => {});\n"
        "});\n\n"
        "it('top level tail one', () => {\n"
        "  expect(true).toBe(true);\n"
        "});\n\n"
        "it('top level tail two', () => {\n"
        "  expect(true).toBe(true);\n"
        "});\n"
    )
    out, err = _inject(pristine, TEST)
    assert err == ""
    assert out.rstrip().endswith(TEST.rstrip())
    # and the tail test's body was NOT invaded
    tail_two = out.index("top level tail two")
    assert "probe" not in out[tail_two: out.index("});", tail_two)]


def test_file_ending_in_describe_after_earlier_tests_injects_inside():
    # Mirror case: top-level tests early, describe LAST -> inject into the
    # final describe (its close is genuinely the file's last close).
    pristine = (
        "test('early', () => {});\n\n"
        "describe('tail group', () => {\n"
        "  it('y', () => {});\n"
        "});\n"
    )
    out, err = _inject(pristine, TEST)
    assert err == ""
    assert out.index("probe") < out.rindex("});")


def test_semi_free_describe_close_still_found():
    # prettier semi:false shape (zustand): file ends `})` not `});`.
    pristine = (
        "describe('g', () => {\n"
        "  it('x', () => {})\n"
        "})\n"
    )
    out, err = _inject(pristine, TEST)
    assert err == ""
    assert out.index("probe") < out.rindex("})")
