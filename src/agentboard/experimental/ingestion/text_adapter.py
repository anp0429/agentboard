"""v0.1 ingestion: turn a plain-text outline into verified Nodes.

This adapter exists to prove the point that the library is generic. It knows
nothing about diagrams. Each non-empty line becomes a Node. That is enough to run
the whole loop end to end. The svg-graph-parser adapter (v0.2) implements the
same one-method protocol and returns Nodes that carry diagram geometry instead —
no other part of the system changes.
"""
from __future__ import annotations

import re

from ..state import Node


class TextIngestionAdapter:
    """Implements the ``IngestionAdapter`` protocol."""

    def ingest(self, source: str) -> list[Node]:
        nodes: list[Node] = []
        for i, line in enumerate(source.splitlines()):
            label = line.strip().lstrip("-*0123456789. ").strip()
            if not label:
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or f"n{i}"
            nodes.append(Node(id=slug, label=label))
        return nodes
