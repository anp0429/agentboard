"""Dataset collector: every review is training data.

The gate is a reward function with no model in it, so every finding is a
labeled example: the inputs the proposer saw, the test it wrote, and the
executed verdict. This appends one JSONL row per finding to a growing corpus.
It changes NO verdict logic; it is a pure side effect of a run.

Why this exists: the propose side is the model's job and the gate side is a
verifiable, execution-grounded label. That pairing is exactly the signal
reinforcement-learning-from-verifiable-rewards needs, and it is produced for
free on every run. Collecting it from day one means the corpus is already
large when the model work (roadmap Phase A/C) begins.

Design rules, each load-bearing:
  * Facts only, no reward shaping. Store the executed status and the advisory
    audit; let the trainer decide the reward later. The one honest,
    model-free label is `ran` (did the test execute): true for handled and
    confirmed_gap, false for broken_test, null for skipped_covered/timed_out.
  * Append-only JSONL, one row per finding, so the corpus grows across runs
    and streams line by line without loading the whole file.
  * Explicit, never silent. Writing is opt-in and prints where it wrote,
    because quiet data collection is exactly what this project refuses to do.
  * The advisory audit is stored but never used to relabel `ran`. A model's
    opinion does not overwrite an executed fact, same rule as everywhere else.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .review import ReviewFinding, ReviewRun

SCHEMA_VERSION = 1


def default_path() -> str:
    return os.environ.get(
        "AGENTBOARD_DATASET",
        os.path.join(os.path.expanduser("~"), ".agentboard", "dataset.jsonl"),
    )


def _ran(status: str) -> bool | None:
    """The verifiable, model-free label: did the proposed test execute?

    true  -> it ran (handled = ran+passed, confirmed_gap = ran+failed).
    false -> it did not run (broken_test: compile/load/crash).
    null  -> not applicable (skipped_covered: no test) or ambiguous
             (timed_out: started, never finished).
    """
    if status in ("handled", "confirmed_gap"):
        return True
    if status == "broken_test":
        return False
    return None


def finding_row(
    finding: ReviewFinding,
    *,
    repo: str,
    base: str,
    head: str,
    target: str,
    tests: str,
    intent: str,
    axis: str,
    reviewer_model: str,
    critic_model: str,
    ts: str,
) -> dict:
    """One finding -> one training row (PURE). Facts the proposer saw plus the
    executed verdict. The reward is left to the trainer; only the raw executed
    label `ran` is asserted here."""
    return {
        "schema_version": SCHEMA_VERSION,
        "ts": ts,
        "repo": repo,
        "base": base,
        "head": head,
        "target": finding.source_file or target,
        "tests": tests,
        "intent": intent,
        "axis": axis,
        "reviewer_model": reviewer_model,
        "critic_model": critic_model,
        # the proposal (model output)
        "behavior": finding.behavior,
        "proposed_axis": finding.axis,
        "covered_by_existing": finding.covered_by_existing,
        "proposed_test": finding.test_code,
        # the executed verdict (gate output — the label)
        "status": finding.status,
        "ran": _ran(finding.status),
        "observed": finding.observed or None,
        # the advisory triage (model opinion — never overwrites `ran`)
        "audit": finding.audit or None,
        "audit_reason": finding.audit_reason or None,
        "audit_evidence": finding.audit_evidence or None,
    }


def append_run(
    run: ReviewRun,
    *,
    path: str,
    repo: str,
    base: str,
    head: str,
    pairs: list[tuple[str, str]],
    intent: str,
    axis: str,
    reviewer_model: str,
    critic_model: str,
) -> int:
    """Append one JSONL row per finding in the run. Returns the row count.
    Never raises into the caller: collection must never break a review."""
    ts = datetime.now(timezone.utc).isoformat()
    tests_for = {t: ts_ for t, ts_ in pairs}
    default_tests = pairs[0][1] if pairs else ""
    rows = [
        finding_row(
            f,
            repo=repo, base=base, head=head,
            target=f.source_file or (pairs[0][0] if pairs else ""),
            tests=tests_for.get(f.source_file, default_tests),
            intent=intent, axis=axis,
            reviewer_model=reviewer_model, critic_model=critic_model,
            ts=ts,
        )
        for f in run.findings
    ]
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"  [warn] dataset: could not write {path}: {e}")
        return 0
    print(f"  dataset: appended {len(rows)} row(s) to {path}")
    return len(rows)


def backfill_from_json_out(json_out_path: str, dataset_path: str) -> int:
    """Reconstruct dataset rows from a schema-v1 --json-out artifact.

    The json-out files already carry intent, targets, base/head, and every
    finding with its executed status, test, and audit — so a corpus can be
    seeded from runs that happened before this collector existed (the v1/v2
    benchmark, for one). reviewer/critic model are not in the artifact, so
    they are recorded as unknown rather than guessed."""
    with open(json_out_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    ts = datetime.now(timezone.utc).isoformat()
    targets = doc.get("targets", [])
    tests_for = {t.get("target"): t.get("tests", "") for t in targets}
    default_tests = targets[0].get("tests", "") if targets else ""
    rows = []
    for f in doc.get("findings", []):
        status = f.get("status", "")
        src = f.get("source_file", "")
        rows.append({
            "schema_version": SCHEMA_VERSION,
            "ts": ts,
            "repo": doc.get("repo", ""),
            "base": doc.get("base", ""),
            "head": doc.get("head", ""),
            "target": src or (targets[0].get("target") if targets else ""),
            "tests": tests_for.get(src, default_tests),
            "intent": doc.get("intent", ""),
            "axis": "unknown",
            "reviewer_model": "unknown",
            "critic_model": "unknown",
            "behavior": f.get("behavior", ""),
            "proposed_axis": None,
            "covered_by_existing": status == "skipped_covered",
            "proposed_test": f.get("test_code"),
            "status": status,
            "ran": _ran(status),
            "observed": f.get("observed") or None,
            "audit": f.get("audit"),
            "audit_reason": f.get("audit_reason"),
            "audit_evidence": f.get("audit_evidence"),
        })
    os.makedirs(os.path.dirname(os.path.abspath(dataset_path)), exist_ok=True)
    with open(dataset_path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)
