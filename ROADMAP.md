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

- **Coverage is a sampling process, now measured.** The reviewer reaches the
  topic reliably but samples which edge cases, so runs find overlapping-but-not-
  identical sets. This is measured by the cross-repo benchmark (`BENCHMARK.md`),
  not asserted: 8 of 12 rows caught a real bug, 4 exact strict catches, with the
  misses published. The gate's verdict, separately, is deterministic (byte-
  identical fingerprints across runs and across days).
- **Precision layer (gap auditor) under-commits.** It correctly runs against the
  whole change, but still returns `uncertain` on some real gaps instead of
  committing with evidence. It is advisory only and never changes the gate's
  verdict, so this is a triage-quality issue, not a correctness one.
- **Some findings are brittle-assertion false positives** (exact deep-equal on
  rendered output/whole responses). Assertion strength should be exact on
  *collections/counts* (fabrication/over-production) and looser on *rendered field
  values*.

## Benchmark (done)

- [x] **Cross-repo benchmark with published misses.** 12 bugs, 8 repos, neutral
  intents, parent-commit checkouts, scored against the real fix. 8 rows caught a
  real bug, 4 exact strict catches, 0 environment failures. See `BENCHMARK.md`.
  This replaces the single-repo reliability count as the headline evidence.
- [x] **Different-domain proof.** The benchmark spans URL utils, object merge, path
  handling, a reactive store, an HTTP client, devtools sorting, and schema parsing.
  The generic claim is earned: no row is database-shaped.

## Next

- [x] **Dataset collector (Phase B).** `--dataset` appends one JSONL row per finding:
  the proposal, the executed verdict, the advisory audit. The label is `ran` (did the
  test execute), a model-free fact the audit never overwrites. Backfills from existing
  `--json-out` artifacts, so the benchmark seeded ~260 rows on day one. Changes no
  verdict logic. See `dataset.py` and `bench/report.py`. This is the substrate for the
  model work below.
- [ ] **Verified fix stage.** For a confirmed gap, propose a fix and prove it the same
  way the gap was proven: `TransitionVerifier` shows red -> green -> no regression, no
  model in the fix's verdict. Pieces exist (`fix_with_test_agent`, `TransitionVerifier`,
  the `fix_status` fields, the board rendering); this is wiring plus an opt-in `--fix`
  flag, author-side first, collapsed or off in PR comments on repos you do not own. A
  bad candidate fix must render as rejected, never as a diff to apply.
- [ ] **Assertion quality.** The benchmark's core finding: same bug class, one proposal
  asserts the right remedy (catch) and one the wrong remedy (miss + false positive).
  Wire the assertion lint (`verifiers/assertion_lint.py`, built and tested, not yet in
  the loop) and feed assertion-shape guidance back to the proposer.
- [ ] **Auditor calibration, round two.** The false-positive-must-cite-source rule
  helped, but the auditor still inverted on two strict catches by citing the *buggy*
  line as the contract (see `BENCHMARK.md`). It reasons from "what the code does," which
  is the bug when the code is buggy. Next: reason from the intent's implied contract,
  not the current behavior. It stays advisory regardless.

## Model work (the reward-function thesis)

The gate is a reward function with no model in it. Every review emits an
execution-grounded label for free. That is reinforcement learning from verifiable
rewards, applied to code review. The ladder, cheapest rung first:

- [ ] **Phase A: local-model swap.** The reviewer model is already a config value and
  the client honors `OPENAI_BASE_URL`, so an open coding model behind an OpenAI-compatible
  server plugs in with no code change. Run the best open model against this same benchmark
  and publish "frontier vs open, before the gate and after the gate." If the after-gate gap
  is small, that is the headline: the gate makes weak models trustworthy.
- [ ] **Phase C: train the proposer on gate outcomes.** Rejection-sample or LoRA-tune the
  open proposer to maximize proposals that run and reveal, penalizing broken tests and
  audited false positives. Measure on held-out benchmark rows never trained on. Starts only
  when the benchmark is frozen as the eval set and the dataset crosses ~2,000 rows. Skill in
  weights (how to propose an executable falsifying test), not repo facts.
- [ ] **Phase D: company-adapted review.** Local model inside the firewall, learning from
  two signals a company already produces: the gate's verdicts, and its senior engineers'
  review comments as persona-tagged taste. The world's models learn on everyone's data; this
  one learns on yours, and nothing leaves the building. Human comments train the proposer's
  taste, never the verdict. The gate stays model-free forever. Pilots only where authorized.

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
