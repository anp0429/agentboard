"""Proposal cache — pay for sampling only when the inputs changed.

The pipeline has two very different halves. The gate is deterministic and
runs in seconds; the propose side (reviewer + critic) is sampled, costs real
tokens, and takes minutes. When NOTHING it reads has changed — same intent,
same diff, same source, same tests, same models, same harness notes — a
rerun buys nothing but a different sample of the same coverage space. This
cache makes that rerun free: verdicts are always re-executed live; only the
PROPOSALS are reused.

Honesty rules, because coverage is a sampling process by design:
  * The cache NEVER silently prevents resampling — every hit prints its key,
    and `fresh=True` (or AGENTBOARD_FRESH=1) forces a new sample and
    overwrites the entry. The human decides when to pay for new coverage.
  * Only proposal-side fields are stored (behavior, axis, coverage read,
    test code). Verdict fields (status, observed, audit) are per-gate-run
    facts and are never cached — a loaded finding always starts "pending".
  * The key covers every byte that feeds either prompt. Change one character
    of the diff and the key changes; a stale hit is structurally impossible.
"""

from __future__ import annotations

import hashlib
import json
import os

from .review import ReviewFinding

_CACHE_VERSION = "1"  # bump on serialization/prompt-shape changes

_FIELDS = (
    "behavior",
    "axis",
    "covered_by_existing",
    "coverage_note",
    "test_path",
    "test_code",
)


def cache_dir() -> str:
    return os.environ.get(
        "AGENTBOARD_CACHE_DIR",
        os.path.join(os.path.expanduser("~"), ".agentboard", "proposal_cache"),
    )


def proposal_key(
    *,
    intent: str,
    change: str,
    source: str,
    tests: str,
    reviewer_model: str,
    critic_model: str,
    harness_notes: str,
    run_critic: bool,
    axis: str = "default",
) -> str:
    """sha256 over every input either prompt reads. Order fixed, versioned."""
    h = hashlib.sha256()
    for part in (
        _CACHE_VERSION,
        intent,
        change,
        source,
        tests,
        reviewer_model,
        critic_model,
        harness_notes,
        "critic" if run_critic else "no-critic",
    ) + ((f"axis={axis}",) if (axis or "default") != "default" else ()):
        # axis biases which cases get proposed, so a non-default axis must key
        # the cache separately. default appends NOTHING, so every pre-axis key
        # (and the banked fingerprints tied to them) stays byte-identical.
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def load(key: str) -> list[ReviewFinding] | None:
    """Return cached proposals, or None on miss/corruption (never raises)."""
    path = os.path.join(cache_dir(), f"{key}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        out = []
        for row in data["findings"]:
            f = ReviewFinding(behavior=str(row["behavior"]))
            for field in _FIELDS[1:]:
                if field in row:
                    setattr(f, field, row[field])
            out.append(f)
        return out
    except Exception:  # noqa: BLE001 — any problem is just a miss
        return None


def save(key: str, findings: list[ReviewFinding]) -> None:
    os.makedirs(cache_dir(), exist_ok=True)
    rows = [{field: getattr(f, field) for field in _FIELDS} for f in findings]
    path = os.path.join(cache_dir(), f"{key}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"version": _CACHE_VERSION, "findings": rows}, fh, indent=1)


def propose_or_cached(
    reviewer,
    critic,
    *,
    intent: str,
    change: str,
    source: str,
    tests: str,
    fresh: bool = False,
) -> list[ReviewFinding]:
    """The propose phase with caching: reviewer (+ critic if given), reused
    when every input byte matches a prior run. Prints what it did — a silent
    cache would hide the sampling decision from the human."""
    fresh = fresh or os.environ.get("AGENTBOARD_FRESH") == "1"
    key = proposal_key(
        intent=intent,
        change=change,
        source=source,
        tests=tests,
        reviewer_model=getattr(reviewer, "model", ""),
        critic_model=getattr(critic, "model", "") if critic else "",
        harness_notes=getattr(reviewer, "harness_notes", "") or "",
        run_critic=critic is not None,
        axis=getattr(reviewer, "axis", "default"),
    )
    if not fresh:
        cached = load(key)
        if cached is not None:
            print(
                f"  proposals: cache hit ({key[:12]}) — {len(cached)} "
                f"behavior(s), 0 tokens. AGENTBOARD_FRESH=1 to resample."
            )
            return cached

    findings = reviewer.review(intent, change=change)
    if critic is not None:
        findings = findings + critic.critique(intent, source, tests, findings)
    save(key, findings)
    print(f"  proposals: sampled fresh, cached as {key[:12]}")
    return findings
