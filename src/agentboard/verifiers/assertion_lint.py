"""Assertion lint — a DETERMINISTIC check for over-production-blind tests.

The prompt rule (assert exact sets) is a stopgap: it relies on the model
remembering it. This is the enforcement version. It reads a proposed test and
rejects the one failure mode that let the composite-FK cartesian-product bug pass
as "handled": a PRESENCE-ONLY matcher on a collection, with NO bound on the
collection's size. Presence-only matchers (arrayContaining, toContain, ...) are
true when the expected items are somewhere in the output; they are BLIND to extra
or duplicated items the tool fabricated. Pairing them with an explicit length
assertion closes that hole.

HONEST SCOPE (do not oversell this):
  * This is a HEURISTIC over test source, not a parser. It works per-test, not
    per-assertion: it asks "does this test use a presence matcher AND never bound
    a length?" That catches the real bug and any test shaped like it.
  * It does NOT decide whether a collection is "meant to be complete" — it cannot
    read intent. It enforces the weaker rule that is ALWAYS valid: if you assert a
    collection by presence, you must also bound its size, or over-production is
    unchecked. A test that legitimately only cares about presence can satisfy this
    by adding the length it expects.
  * No LLM here. It is advisory input to the loop (reject + regenerate), never a
    verdict on the code. It judges the TEST, not the tool.
"""
from __future__ import annotations

import re

# matchers that are satisfied by presence/inclusion — blind to extra items
_PRESENCE = re.compile(
    r"\b(arrayContaining|toContain|toContainEqual|objectContaining|stringContaining)\b"
)
# signals that the test bounds a collection's size / asserts it exactly
_LENGTH_BOUND = re.compile(
    r"\btoHaveLength\s*\(|\.length\s*\)?\s*\.\s*(toBe|toEqual|toStrictEqual)\b"
    r"|\btoStrictEqual\s*\(\s*\[|\btoEqual\s*\(\s*\["
)


def lint_assertion(test_code: str) -> tuple[bool, str]:
    """Return (ok, reason). ok=False means the test is blind to over-production.

    ok=True  -> no presence-only matcher, OR a presence matcher paired with a
                length/exact bound (over-production is checked).
    ok=False -> a presence-only matcher with no length/exact bound anywhere in
                the test: the tool could return extra/duplicated items and this
                test would still pass. Regenerate with an exact assertion.
    """
    if not test_code:
        return True, ""
    presence = _PRESENCE.search(test_code)
    if not presence:
        return True, ""                      # not using a presence matcher at all
    if _LENGTH_BOUND.search(test_code):
        return True, ""                      # presence matcher, but size is bounded
    return False, (
        f"presence-only matcher '{presence.group(1)}' used with no length/exact "
        f"bound — blind to over-production (extra or duplicated items pass). "
        f"Add toHaveLength(N) and deep-equal the full expected set."
    )
