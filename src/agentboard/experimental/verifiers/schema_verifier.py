"""The v0.1 verifier: schema and reference checks. Deterministic. No LLM.

It enforces exactly the claims that are *checkable*:

  1. well-formedness     — a proposal has the fields it must have
  2. real references     — node_ref points at a Node that actually exists
  3. real targets        — a fix targets an issue that was actually committed

That is the whole job. It does not, and must not, judge whether a proposal is a
*good idea*. Rule 2 is the small version of the thing that makes the diagram use
case special: in v0.2 the same check becomes "does this edge exist in the
verified scene graph?", catching an agent that hallucinated structure before it
ever reaches the board.
"""
from __future__ import annotations

from ..state import Node, Proposal, Rejection


class SchemaVerifier:
    """Implements the ``Verifier`` protocol."""

    def verify(
        self,
        proposals: list[Proposal],
        nodes: list[Node],
        committed: list[Proposal],
    ) -> tuple[list[Proposal], list[Rejection]]:
        known_nodes = {n.id for n in nodes}
        known_issues = {p.id for p in committed if p.kind == "issue"}
        # issues committed earlier this same batch also count as valid targets
        known_issues |= {p.id for p in proposals if p.kind == "issue"}

        accepted: list[Proposal] = []
        rejected: list[Rejection] = []

        for p in proposals:
            if not p.id or not p.persona or p.kind not in ("issue", "fix"):
                rejected.append(Rejection(p, "malformed proposal (missing fields)"))
                continue
            if p.node_ref not in known_nodes:
                rejected.append(
                    Rejection(p, f"references unknown node '{p.node_ref}'")
                )
                continue
            if p.kind == "fix" and p.targets and p.targets not in known_issues:
                rejected.append(
                    Rejection(p, f"fix targets unknown issue '{p.targets}'")
                )
                continue
            accepted.append(p)

        return accepted, rejected
