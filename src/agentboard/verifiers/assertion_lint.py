"""Deterministic brittleness lint for PROPOSED test code — the executable
form of the exactness rules the prompts state only in words. Advisory
precision layer in the artifact_note tradition: it annotates confirmed
gaps whose deciding assertion has a shape that historically produced
false positives, and NEVER changes a status.

Born from three specimens the gate's own self-reviews produced:
(1) a two-character substring asserted against a prose sentence
    (run c6b038372514ff65: `' i' not in <human-readable surface>`);
(2) exact equality against a display-formatted string — a format opinion
    about bullet prefixes wearing a behavior claim's clothes
    (run ff36b2226dfda3eb, gap_details);
(3) exact equality against a long rendered string, where any legitimate
    wording change breaks the test (the benchmark's known miss class).

No model, no cost, always on. A flagged gap is still a gap — the lint is
a reading aid for the human deciding whether the failing test's
assertion is about behavior or about presentation.
"""

from __future__ import annotations

import ast

_DISPLAY_MARKERS = ("\n- ", "\n  - ", "\n* ", "\u2022")
_LONG_RENDERED = 120
_SHORT_NEEDLE = 3


def _str_value(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def lint_test(test_code: str) -> list[str]:
    """Human-readable brittleness notes for one proposal's test code.
    Empty list = nothing suspicious (or unparsable code, which the gate
    already reports as a broken test — not this layer's job)."""
    try:
        tree = ast.parse(test_code or "")
    except SyntaxError:
        return []
    notes: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        op = node.ops[0]
        left, right = node.left, node.comparators[0]
        needle = _str_value(left)
        if isinstance(op, (ast.In, ast.NotIn)) and needle is not None:
            if needle.strip() and len(needle.strip()) <= _SHORT_NEEDLE:
                notes.append(
                    f"line {node.lineno}: membership test with the "
                    f"{len(needle)}-char needle {needle!r} — short "
                    "substrings match prose by accident; assert a full "
                    "token or an exact value instead")
        if isinstance(op, (ast.Eq, ast.NotEq)):
            for side in (left, right):
                s = _str_value(side)
                if s is None:
                    continue
                if len(s) >= _LONG_RENDERED:
                    notes.append(
                        f"line {node.lineno}: exact equality against a "
                        f"{len(s)}-char rendered string — whole-output "
                        "matches break on any legitimate wording change; "
                        "assert the substance, not the rendering")
                    break
                if s.lstrip().startswith(("- ", "* ")) \
                        or any(m in s for m in _DISPLAY_MARKERS):
                    notes.append(
                        f"line {node.lineno}: exact equality against a "
                        "display-formatted string (bullet/prefix "
                        "markers) — format opinions are not behavior; "
                        "assert content, not presentation")
                    break
    return notes
