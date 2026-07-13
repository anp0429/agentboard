"""tldraw adapter — the seam, stubbed honestly for v0.2.

Implements the ``WhiteboardAdapter`` protocol (same single ``project`` method as
the working HTML adapter). It is intentionally not finished: the point of v0.1 is
to prove the seam exists and is type-correct, not to ship half-tested API calls.

  - tldraw: map each Proposal/Conflict/Rejection to a tldraw shape record and
    write a .tldr document (tldraw's persisted snapshot format). tldraw is the
    intended zero-account interactive default once the shape mapping is pinned to
    a tldraw version.
"""
from __future__ import annotations

from ..state import Node, Snapshot


class TldrawWhiteboardAdapter:
    """Implements ``WhiteboardAdapter``. v0.2."""

    def __init__(self, path: str = "agentboard.tldr"):
        self.path = path

    def project(self, goal: str, nodes: list[Node], snapshots: list[Snapshot]) -> str:
        raise NotImplementedError(
            "v0.2: map snapshots -> tldraw shape records, write a .tldr document. "
            "One column of shapes per iteration; reuse the layout math from the "
            "HTML adapter for x/y placement."
        )