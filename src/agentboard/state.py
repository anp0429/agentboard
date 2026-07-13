"""Core data structures and the blackboard state.

The blackboard (``Board``) is the single source of truth. Agents read and write
*this*, never the whiteboard directly. The whiteboard is a render target that a
single projector writes once per iteration. Keeping one writer is what stops the
board from turning into overlapping garbage and what makes runs reproducible.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Literal, TypedDict

Severity = Literal["low", "medium", "high"]
Status = Literal["running", "converged", "budget_exhausted", "needs_human"]


@dataclass
class Node:
    """A verified entity in the ingested context.

    Ingestion adapters produce these. For a text task a Node is a section or
    entity; for the svg-graph-parser adapter (v0.2) a Node is a diagram shape
    with resolved geometry. Either way, ``Node.id`` is the set of references a
    proposal is *allowed* to point at. The verifier rejects anything else.
    """

    id: str
    label: str


@dataclass
class CodeChange:
    """An actual edit a proposal carries, so the verifier can apply it and run
    the tests. ``find``/``replace`` does a single textual replacement; ``append``
    adds text to the end of the file. Path is relative to the repo root."""

    path: str
    find: str | None = None
    replace: str | None = None
    append: str | None = None

    def key(self) -> str:
        """Identity for conflict detection: two edits with the same key touch the
        same thing."""
        return f"{self.path}::{self.find or ('append:' + (self.append or '')[:24])}"


@dataclass
class Proposal:
    """One thing an agent wants to commit: an issue it found or a fix it suggests."""

    id: str
    persona: str
    kind: Literal["issue", "fix"]
    node_ref: str  # MUST match a real Node.id — this is the claim the verifier checks
    text: str
    severity: Severity = "medium"
    targets: str | None = None  # for a fix: the issue id it addresses
    change: "CodeChange | None" = None  # if set, the verifier runs tests on it
    test_change: "CodeChange | None" = None  # the NEW test that proves `change`; enables transition verification
    test: "CodeChange | None" = None  # the NEW test that proves `change` (transition check)


@dataclass
class Rejection:
    """A proposal the verifier refused to commit, with the deterministic reason.

    Rejections are kept, not discarded. They become the "what they tried and why
    it failed" trail on the board — the audit history that makes the output
    trustworthy instead of a highlight reel. ``explanation`` is an optional
    LLM-written narrative (interpretation, not fact)."""

    proposal: Proposal
    reason: str
    explanation: str = ""


@dataclass
class Conflict:
    """Two committed proposals that disagree about the same node.

    We do not average these away. We surface them. A high-severity conflict is
    what trips the human gate. ``explanation`` is an optional LLM-written
    narrative of the tradeoff (interpretation, not fact)."""

    node_ref: str
    proposals: list[Proposal]
    note: str
    explanation: str = ""


@dataclass
class Snapshot:
    """An immutable record of one iteration. This is the unit the board renders."""

    iteration: int
    accepted: list[Proposal] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    summary: str = ""


class Board(TypedDict, total=False):
    """The LangGraph state object. Lists tagged with ``operator.add`` accumulate
    across iterations (the reducer concatenates returns); everything else is
    overwritten per node."""

    goal: str
    nodes: list[Node]                                   # verified context (ref allow-list)
    iteration: int
    budget: int                                         # max iterations
    committed: Annotated[list[Proposal], operator.add]  # accumulates across the run
    snapshots: Annotated[list[Snapshot], operator.add]  # the append-only board log
    last_proposals: list[Proposal]                      # this iteration, pre-verify
    last_rejections: list[Rejection]
    last_conflicts: list[Conflict]
    last_delta: int                                     # accepted-this-iteration count
    status: Status
    needs_human: bool
    board_location: str                                 # where the projector wrote