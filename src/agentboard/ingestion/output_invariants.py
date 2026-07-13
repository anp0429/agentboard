"""Output invariants — the general 'dimensions' axis of a review.

These are domain-INDEPENDENT properties a produced result should satisfy. They
come from property-based testing (QuickCheck / Hypothesis / fast-check), where a
test asserts an invariant that must hold for ALL inputs, not one example. They are
the reusable 'years of experience' layer: they apply to a database tool, an auth
layer, a serializer — anything that produces a result a consumer relies on.

WHY THIS IS NOT A CHECKLIST-IN-A-PROMPT
---------------------------------------
Handing a model these names does NOT make its tests correct. Studies that
hand-labeled LLM-written property tests found many were UNSOUND: the property held
for most inputs but missed edge cases, or was hallucinated outside the real
contract. That is the composite-FK failure exactly. So these invariants are only
trustworthy AFTER the deterministic gate runs the test they become. The list is an
INPUT to the engine; the engine (write test -> run -> classify) is the product.

LEAK RULE (non-negotiable)
--------------------------
Each invariant is stated as a PROPERTY of a result, never as a named structure of
any specific domain. "the result contains no item without a real source" — never
"check foreign keys". The reviewer must map the property to this change itself;
naming the structure would make a finding the prompt read back.

Each invariant carries the ASSERTION SHAPE it forces, so it lands as a runnable
test, not an adjective.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Invariant:
    id: str
    name: str
    question: str          # the general question the reviewer asks of the change
    catches: str           # the failure mode it exposes
    assertion_shape: str   # how a test must assert it (drives strong assertions)


OUTPUT_INVARIANTS: list[Invariant] = [
    Invariant(
        id="completeness",
        name="Completeness",
        question="Is every item that should be in the result actually present?",
        catches="dropped / missing items (the tool silently omits something real).",
        assertion_shape="assert every expected item appears; assert the count is not short.",
    ),
    Invariant(
        id="soundness",
        name="Soundness",
        question="Does every item in the result correspond to something real — nothing invented or duplicated?",
        catches="fabricated or duplicated items (the tool emits something with no real source).",
        assertion_shape=("assert the EXACT set: exact length AND full deep-equal, so extra or "
                         "duplicated items fail. Presence-only matchers are forbidden here — they "
                         "pass when the tool over-produces."),
    ),
    Invariant(
        id="uniqueness",
        name="Uniqueness",
        question="Are items that must be distinct actually distinct?",
        catches="duplicate entries where the contract implies a set, not a bag.",
        assertion_shape="assert no duplicates: the deduplicated length equals the length.",
    ),
    Invariant(
        id="count_relation",
        name="Count relation",
        question="Is the size of the result the size the input implies (not more, not fewer)?",
        catches="cardinality blow-ups (e.g. a product where a 1:1 mapping was expected) or shortfalls.",
        assertion_shape="assert the result length equals the exact number the input dictates.",
    ),
    Invariant(
        id="ordering_stability",
        name="Ordering stability",
        question="If order is meaningful, is it the specified order — and stable across runs?",
        catches="nondeterministic or wrong ordering that a consumer or a diff depends on.",
        assertion_shape="assert the exact order; or, if order is unspecified, sort before deep-equal AND assert stability across two runs.",
    ),
    Invariant(
        id="idempotence",
        name="Idempotence / determinism",
        question="Does the same input produce the same output when the operation is repeated?",
        catches="run-to-run drift; hidden state leaking between calls.",
        assertion_shape="run twice on unchanged input; assert the two results deep-equal.",
    ),
]


def invariant_ids() -> list[str]:
    return [i.id for i in OUTPUT_INVARIANTS]


def as_prompt_block() -> str:
    """Render the invariants as a general reasoning block for a reviewer prompt.

    No structure names — properties only. The reviewer reasons across these for
    the change in front of it and writes ONE test per applicable property, with
    the assertion shape the invariant forces.
    """
    lines = [
        "REVIEW ACROSS THESE OUTPUT INVARIANTS (general properties, not a checklist "
        "to paste). For the change under review, decide which apply, then for each "
        "that applies write ONE test that asserts it with the assertion shape given. "
        "Do not name specific structures from memory — reason from the property to "
        "this change yourself.",
    ]
    for inv in OUTPUT_INVARIANTS:
        lines.append(
            f"- {inv.name}: {inv.question} Catches: {inv.catches} "
            f"Assert: {inv.assertion_shape}"
        )
    return "\n".join(lines)
