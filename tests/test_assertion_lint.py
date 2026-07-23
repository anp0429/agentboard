"""The assertion lint's first tests ARE its three specimens: false
positives the gate's own self-reviews produced, jarred at the time with
exactly this layer in mind. Advisory only — a note never changes a
status, and unparsable code is the broken-test path's job, not ours."""

from agentboard.verifiers.assertion_lint import lint_test


def test_specimen_one_short_needle_against_prose():
    # run c6b038372514ff65: `' i' not in <human-readable surface>`
    code = (
        "def test_x():\n"
        "    out = surface()\n"
        "    assert ' i' not in out\n"
    )
    notes = lint_test(code)
    assert len(notes) == 1
    assert "needle" in notes[0] and "' i'" in notes[0]


def test_specimen_two_format_opinion_exact_match():
    # run ff36b2226dfda3eb: gap_details compared against bullet-prefixed
    # display lines
    code = (
        "def test_x():\n"
        "    assert details() == '- first broke\\n- second broke'\n"
    )
    notes = lint_test(code)
    assert len(notes) == 1
    assert "display-formatted" in notes[0]


def test_specimen_three_long_rendered_equality():
    body = "x" * 150
    code = f"def test_x():\n    assert render() == '{body}'\n"
    notes = lint_test(code)
    assert len(notes) == 1
    assert "150-char" in notes[0]


def test_honest_assertions_stay_silent():
    code = (
        "def test_x():\n"
        "    assert compute(3) == 7\n"
        "    assert 'ImportError' in str(err)\n"
        "    assert result == 'ok'\n"
        "    assert flag is True\n"
    )
    assert lint_test(code) == []


def test_unparsable_code_is_not_this_layers_job():
    assert lint_test("def broken(:\n") == []


def test_lint_note_field_exists_and_defaults_empty():
    from agentboard.review import ReviewFinding
    f = ReviewFinding(behavior="b")
    assert f.lint_note == ""
