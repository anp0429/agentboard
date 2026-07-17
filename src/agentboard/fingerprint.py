"""Verdict fingerprint — one string that makes two runs comparable.

"The gate is deterministic" is a claim; this module makes it checkable. A
fingerprint is a sha256 over ONLY the verdict-relevant fields of a review:

    per finding:  sha256(behavior), axis, status

Everything nondeterministic is excluded by construction — timestamps,
durations, temp paths, raw stdout/stderr, and the ORDER findings were
classified in (findings are sorted by behavior hash before hashing, so a
parallelized gate produces the same fingerprint as a serial one).

Deliberately excluded, and why:
  - `observed`: raw messages carry machine paths and millisecond timings.
    The status already encodes the verdict class; the message is evidence
    for humans, not part of the verdict.
  - `audit*` fields: the auditor is an LLM and ADVISORY by design. Letting
    it into the fingerprint would put a model back inside the determinism
    claim.
  - test_code: the proposal side is sampled, not deterministic. The
    fingerprint answers "same suite, same target -> same verdicts?", so it
    hashes what was asked (behavior) and what was ruled (status), not how
    the question was phrased in code.

Two runs over the same findings and target MUST produce the same
fingerprint. If they don't, the gate has a determinism bug — which is
exactly what tests/test_determinism.py exists to catch.
"""

from __future__ import annotations

import hashlib
import json

from .review import ReviewRun


def _behavior_key(behavior: str) -> str:
    return hashlib.sha256(behavior.strip().encode("utf-8")).hexdigest()[:16]


def verdict_fingerprint(run: ReviewRun) -> str:
    """Canonical sha256 of the review's verdicts. Order-insensitive."""
    rows = sorted(
        (
            _behavior_key(f.behavior),
            f.axis,
            f.status,
        )
        for f in run.findings
    )
    canon = json.dumps(rows, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def verdict_summary(run: ReviewRun) -> str:
    """One line for the end of a run: counts + fingerprint prefix."""
    counts: dict[str, int] = {}
    for f in run.findings:
        counts[f.status] = counts.get(f.status, 0) + 1
    parts = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    return f"verdicts: {parts}  fingerprint: {verdict_fingerprint(run)[:16]}"
