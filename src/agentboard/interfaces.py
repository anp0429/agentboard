"""The seams. Everything swappable in agentboard implements one of these.

Four protocols, three of them the seams that make the library generic in
*mechanism* while staying narrow in *purpose*:

    IngestionAdapter  — raw source        -> verified Nodes
    Agent             — a persona         -> Proposals        (behavioral skill)
    Verifier          — proposals         -> accepted / rejected   (EXTERNAL check)
    WhiteboardAdapter — snapshots         -> a board you can open

The Verifier is the load-bearing one. It is deterministic and external on
purpose. An LLM grading its own answer is theatre — models rationalise their own
mistakes. So verification lives outside the generative path: a schema, a stated
constraint, or (v0.2) a geometric invariant. It rejects *falsehood*, never
*taste*. "Is this fix wired to a node that actually exists?" is checkable.
"Is this a good idea?" is not, and no verifier should pretend otherwise.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .state import Node, Proposal, Rejection, Snapshot


@runtime_checkable
class IngestionAdapter(Protocol):
    """Turn some raw source into verified context Nodes.

    v0.1 ships a text adapter (proves the library works with zero domain
    knowledge). v0.2 plugs in svg-graph-parser so the Nodes carry real diagram
    geometry — the differentiator no generic framework has.
    """

    def ingest(self, source: Any) -> list[Node]:
        ...


@runtime_checkable
class Agent(Protocol):
    """A persona that proposes issues and fixes. The behavioral skill layer.

    Personas commit their positions *before* seeing each other's, to kill
    anchoring and false consensus. Swap ``StubAgent`` for an LLM-backed agent by
    implementing this one method — nothing else in the loop changes.
    """

    def propose(
        self,
        persona: str,
        goal: str,
        nodes: list[Node],
        prior_committed: list[Proposal],
        iteration: int,
    ) -> list[Proposal]:
        ...


@runtime_checkable
class Verifier(Protocol):
    """The external, deterministic gate every proposal must pass before commit.

    Returns ``(accepted, rejections)``. Must not call an LLM. v0.1 checks schema
    and references; v0.2 adds geometry and constraint checks.
    """

    def verify(
        self,
        proposals: list[Proposal],
        nodes: list[Node],
        committed: list[Proposal],
    ) -> tuple[list[Proposal], list[Rejection]]:
        ...


@runtime_checkable
class WhiteboardAdapter(Protocol):
    """Project the cumulative snapshot log onto a board and return a locator
    (a file path or URL). Single writer, called once per iteration.

    Receives the ingested ``nodes`` too, so the board can draw the system being
    annotated (problems and fixes are linked back to the node they touch).
    """

    def project(self, goal: str, nodes: list[Node], snapshots: list[Snapshot]) -> str:
        ...
