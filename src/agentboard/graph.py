"""agentboard.graph: blast-radius scoping via code-review-graph.

AGENTBOARD_GRAPH_WRAPPER_V3 (grep signature)

Design rules (locked):
- This is the ONLY module in agentboard allowed to import code_review_graph.
- Every public function degrades gracefully when the package is missing or a
  call fails. A review must never die because the graph did.
- The gate stays the star. This module only answers "which files."
- Test-gap detection is deliberately NOT computed here. "Has tests" is
  decided by agentboard's own tests-autodetect at the CLI layer, so the
  --scope test-gaps dial reflects what the gate can actually execute.
  Where useful, pass that logic in via the gap_probe callback.

Verified against the installed code-review-graph on 2026-07-17:
- get_impact_radius: sync, lives in code_review_graph.tools.query, returns
  keys [changed_files, changed_nodes, context_savings, edges, impacted_files,
  impacted_nodes, status, summary, total_impacted, truncated].
- The sync build core is build_or_update_graph in
  code_review_graph.tools.build (confirmed by the wrapper's own probe on
  first live run). The async build_or_update_graph_tool in main.py is only
  the MCP layer; we never import main.py (it would drag in fastmcp).
- postprocess is a STRING: "full" | "minimal" | "none".

Credit: graph engine is code-review-graph by tirth8205 (MIT),
https://github.com/tirth8205/code-review-graph
Graph persists at <repo>/.code-review-graph/graph.db (SQLite, in-repo,
auto-gitignored), so builds are build-once, update-incrementally.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

_OK_STATUSES = {None, "", "ok", "success", "succeeded", "complete", "completed"}


def _log(msg: str) -> None:
    print(f"[graph] {msg}", file=sys.stderr)


def _import_get_impact_radius():
    try:
        from code_review_graph.tools.query import get_impact_radius
        return get_impact_radius
    except Exception:
        return None


def _import_build_core():
    try:
        from code_review_graph.tools.build import build_or_update_graph
        return build_or_update_graph
    except Exception:
        return None


def graph_available() -> bool:
    """True if code-review-graph is importable in this environment."""
    return _import_get_impact_radius() is not None


def ensure_graph(
    repo: Path,
    base: str = "HEAD~1",
    full_rebuild: bool = False,
    postprocess: str = "full",
) -> bool:
    """Build once / incrementally update the graph. Returns True on success.

    Safe to call every run: the engine's incremental update makes the
    no-change case cheap, and graph.db persists in-repo.
    """
    build = _import_build_core()
    if build is None:
        _log("code-review-graph build core not found; continuing without blast radius")
        return False
    try:
        result = build(
            full_rebuild=full_rebuild,
            repo_root=str(repo),
            base=base,
            postprocess=postprocess,
            recurse_submodules=None,
        )
    except Exception as exc:
        _log(f"graph build failed ({exc!r}); continuing without blast radius")
        return False
    if isinstance(result, dict):
        status = result.get("status")
        if status not in _OK_STATUSES:
            _log(f"graph build reported status={status!r}; treating as unavailable")
            return False
    return True


def blast_radius(
    repo: Path,
    changed_files: Optional[Iterable[str]] = None,
    base: str = "HEAD~1",
    depth: int = 2,
    max_results: int = 500,
) -> Optional[dict[str, Any]]:
    """Return normalized impact data, or None if the graph is unavailable.

    Normalized shape:
        impacted_files:  list[str]
        changed_files:   list[str]  (as the engine resolved them)
        changed_nodes:   passthrough
        total_impacted:  passthrough
        truncated:       bool
        context_savings: passthrough (for the cost-curve print)
        summary:         passthrough (for the cost-curve print)
        raw:             the untouched engine dict
    """
    gir = _import_get_impact_radius()
    if gir is None:
        return None
    try:
        raw = gir(
            changed_files=list(changed_files) if changed_files else None,
            max_depth=depth,
            max_results=max_results,
            repo_root=str(repo),
            base=base,
            detail_level="standard",
        )
    except Exception as exc:
        _log(f"get_impact_radius failed ({exc!r}); continuing without blast radius")
        return None
    if not isinstance(raw, dict):
        _log(f"unexpected impact result type {type(raw).__name__}; ignoring")
        return None
    status = raw.get("status")
    if status not in _OK_STATUSES:
        _log(f"impact query reported status={status!r}; ignoring result")
        return None
    return {
        "impacted_files": [str(f) for f in raw.get("impacted_files", [])],
        "changed_files": [str(f) for f in raw.get("changed_files", [])],
        "changed_nodes": raw.get("changed_nodes", []),
        "total_impacted": raw.get("total_impacted"),
        "truncated": bool(raw.get("truncated", False)),
        "context_savings": raw.get("context_savings"),
        "summary": raw.get("summary"),
        "raw": raw,
    }


def depth_costs(
    repo: Path,
    changed_files: Optional[Iterable[str]] = None,
    base: str = "HEAD~1",
    max_depth: int = 3,
    max_results: int = 500,
    gap_probe: Optional[Callable[[str], bool]] = None,
) -> list[dict[str, Any]]:
    """Per-depth cost curve: files (and optionally test-gaps) at each depth.

    Graph queries only, zero tokens spent. The CLI prints this before the
    user picks --depth, then applies the confirm-threshold on top.

    gap_probe: callable(file_path) -> True if agentboard can find tests for
    that file. When provided, each row gains a test_gaps count (files where
    gap_probe is False). The same file recurs across depths, so the caller
    should pass a memoized probe.
    """
    rows: list[dict[str, Any]] = []
    files = list(changed_files) if changed_files else None
    for d in range(1, max_depth + 1):
        result = blast_radius(repo, files, base=base, depth=d, max_results=max_results)
        if result is None:
            break
        row: dict[str, Any] = {
            "depth": d,
            "files": len(result["impacted_files"]),
            "truncated": result["truncated"],
        }
        if gap_probe is not None:
            row["test_gaps"] = sum(
                1 for f in result["impacted_files"] if not gap_probe(f)
            )
        rows.append(row)
    return rows
