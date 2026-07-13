"""PeerReviewer — a SECOND model, used as a disagreement detector.

The thing to be ruthless about: two LLMs agreeing does not make an answer
correct. It makes it popular among models trained on the same internet.
Agreement cuts variance, not error. So this component is NOT a vote and NOT a
gate. Correctness was already settled by the deterministic verifier
(red-on-baseline / green-after / no-regression). A model never overrides that.

What a second model is genuinely good for is the question the deterministic gate
*cannot* answer: not "is this change correct?" but "is this change worth
asserting — does it actually address the intent, is the test meaningful?" That
is taste, and taste is exactly where models legitimately differ.

So the reviewer does one honest thing: it asks an INDEPENDENT model (must be a
different model than the one that proposed) for an agree/disagree on each
already-accepted proposal, and turns *disagreement* into a ``Conflict`` on the
board. We do not average conflicts away — agentboard surfaces them. High-severity
disagreement trips the human gate. Agreement is logged and the proposal proceeds
(the deterministic gate already passed). Nothing is ever accepted *because* a
second model liked it.

Structure mirrors ``openai_agent``: the model call and the parsing are split, so
``parse_review`` is a pure, offline-testable function with no network.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Callable

from ..state import Conflict, Node, Proposal, Severity


@dataclass
class Review:
    """One independent model's verdict on one already-accepted proposal."""
    proposal_id: str
    agrees: bool
    severity: Severity  # strength of DISAGREEMENT; drives the human gate
    reason: str


# A review function: (proposal, nodes, goal) -> Review. Injected, so the reviewer
# is model-agnostic and offline-testable with a stub.
ReviewFn = Callable[[Proposal, list[Node], str], Review]


def parse_review(proposal_id: str, data: dict) -> Review:
    """Turn a model's JSON verdict into a Review. Pure, defensive, offline.

    Expected shape: {"agrees": bool, "severity": "low|medium|high", "reason": str}
    Anything malformed defaults to AGREEMENT — a reviewer that returns garbage
    must not be allowed to manufacture a blocking conflict out of noise.
    """
    agrees = bool(data.get("agrees", True))
    sev = data.get("severity", "low")
    if sev not in ("low", "medium", "high"):
        sev = "low"
    reason = str(data.get("reason", "")).strip()[:240]
    return Review(proposal_id=proposal_id, agrees=agrees, severity=sev, reason=reason)


class PeerReviewer:
    """Runs a second model over accepted proposals and surfaces disagreement.

    NOT a verifier. NOT a gate. It returns Conflicts (and a needs_human flag);
    the loop merges these with structural conflicts on the board. It cannot flip
    an accept to a reject — only a person, prompted by a surfaced conflict, can.
    """

    def __init__(self, review_fn: ReviewFn, reviewer_name: str = "peer-model"):
        self.review_fn = review_fn
        self.reviewer_name = reviewer_name

    def review(
        self,
        goal: str,
        accepted: list[Proposal],
        nodes: list[Node],
    ) -> tuple[list[Conflict], bool]:
        conflicts: list[Conflict] = []
        needs_human = False
        for p in accepted:
            if p.change is None:
                continue  # nothing substantive to second-guess
            r = self.review_fn(p, nodes, goal)
            if r.agrees:
                continue  # the gate passed and the peer concurs — proceed quietly
            conflicts.append(
                Conflict(
                    node_ref=p.node_ref,
                    proposals=[p],
                    note=f"{self.reviewer_name} disagrees ({r.severity}): {r.reason}",
                )
            )
            if r.severity == "high":
                needs_human = True
        return conflicts, needs_human


# --- a real second-model review function (provided; needs a key to run) ------

_REVIEW_SYSTEM = (
    "You are a senior engineer giving an INDEPENDENT second opinion on a change "
    "another engineer proposed to address this goal:\n  {goal}\n\n"
    "IMPORTANT: correctness is already proven — the change passed a deterministic "
    "test gate (the new test failed before the change and passes after, with no "
    "regressions). Do NOT re-judge correctness. Judge only RELEVANCE and VALUE: "
    "does this change actually address the goal, and is its test meaningful "
    "(not trivial, not off-target)?\n"
    "Respond ONLY with JSON: "
    '{{"agrees": true|false, "severity": "low|medium|high", "reason": "<one sentence>"}}. '
    "severity is how strongly you'd block this if you disagree."
)


def anthropic_review_fn(model: str = "claude-opus-4-8", reviewer_name: str = "claude-reviewer") -> ReviewFn:
    """Second opinion via Claude. Pair this with an OpenAI-backed *proposer* (or
    vice-versa): the disagreement between two different model families is the
    informative signal — two near-identical models agreeing tells you little."""
    from anthropic import Anthropic  # local import: only needed if you call it

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def _fn(proposal: Proposal, nodes: list[Node], goal: str) -> Review:
        node_labels = {n.id: n.label for n in nodes}
        where = node_labels.get(proposal.node_ref, proposal.node_ref)
        user = (
            f"Target: {where}\n"
            f"Proposal: {proposal.text}\n"
            f"Code change: {proposal.change}\n"
            f"Test that proves it: {proposal.test_change}\n"
            "Respond ONLY with the JSON object specified."
        )
        resp = client.messages.create(
            model=model,
            max_tokens=300,
            system=_REVIEW_SYSTEM.format(goal=goal),
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        start, end = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[start : end + 1]) if start >= 0 else {}
        except json.JSONDecodeError:
            data = {}
        return parse_review(proposal.id, data)

    return _fn


def openai_review_fn(model: str = "gpt-4o", reviewer_name: str = "gpt-reviewer") -> ReviewFn:
    """Second opinion via OpenAI. Use a DIFFERENT model family than the proposer
    (e.g. GPT reviews Claude's proposals, or vice-versa) — agreement between two
    near-identical models is the least informative kind."""
    from openai import OpenAI  # local import: only needed if you actually call it

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    def _fn(proposal: Proposal, nodes: list[Node], goal: str) -> Review:
        node_labels = {n.id: n.label for n in nodes}
        where = node_labels.get(proposal.node_ref, proposal.node_ref)
        user = (
            f"Target: {where}\n"
            f"Proposal: {proposal.text}\n"
            f"Code change: {proposal.change}\n"
            f"Test that proves it: {proposal.test_change}\n"
        )
        resp = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _REVIEW_SYSTEM.format(goal=goal)},
                {"role": "user", "content": user},
            ],
        )
        try:
            data = json.loads(resp.choices[0].message.content or "{}")
        except json.JSONDecodeError:
            data = {}
        return parse_review(proposal.id, data)

    return _fn