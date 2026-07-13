"""Flow whiteboard: a brainstorm map, the way humans actually use a whiteboard.

Stickies and arrows on one connected canvas, not columns. Each system node is an
anchor; problems hang off the node that has them; fixes hang off the problem they
address. When two personas propose competing fixes for the same problem, both are
drawn side by side under a CONFLICT band that shows *what each one said* — so a
human has something to decide with. Rejected attempts are kept in a lane at the
bottom with the reason they failed. Every sticky carries an iteration badge (i1,
i2, ...) so the append-only history survives in the spatial view.

The layout is deterministic coordinate math — the same geometry-first approach
that makes the board clean instead of a pile of overlapping notes.
"""
from __future__ import annotations

import html
import os
import tempfile

from ..state import Node, Proposal, Rejection, Snapshot

# ---- geometry constants ----
NODE_X, NODE_W = 24, 150
ISS_X, ISS_W = 224, 230
FIX_X, FIX_W = 510, 230
TOP = 120                     # header height
LINE_H = 16
CHAR_W = 7.0                  # approx px per char at 12.5px
V_GAP = 18                    # vertical gap between sibling stickies
PAD_X, PAD_TOP, PAD_BOT = 12, 26, 12


def _wrap(text: str, width_px: int) -> list[str]:
    max_chars = max(8, int((width_px - 2 * PAD_X) / CHAR_W))
    # hard-break any token longer than a line (file paths, test ids have no spaces)
    raw = text.split()
    words: list[str] = []
    for w in raw:
        while len(w) > max_chars:
            words.append(w[:max_chars])
            w = w[max_chars:]
        words.append(w)
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= max_chars:
            cur = f"{cur} {w}".strip()
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _h(lines: list[str]) -> int:
    return PAD_TOP + len(lines) * LINE_H + PAD_BOT


def _change_str(change) -> str:
    if change is None:
        return ""
    if change.append is not None:
        first = change.append.strip().splitlines()[0] if change.append.strip() else ""
        return f"＋ {first}"
    return f"{(change.find or '').strip()}  →  {(change.replace or '').strip()}"


def _sticky(x, y, w, lines, cls, persona, badge, title="", code=""):
    h = _h(lines)
    e = html.escape
    code = code if len(code) <= 40 else code[:39] + "…"
    head = f'<text class="who" x="{x+PAD_X}" y="{y+16}">{e(persona)}</text>'
    badge_html = (
        f'<text class="badge" x="{x+w-PAD_X}" y="{y+16}" text-anchor="end">{e(badge)}</text>'
    )
    tspans = "".join(
        f'<tspan x="{x+PAD_X}" y="{y+PAD_TOP+10+i*LINE_H}">{e(ln)}</tspan>'
        for i, ln in enumerate(lines)
    )
    title_html = (
        f'<text class="ttl" x="{x+PAD_X}" y="{y+PAD_TOP+10}">{e(title)}</text>' if title else ""
    )
    if title:
        tspans = "".join(
            f'<tspan x="{x+PAD_X}" y="{y+PAD_TOP+10+(i+1)*LINE_H}">{e(ln)}</tspan>'
            for i, ln in enumerate(lines)
        )
        h += LINE_H
    code_html = ""
    if code:
        h += LINE_H + 6
        code_html = f'<text class="diff" x="{x+PAD_X}" y="{y+h-11}">{e(code)}</text>'
    # tspans MUST live inside a <text> element or strict browsers drop them
    body_text = f'<text class="body">{tspans}</text>' if tspans else ""
    rect = f'<rect class="card {cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="10"/>'
    return rect + head + badge_html + title_html + body_text + code_html, h


def _edge(x1, y1, x2, y2, label="", dashed=False):
    midx = (x1 + x2) / 2
    dash = ' stroke-dasharray="4 4"' if dashed else ""
    path = (
        f'<path class="edge" d="M{x1},{y1} C{midx},{y1} {midx},{y2} {x2},{y2}" '
        f'fill="none"{dash} marker-end="url(#arrow)"/>'
    )
    lbl = (
        f'<text class="elbl" x="{midx}" y="{(y1+y2)/2 - 4}" text-anchor="middle">{html.escape(label)}</text>'
        if label
        else ""
    )
    return path + lbl


_CSS = """
:root { color-scheme: light dark; }
body { margin:0; font:13px -apple-system,Segoe UI,Roboto,sans-serif; background:#f4f3ee; color:#26251f; }
@media (prefers-color-scheme:dark){ body{background:#1b1b19;color:#e8e6df;} }
.h1 { font-size:16px; font-weight:600; }
.sub { font-size:12px; fill:#7a7970; }
.card { stroke-width:1.4; }
.node { fill:#eceae2; stroke:#b9b7ad; }
.issue { fill:#e9f1fb; stroke:#5b9bd5; }
.fix { fill:#e6f6ef; stroke:#3fae84; }
.confwrap { fill:none; stroke:#e8a33d; stroke-width:1.6; stroke-dasharray:6 4; }
.rej { fill:#fbecec; stroke:#d98a8a; }
@media (prefers-color-scheme:dark){
 .node{fill:#2a2a23;stroke:#54534b;} .issue{fill:#1e2b3a;stroke:#4f82b3;}
 .fix{fill:#16302a;stroke:#379a76;} .rej{fill:#33201f;stroke:#9c5b5b;} }
text { fill:#26251f; } @media (prefers-color-scheme:dark){ text{fill:#e8e6df;} }
.who { font-size:10px; font-weight:600; letter-spacing:.05em; text-transform:uppercase; fill:#86857c; }
.badge { font-size:10px; font-weight:700; fill:#86857c; }
.ttl { font-size:11px; font-weight:700; fill:#a8731a; }
.edge { stroke:#9d9b91; stroke-width:1.4; }
.elbl { font-size:10px; fill:#9d9b91; }
.conflbl { font-size:11px; font-weight:700; fill:#c47f15; }
.lane { font-size:12px; font-weight:600; fill:#7a7970; }
.body { fill:#26251f; }
@media (prefers-color-scheme:dark){ .body { fill:#e8e6df; } }
.diff { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:10.5px; fill:#5f7a52; }
.why { font-size:11px; font-style:italic; fill:#7a7970; }
"""


class FlowWhiteboardAdapter:
    """Implements the ``WhiteboardAdapter`` protocol. Renders a brainstorm map."""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(tempfile.gettempdir(), "agentboard_flow.html")

    def project(self, goal: str, nodes: list[Node], snapshots: list[Snapshot]) -> str:
        # ---- flatten the cumulative log, tagging each item with its iteration ----
        iter_of: dict[str, int] = {}
        issues: list[Proposal] = []
        fixes: list[Proposal] = []
        rejections: list[tuple[Rejection, int]] = []
        conflict_expl: dict[frozenset, str] = {}
        for snap in snapshots:
            for p in snap.accepted:
                iter_of[p.id] = snap.iteration
                (issues if p.kind == "issue" else fixes).append(p)
            for r in snap.rejected:
                rejections.append((r, snap.iteration))
            for c in snap.conflicts:
                conflict_expl[frozenset(p.id for p in c.proposals)] = c.explanation

        label_of = {n.id: n.label for n in nodes}
        issues_by_node: dict[str, list[Proposal]] = {}
        for iss in issues:
            issues_by_node.setdefault(iss.node_ref, []).append(iss)
        fixes_by_issue: dict[str, list[Proposal]] = {}
        for fx in fixes:
            if fx.targets:
                fixes_by_issue.setdefault(fx.targets, []).append(fx)

        body: list[str] = []
        y = TOP

        # only draw modules that actually have findings; count the rest
        active_nodes = [n for n in nodes if issues_by_node.get(n.id)]
        inactive = len(nodes) - len(active_nodes)

        # ---- lay out node -> issues -> fixes top to bottom ----
        for node in active_nodes:
            n_issues = issues_by_node.get(node.id, [])
            node_block_top = y

            if not n_issues:
                lines = _wrap(node.label, NODE_W)
                seg, h = _sticky(NODE_X, y, NODE_W, lines, "node", "node", "", "")
                body.append(seg)
                y += h + V_GAP
                continue

            issue_centers = []
            for iss in n_issues:
                iss_lines = _wrap(iss.text, ISS_W)
                iss_h = _h(iss_lines) + (LINE_H if iss.severity else 0)
                n_fixes = fixes_by_issue.get(iss.id, [])

                fix_top = y
                fix_centers = []
                if n_fixes:
                    fy = y
                    for fx in n_fixes:
                        fl = _wrap(fx.text, FIX_W)
                        seg, fh = _sticky(
                            FIX_X, fy, FIX_W, fl, "fix", fx.persona,
                            f"i{iter_of.get(fx.id,'?')}", title="proposed fix",
                            code=_change_str(fx.change),
                        )
                        body.append(seg)
                        fix_centers.append(fy + fh / 2)
                        fy += fh + V_GAP
                    fix_block_h = (fy - V_GAP) - fix_top
                else:
                    fix_block_h = 0

                iss_y = y if fix_block_h <= iss_h else y + (fix_block_h - iss_h) / 2
                title = f"problem · {iss.severity}"
                seg, ih = _sticky(
                    ISS_X, iss_y, ISS_W, iss_lines, "issue", iss.persona,
                    f"i{iter_of.get(iss.id,'?')}", title=title,
                )
                body.append(seg)
                iss_center = iss_y + ih / 2
                issue_centers.append(iss_center)

                # edges issue -> each fix
                for fc in fix_centers:
                    body.append(_edge(ISS_X + ISS_W, iss_center, FIX_X, fc, "fixes"))

                # conflict band around competing fixes from different personas
                expl_h = 0
                if len(n_fixes) > 1 and len({f.persona for f in n_fixes}) > 1:
                    band_top = fix_top - 10
                    band_h = fix_block_h + 20
                    body.append(
                        f'<rect class="confwrap" x="{FIX_X-10}" y="{band_top}" '
                        f'width="{FIX_W+20}" height="{band_h}" rx="12"/>'
                    )
                    body.append(
                        f'<text class="conflbl" x="{FIX_X+FIX_W/2}" y="{band_top-6}" '
                        f'text-anchor="middle">⚠ CONFLICT — human decides</text>'
                    )
                    expl = conflict_expl.get(frozenset(f.id for f in n_fixes), "")
                    if expl:
                        elines = _wrap("why it's a real call: " + expl, FIX_W + 20)
                        ey = band_top + band_h + 14
                        for i, ln in enumerate(elines):
                            body.append(
                                f'<text class="why" x="{FIX_X-10}" y="{ey + i*LINE_H}">'
                                f'{html.escape(ln)}</text>'
                            )
                        expl_h = len(elines) * LINE_H + 14

                y += max(iss_h, fix_block_h + expl_h) + V_GAP

            # node sticky, vertically centered against its issues
            n_lines = _wrap(node.label, NODE_W)
            node_center = (issue_centers[0] + issue_centers[-1]) / 2
            node_y = node_center - _h(n_lines) / 2
            seg, nh = _sticky(NODE_X, node_y, NODE_W, n_lines, "node", "node", "", "")
            body.append(seg)
            for ic in issue_centers:
                body.append(_edge(NODE_X + NODE_W, node_center, ISS_X, ic, "found on"))
            y = max(y, node_block_top)

        # ---- footer: modules with no findings ----
        if inactive:
            y += 12
            body.append(
                f'<text class="lane" x="{NODE_X}" y="{y}">+ {inactive} other modules reviewed, no findings</text>'
            )
            y += 8

        # ---- rejected lane ----
        if rejections:
            y += 14
            body.append(f'<text class="lane" x="{NODE_X}" y="{y}">Rejected — kept for the record</text>')
            y += 14
            for rej, it in rejections:
                p = rej.proposal
                lines = _wrap("tried: " + p.text, FIX_W) + _wrap("✗ " + rej.reason, FIX_W)
                if rej.explanation:
                    lines += _wrap("why: " + rej.explanation, FIX_W)
                seg, h = _sticky(
                    NODE_X, y, FIX_W, lines, "rej", p.persona, f"i{it}", title="rejected attempt",
                    code=_change_str(p.change),
                )
                body.append(seg)
                y += h + V_GAP

        width = FIX_X + FIX_W + 60
        height = int(y + 30)
        e = html.escape
        svg = (
            f'<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg">'
            f'<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            f'markerHeight="7" orient="auto-start-reverse"><path d="M1 1L9 5L1 9" fill="none" '
            f'stroke="#9d9b91" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'
            f'</marker></defs>'
            f'<text class="h1" x="{NODE_X}" y="34">agentboard — brainstorm map</text>'
            f'<text class="sub" x="{NODE_X}" y="56">{e(goal)}</text>'
            f'<text class="sub" x="{NODE_X}" y="76">grey = system · blue = problem · green = fix</text>'
            f'<text class="sub" x="{NODE_X}" y="94">amber = conflict · red = rejected · iN = the iteration it appeared</text>'
            f'{"".join(body)}</svg>'
        )
        doc = (
            f"<!doctype html><meta charset=utf-8><title>agentboard</title>"
            f"<style>{_CSS}</style>{svg}"
        )
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(doc)
        return self.path