"""Default whiteboard: a single self-contained HTML file.

This is the zero-friction default — no account, no dependency, opens in any
browser. It renders the cumulative snapshot log as one column per iteration so
you can literally see the run evolve: what was found, what was committed, what
the verifier rejected and why, and where personas conflicted.

tldraw is the next adapter (see tldraw_adapter.py); it implements the same
one-method protocol.
"""
from __future__ import annotations

import html
import os
import tempfile

from ..state import Node, Snapshot

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin:0; font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
       background:#f6f6f4; color:#26251f; }
@media (prefers-color-scheme: dark){ body{ background:#1c1c1a; color:#e8e6df; } }
header { padding:20px 24px; border-bottom:1px solid #d9d8d2; }
@media (prefers-color-scheme: dark){ header{ border-color:#3a3a36; } }
h1 { font-size:18px; font-weight:500; margin:0; }
.sub { color:#76756e; font-size:13px; margin-top:4px; }
.cols { display:flex; gap:18px; padding:24px; overflow-x:auto; align-items:flex-start; }
.col { min-width:300px; max-width:340px; flex:0 0 auto; }
.col h2 { font-size:14px; font-weight:500; margin:0 0 4px; }
.col .csub { font-size:12px; color:#76756e; margin-bottom:12px; }
.card { border-radius:12px; padding:12px 14px; margin-bottom:10px; border:1px solid;
        background:#fff; }
@media (prefers-color-scheme: dark){ .card{ background:#26261f; } }
.card .who { font-size:11px; text-transform:uppercase; letter-spacing:.04em;
             color:#76756e; margin-bottom:4px; }
.card .ref { font-size:11px; color:#76756e; margin-top:6px; }
.issue { border-color:#85b7eb; }
.fix   { border-color:#5dcaa5; }
.rej   { border-color:#f09595; opacity:.92; }
.rej .reason { color:#a32d2d; font-size:12px; margin-top:6px; }
.conf  { border-color:#ef9f27; }
.conf .note { color:#854f0b; font-size:12px; margin-top:6px; }
.tag { display:inline-block; font-size:11px; padding:1px 7px; border-radius:20px;
       border:1px solid #d9d8d2; color:#76756e; margin-left:6px; }
.empty { color:#9c9a92; font-size:13px; font-style:italic; }
"""


def _card(cls: str, who: str, body: str, ref: str = "", extra: str = "") -> str:
    ref_html = f'<div class="ref">{html.escape(ref)}</div>' if ref else ""
    return (
        f'<div class="card {cls}"><div class="who">{html.escape(who)}</div>'
        f"<div>{html.escape(body)}</div>{ref_html}{extra}</div>"
    )


class HtmlWhiteboardAdapter:
    """Implements the ``WhiteboardAdapter`` protocol."""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(tempfile.gettempdir(), "agentboard.html")

    def project(self, goal: str, nodes: list[Node], snapshots: list[Snapshot]) -> str:
        cols = []
        for snap in snapshots:
            cards = []
            for p in snap.accepted:
                cls = "issue" if p.kind == "issue" else "fix"
                sev = f'<span class="tag">{p.severity}</span>' if p.kind == "issue" else ""
                cards.append(
                    _card(cls, f"{p.persona} · {p.kind}{sev}", p.text, f"on: {p.node_ref}")
                )
            for c in snap.conflicts:
                names = ", ".join(pr.persona for pr in c.proposals)
                cards.append(
                    _card("conf", f"conflict · {names}", c.note, f"on: {c.node_ref}")
                )
            for r in snap.rejected:
                cards.append(
                    _card(
                        "rej",
                        f"{r.proposal.persona} · rejected",
                        r.proposal.text,
                        f"on: {r.proposal.node_ref}",
                        extra=f'<div class="reason">rejected: {html.escape(r.reason)}</div>',
                    )
                )
            body = "".join(cards) or '<div class="empty">no changes — converged</div>'
            cols.append(
                f'<div class="col"><h2>Iteration {snap.iteration}</h2>'
                f'<div class="csub">{html.escape(snap.summary)}</div>{body}</div>'
            )

        doc = (
            f"<!doctype html><meta charset=utf-8><title>agentboard</title>"
            f"<style>{_CSS}</style>"
            f'<header><h1>agentboard</h1><div class="sub">{html.escape(goal)}</div></header>'
            f'<div class="cols">{"".join(cols)}</div>'
        )
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(doc)
        return self.path
