"""Personas (behavioral skills) and a deterministic stub agent.

``StubAgent`` is scripted, not random, so the whole loop runs with no API key and
so the verify-before-commit logic is unit-testable. Replace it with an
LLM-backed agent by implementing ``Agent.propose`` (see interfaces.py). The
script is deliberately a little story:

    iteration 1  every persona raises one issue
    iteration 2  personas propose fixes; security points one fix at a node that
                 does NOT exist (the verifier must reject it); reliability and
                 simplicity propose contradictory fixes on the same node (a
                 conflict the synthesizer must surface, not average)
    iteration 3+ nothing new -> the loop converges and stops on its own
"""
from __future__ import annotations

from .state import Node, Proposal

# (name, what this persona pays attention to). Personas are just prompts/skills;
# this is the whole "team". Add or remove freely — the loop does not care.
DEFAULT_PERSONAS: list[tuple[str, str]] = [
    ("reliability", "Failure modes, retries, idempotency, the 3am page."),
    ("security", "Trust boundaries, secrets, missing authorization."),
    ("simplicity", "Pushes back on overengineering. YAGNI."),
]


class StubAgent:
    """Deterministic agent. Implements the ``Agent`` protocol."""

    def propose(
        self,
        persona: str,
        goal: str,
        nodes: list[Node],
        prior_committed: list[Proposal],
        iteration: int,
    ) -> list[Proposal]:
        if not nodes:
            return []
        n0 = nodes[0].id
        n1 = nodes[1].id if len(nodes) > 1 else n0

        if iteration == 1:
            return [
                Proposal(
                    id=f"{persona}-i1",
                    persona=persona,
                    kind="issue",
                    node_ref=n0 if persona != "security" else n1,
                    text={
                        "reliability": "No retry/backoff on the outbound call.",
                        "security": "Endpoint accepts unauthenticated writes.",
                        "simplicity": "Two services do what one could.",
                    }.get(persona, "Generic concern."),
                    severity="high" if persona == "security" else "medium",
                )
            ]

        if iteration == 2:
            issue_id = f"{persona}-i1"
            if persona == "security":
                # A fix that points at a node that does not exist. The verifier
                # MUST reject this — it is the stand-in for a hallucinated edge.
                return [
                    Proposal(
                        id="security-f1",
                        persona="security",
                        kind="fix",
                        node_ref="ghost-node",
                        text="Add an auth check on the (nonexistent) gateway.",
                        targets=issue_id,
                    )
                ]
            if persona == "reliability":
                return [
                    Proposal(
                        id="reliability-f1",
                        persona="reliability",
                        kind="fix",
                        node_ref=n0,
                        text="Wrap the call in a retry with exponential backoff.",
                        targets=issue_id,
                    )
                ]
            if persona == "simplicity":
                # Same node as reliability's fix, opposite advice -> a conflict.
                # NOTE: targets reliability's issue ON PURPOSE (not its own
                # issue_id) — two personas' fixes on one issue is what makes
                # the synthesizer surface a conflict instead of averaging.
                return [
                    Proposal(
                        id="simplicity-f1",
                        persona="simplicity",
                        kind="fix",
                        node_ref=n0,
                        text="Delete the call entirely; it is not needed.",
                        targets="reliability-i1",
                    )
                ]

        # iteration 3+: no new proposals -> convergence.
        return []