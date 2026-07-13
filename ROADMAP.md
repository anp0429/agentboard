# Roadmap & status

Honest state of the project. What works, what's stubbed, what's next.
The design invariants at the bottom are load-bearing — changes that break them
break the core thesis.

## What works today (verified)

- **Review pipeline runs end to end on a real repo.** `intent (issue) + PR diff ->
  reviewer proposes behaviors + tests -> deterministic gate runs each test ->
  classifies handled / confirmed_gap / broken_test -> board.html`.
- **The gate is deterministic and external.** No LLM decides accept/reject. A
  proposed test only becomes a "gap" if it actually compiles, runs, and fails its
  assertion on the real code. A model that writes a garbage test cannot manufacture
  a gap.
- **It surfaced two real bugs in a live PR** (Supabase MCP #278), each reproduced by
  a failing test the pipeline proposed, then confirmed by hand and reported:
  1. composite foreign keys returned as a cartesian product of columns,
  2. trigger-function classification coupled to an unrelated request flag.
- **Warm-base verifier.** Install/build once per run, reset + inject + run per
  finding — instead of a full reinstall per finding.
- **Diff ingestion.** Reviews the PR's actual changed lines (vs the merge-base),
  fed whole, not a single hand-picked file.

## Known weaknesses (measured or observed)

- **Coverage is not yet fully reliable.** The reviewer reaches the bug-triggering
  case on most runs, not every run. Reliability is being measured (N-run count in
  `examples/run_reliability_5x.py`) before claiming more.
- **Precision layer (gap auditor) under-commits.** It correctly runs against the
  whole change, but still returns `uncertain` on some real gaps instead of
  committing with evidence. It is advisory only and never changes the gate's
  verdict, so this is a triage-quality issue, not a correctness one.
- **Some findings are brittle-assertion false positives** (exact deep-equal on
  rendered output/whole responses). Assertion strength should be exact on
  *collections/counts* (fabrication/over-production) and looser on *rendered field
  values*.

## Next

- [ ] **Finish reliability measurement.** Freeze the config, run N times with memory
  off, count how often the target bug is caught. This is the number that gates
  everything below.
- [ ] **Different-domain test.** Run the same pipeline on a non-DB PR. The general
  invariants (completeness, soundness, uniqueness, count, ordering, idempotence —
  see `ingestion/output_invariants.py`) should surface a real bug there too. If it
  only works on DB PRs, the "generic" claim is not yet earned.
- [ ] **Wire the assertion lint.** `verifiers/assertion_lint.py` exists and is tested
  but is not yet in the loop. Run it on each proposed test; flag or regenerate tests
  that use presence-only matchers on a collection (blind to over-production).
- [ ] **Tighten the gap auditor** so it commits (likely_real / likely_false_positive)
  with evidence instead of defaulting to uncertain.

## Larger, not yet built (the full loop)

- [x] **Fix stage (wired, not yet proven on a real run).** For a confirmed gap,
  `FixAgent` proposes a minimal CodeChange and `TransitionVerifier.verify_transition`
  judges it with the finding's own red test: baseline -> test alone RED (tautology
  guard) -> fix+test GREEN -> no regression. Driver: `examples/run_review_fix.py`.
  Two agents agreeing is never the proof; the gate is. Next: first verified fix on
  a real gap (the composite-FK gap is the natural candidate).
- [ ] **Conflict -> human gate on the board.** When agents disagree on a fix, or a
  fix regresses, surface it as a Conflict and pause for a human decision. Logic is
  stubbed; not wired to the fix stage.
- [ ] **Everything on the board.** Findings, proposed fixes, the gate's verdict on
  each fix, conflicts, human decisions — all projected to `board.html` as the audit
  trail.
- [ ] **Memory (last).** Cache verdicts by (intent + code-hash) to skip settled work.
  Must feed the AGENT (skip re-proposing), never the GATE (always re-runs the real
  test), and must invalidate on code change. Build only after the loop above is
  reliable — memory added earlier hides the variable reliability is measuring.

## Architecture debt

- Two parallel pipelines exist and are not yet reconciled: the review pipeline
  (`review.py`, `finding_verifier.py`) and the older proposal path
  (`state.py` Proposal/Snapshot, `transition_verifier.py`, wired into `loop.py`).
  The fix stage is where they meet — a confirmed review finding becomes the goal for
  a transition-verified fix. Unify the data models (ReviewFinding vs Proposal) and
  update the pytest tests (currently cover the old path only) as part of that work.

## Design invariants (do not break)

1. **The verifier is deterministic and external.** No LLM in the accept/reject path.
2. **Correctness comes from the code, checked fresh** — not from memory, not from a
   second model agreeing, not from a test the proposing agent also authored without
   the red-on-baseline / green-after / no-regression transition check.
3. **A second model detects disagreement; it never votes correctness.** Disagreement
   is surfaced as a Conflict for a human, never averaged away or used to approve.
4. **Per-proposal isolation.** Every change is verified against a clean tree.
