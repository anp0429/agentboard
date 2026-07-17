"""The review record and its board.

A reviewer agent doesn't just accept/reject — it produces an audit trail:
for each behavior the intent implies, was it already covered? if not, a test
was written and run; what did it show? This module holds that record and renders
it as a single self-contained HTML page so a human can read exactly what the
loop did and why — the documented review IS the product, not just the fix.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Literal

Axis = Literal["correctness", "consistency"]
Status = Literal[
    "skipped_covered", "handled", "confirmed_gap", "broken_test", "timed_out", "pending"
]


@dataclass
class ReviewFinding:
    behavior: str                      # the intended behavior, derived from the intent
    axis: Axis = "correctness"
    covered_by_existing: bool = False  # agent's read: already tested?
    coverage_note: str = ""            # why it thinks so (which test, or "none found")
    test_path: str | None = None       # the test it wrote (if uncovered)
    test_code: str | None = None
    status: Status = "pending"         # set by the FindingVerifier after running
    observed: str = ""                 # what the run showed (assertion msg / output / error)
    # advisory precision layer (GapAuditor) — NEVER changes status, triage only
    audit: str = ""                    # "likely_real" | "likely_false_positive" | "uncertain"
    audit_reason: str = ""
    audit_evidence: str = ""
    # fix stage (TransitionVerifier is the judge — red->green->no-regression)
    fix_status: str = ""               # "" | "fix_verified" | "fix_rejected" | "fix_not_attempted"
    fix_note: str = ""                 # verifier's reason (or agent's failure to propose)
    fix_change: str = ""               # human-readable summary of the applied edit


@dataclass
class ReviewRun:
    intent: str
    target: str
    findings: list[ReviewFinding] = field(default_factory=list)
    # set when the environment itself failed (install/build/smoke/fidelity):
    # a run-level fact, rendered ONCE as a banner — never as per-finding noise
    env_error: str = ""

    @property
    def gaps(self) -> list[ReviewFinding]:
        return [f for f in self.findings if f.status == "confirmed_gap"]


_STATUS_LABEL = {
    "confirmed_gap": ("gap", "#a32d2d", "#f09595"),
    "handled": ("handled", "#1f7a4d", "#5dcaa5"),
    "skipped_covered": ("already covered", "#76756e", "#c9c8c2"),
    "broken_test": ("test didn't run", "#8a6d1b", "#e0c060"),
    "timed_out": ("timed out — human call", "#5a5a8f", "#a9a9d6"),
    "pending": ("not yet run", "#76756e", "#c9c8c2"),
}

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin:0; font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
       background:#f6f6f4; color:#26251f; }
@media (prefers-color-scheme: dark){ body{ background:#1c1c1a; color:#e8e6df; } }
header { padding:20px 24px; border-bottom:1px solid #d9d8d2; }
h1 { font-size:18px; font-weight:600; margin:0; }
.sub { color:#76756e; font-size:13px; margin-top:6px; white-space:pre-wrap; max-width:900px; }
.summary { padding:14px 24px; font-size:14px; border-bottom:1px solid #d9d8d2; }
.envfail { padding:14px 24px; font-size:14px; font-weight:600; color:#fff;
           background:#a32d2d; }
.envfail .detail { font-weight:400; font-size:13px; margin-top:4px;
                   font-family:ui-monospace,monospace; }
.summary b { font-weight:600; }
.wrap { padding:20px 24px; max-width:920px; }
.card { border-radius:12px; padding:14px 16px; margin-bottom:12px; border:1px solid #d9d8d2;
        border-left-width:4px; background:#fff; }
@media (prefers-color-scheme: dark){ .card{ background:#26261f; border-color:#3a3a36; } }
.behavior { font-weight:600; font-size:15px; }
.meta { font-size:12px; color:#76756e; margin-top:3px; }
.badge { display:inline-block; font-size:11px; font-weight:600; padding:2px 9px;
         border-radius:20px; color:#fff; margin-left:8px; vertical-align:middle; }
.observed { font-size:13px; margin-top:8px; }
details { margin-top:8px; }
summary { font-size:12px; color:#5a76a8; cursor:pointer; }
pre { background:#f0efe9; border-radius:8px; padding:8px 10px; margin:6px 0 0;
      font:12px/1.45 ui-monospace,SFMono-Regular,monospace; white-space:pre-wrap;
      word-break:break-word; }
@media (prefers-color-scheme: dark){ pre{ background:#1f1f1b; } }
.verify { margin-top:10px; font-size:12px; font-weight:600; color:#a32d2d; }
@media (prefers-color-scheme: dark){ .verify{ color:#f09595; } }
pre.gap-test { border:1px solid #e0b4b4; max-height:340px; overflow:auto; }
@media (prefers-color-scheme: dark){ pre.gap-test{ border-color:#5a2d2d; } }
.audit { margin-top:10px; padding:8px 10px; border-radius:8px; border:1px solid #ccc;
         border-left-width:4px; font-size:12px; background:#faf8f2; }
@media (prefers-color-scheme: dark){ .audit{ background:#232019; } }
.audit .ev { color:#76756e; margin-top:4px; font-family:ui-monospace,monospace; }
"""


def render_review_html(run: ReviewRun, path: str) -> str:
    gaps = sum(1 for f in run.findings if f.status == "confirmed_gap")
    covered = sum(1 for f in run.findings if f.status in ("skipped_covered", "handled"))
    broken = sum(1 for f in run.findings if f.status == "broken_test")
    timed = sum(1 for f in run.findings if f.status == "timed_out")

    cards = []
    for f in run.findings:
        label, text_c, border_c = _STATUS_LABEL.get(f.status, _STATUS_LABEL["pending"])
        badge = f'<span class="badge" style="background:{text_c}">{label}</span>'
        axis = f'<span class="meta">axis: {f.axis}</span>'
        cov = (f"already covered — {html.escape(f.coverage_note)}"
               if f.covered_by_existing else
               "not covered by existing tests" + (f" — {html.escape(f.coverage_note)}" if f.coverage_note else ""))
        extra = ""
        if f.observed:
            extra += f'<div class="observed"><b>Observed:</b> {html.escape(f.observed)}</div>'
        if f.fix_status:
            fx_label, fx_color = {
                "fix_verified": ("FIX VERIFIED (red->green, no regression)", "#1f7a4d"),
                "fix_rejected": ("fix rejected by gate", "#8a6d1b"),
                "fix_not_attempted": ("no fix proposed", "#76756e"),
            }.get(f.fix_status, (f.fix_status, "#76756e"))
            extra += (
                f'<div class="audit" style="border-color:{fx_color}">'
                f'<b style="color:{fx_color}">{fx_label}</b>'
                + (f' — {html.escape(f.fix_note)}' if f.fix_note else '')
                + (f'<div class="ev">{html.escape(f.fix_change)}</div>' if f.fix_change else '')
                + '</div>'
            )
        if f.test_code:
            if f.status == "confirmed_gap":
                # advisory precision flag from the GapAuditor (does NOT change status)
                if f.audit:
                    a_label = {"likely_real": ("likely REAL", "#a32d2d"),
                               "likely_false_positive": ("likely FALSE POSITIVE", "#8a6d1b"),
                               "uncertain": ("uncertain", "#76756e")}.get(f.audit, ("", "#76756e"))
                    extra += (
                        f'<div class="audit" style="border-color:{a_label[1]}">'
                        f'<b style="color:{a_label[1]}">Auditor: {a_label[0]}</b>'
                        + (f' — {html.escape(f.audit_reason)}' if f.audit_reason else '')
                        + (f'<div class="ev">evidence: {html.escape(f.audit_evidence)}</div>' if f.audit_evidence else '')
                        + '</div>'
                    )
                extra += (
                    '<div class="verify">Verify this before trusting it: is the '
                    'assertion correct, or did the agent assert the wrong thing?</div>'
                    f'<pre class="gap-test">{html.escape(f.test_code)}</pre>'
                )
            else:
                extra += (f'<details><summary>test the agent wrote</summary>'
                          f'<pre>{html.escape(f.test_code)}</pre></details>')
        cards.append(
            f'<div class="card" style="border-left-color:{border_c}">'
            f'<div class="behavior">{html.escape(f.behavior)}{badge}</div>'
            f'<div class="meta">{cov}</div>{axis}{extra}</div>'
        )

    banner = ""
    if run.env_error:
        banner = (
            '<div class="envfail">Environment preparation failed — '
            'no verdicts were issued.'
            f'<div class="detail">{html.escape(run.env_error)}</div></div>'
        )
    doc = (
        f"<!doctype html><meta charset=utf-8><title>agentboard review</title>"
        f"<style>{_CSS}</style>"
        f'<header><h1>agentboard — review</h1>'
        f'<div class="sub">Target: {html.escape(run.target)}\n\nIntent: {html.escape(run.intent[:400])}</div></header>'
        f'{banner}'
        f'<div class="summary"><b>{gaps}</b> confirmed gap(s) · '
        f'<b>{covered}</b> covered/handled · <b>{broken}</b> test didn\'t run · '
        f'<b>{timed}</b> timed out · '
        f'<b>{len(run.findings)}</b> behaviors reviewed</div>'
        f'<div class="wrap">{"".join(cards)}</div>'
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path