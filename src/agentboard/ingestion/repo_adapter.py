"""Ingestion adapter for a code repository.

Each source module becomes a Node. That is the set of things the agents are
allowed to touch, and the set the verifier checks edits against. Same one-method
protocol as the text adapter — the loop does not change, only what it reviews.
"""
from __future__ import annotations

import os

from ..state import Node


class RepoIngestionAdapter:
    """Implements the ``IngestionAdapter`` protocol.

    Walks ``package_dir`` (relative to ``root``) for ``.py`` files, skipping
    tests, dunder files, and hidden dirs. Node id is the path relative to root.
    """

    def __init__(self, root: str, package_dir: str = "."):
        self.root = root
        self.package_dir = package_dir

    def ingest(self, source: object = None) -> list[Node]:
        base = os.path.join(self.root, self.package_dir)
        nodes: list[Node] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "__")) and d != "tests"]
            for fn in sorted(filenames):
                if not fn.endswith(".py") or fn == "__init__.py":
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, self.root)
                nodes.append(Node(id=rel, label=rel))
        return nodes
