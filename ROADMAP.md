# Roadmap & status

Honest state of the project. What works, what's stubbed, what's next.
The design invariants at the bottom are load-bearing: changes that break them
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
  finding, instead of a full reinstall per finding.
- **Diff ingestion.** Reviews the PR's actual changed lines (vs the merge-base),
  fed whole, not a single hand-picked file.
- **Monorepos at usable speed.** pnpm workspaces gate natively: the
  target's package resolves from the workspace layout, execution is
  scoped to the owning package's own runner binary, and the warm cache
  stores the whole materialized workspace, restored as one clone with no
  re-link. On supabase/supabase (28 projects), first run pays the ~65s
  workspace install; warm runs restore in ~29s and the gate runs in
  seconds. Environment failures triage on first failure and name the fix
  variable instead of retrying blind; `prove.sh` bakes the
  workspace-sized environment in.
- **Two confirmed bugs in a merged monorepo PR, reported upstream.** A
  prove run against a merged supabase/supabase studio PR (two human
  approvals, an AI reviewer, full CI green) produced confirmed gaps in
  the notebook diff derivation behind the assistant-edit approval
  preview; two were hand-verified as real, reproduced unchanged across a
  schema refactor and ten days of development, and filed as
  [#49545](https://github.com/supabase/supabase/issues/49545) and
  [#49546](https://github.com/supabase/supabase/issues/49546) with the
  failing tests attached.
- **First findings from the pytest path.** Pointed at Python repos it had never
  seen, the gate caught `intword`'s 10^36..10^100 formatting gap in humanize
  (never carries to googol) and marshmallow's nested `error_messages` shared by
  reference across field instances via the constructor's shallow merge. Both
  reported upstream with PRs offered; links in the README. Framework-agnostic
  is now a receipt, not an architecture claim.

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

- [x] **`prove` (0.5.0 headline, shipped).** The author-side verb, caveman-simple: an
  agent (or you) wrote code, `edgeverdict prove` tries to break it, and the
  output is one line first: `BROKEN: N failing tests, M attempts` or
  `HELD: M executed attempts, 0 broke it`, evidence below. Zero required
  flags: dirty tree reviews the working tree, clean tree reviews the branch
  against its fork point, targets come from the diff (tests excluded),
  intent derives from commit messages. Same engine, same gate, new door;
  `review` stays as the maintainer/CI verb, unchanged. "Held" never claims
  correctness, only that N executed attempts failed to break it. Spec and
  launch gauntlet in `notes/prove.md`.
- [ ] **The `underspecified` verdict (0.6.0 headline).** When a proposed behavior
  falls where the intent is silent, the gate currently forces it into
  gap-or-handled. Wrong frame: the honest answer is "the intent doesn't say;
  here is what the code actually does, with the executed test that proves it;
  a human should rule." Changes: the reviewer tags each proposal as
  intent-implied or intent-silent, the verifier maps intent-silent + executed
  to `underspecified` instead of `confirmed_gap`, and the board gets its own
  section for them. Gaps accuse; underspecified asks. This is the first
  primitive of the design-session loop below: it teaches the system to say
  "the humans haven't decided this yet."
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
  Build the assertion lint (an earlier roadmap claimed one existed, built and tested; a
  `find` says otherwise — corrected here, receipts rule applies to us too), then wire it in
  the loop) and feed assertion-shape guidance back to the proposer.
- [ ] **Repair as an opt-in verb.** Auto-repair of broken proposals
  currently rides inside prove. Split it: prove executes, reports
  verdicts, and stops; `--repair` opts in. A gate run should never
  spend model tokens the author didn't ask for; this was learned the
  expensive way when repair fired after an environment failure.
- [ ] **Change-level reasoning for multi-file agent PRs.** Today a
  six-file PR is gated as six per-file reviews, which fits how humans
  factor commits but not how coding agents ship: one behavioral change
  spread across files. Next arc: read the PR's intent plus the full
  diff, form change-level behavioral hypotheses, map each to the test
  surface it can be executed against, and gate those. Same verifier,
  different proposer. The evaluation arena is agent-written PRs on a
  large monorepo, where per-file and change-level review genuinely
  diverge.
- [ ] **Auditor calibration, round two.** The false-positive-must-cite-source rule
  helped, but the auditor still inverted on two strict catches by citing the *buggy*
  line as the contract (see `BENCHMARK.md`). It reasons from "what the code does," which
  is the bug when the code is buggy. Next: reason from the intent's implied contract,
  not the current behavior. It stays advisory regardless.

## Monorepos and service-backed tests

- [x] **Mode 2, bring your own environment (shipped).** Opt-in database
  reach for the test phase (`EDGEVERDICT_DB_URL`, install stays
  isolated), a cross-run warm-base cache keyed on dependency state
  (`EDGEVERDICT_WARM_CACHE`), wrapper-aware test title parsing (suites
  wrapping the runner in helpers like `withTestDatabase` now gate), and
  sandbox size overrides for whole-workspace installs
  (`EDGEVERDICT_TMPFS_SIZE`, `EDGEVERDICT_SANDBOX_MEMORY`,
  `EDGEVERDICT_SANDBOX_CPUS`). Proven end
  to end on the supabase/supabase monorepo: pg-meta's live-Postgres
  integration suite gated with real verdicts.
- [x] **Monorepo speed + materialized warm base (shipped).** Test
  execution scoped to the package that owns the resolved test file,
  calling that package's runner binary directly instead of re-resolving
  the workspace per call; serial confirmation re-gates each surviving
  gap in place in the warm tree. The warm cache captures the whole
  materialized workspace after a verified install and restores it as one
  copy-on-write clone: relative symlinks survive, no re-link step, and
  a structural guard discards incomplete cache entries instead of
  serving them. Measured on supabase/supabase: warm base ~74s down to
  ~33s, gate in seconds. Environment preflight refuses ungrated network
  needs in under a second and triages OOM / DNS / disk failures on
  first failure, naming the fix variable; repair is suppressed after
  environment failures.
- [ ] **Mode 3, provision the package's own test services.** Today the
  human stands up the database and sets the URL. Next: detect a
  package-level test-service declaration and run it, the package's own
  compose file so the exact right image is built rather than a stock
  stand-in, pick a free port, wait on the healthcheck, wire the
  resulting URL into the database-reach machinery automatically, and
  tear down after (or keep warm across runs). Detection ladder: a
  package test compose first, a test wrapper script second, nothing
  means no services and the run proceeds as-is. Scope guard: per-package
  test services only, never a repository's full platform stack; the
  point is testing one package, not booting the product.
- [ ] **Python warm cache.** The dependency fingerprint currently reads
  JS lockfiles only, so Python repos never hit the warm cache and pay
  the full install every run. Next: fingerprint `uv.lock`,
  `requirements*.txt`, `poetry.lock`, and `Pipfile.lock`, with the
  pnpm-specific machinery a no-op on Python repos. This is the gate to
  running the same monorepo treatment on Python-first repos.
- [ ] **Jest harness.** Not supported today; a jest suite reports as an
  unsupported environment rather than gating. Mixed repos (jest
  frontend, pytest backend) gate on the Python side. Priced as a known
  gap, not scheduled ahead of the change-level work in Next above.

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

## Where this ends: the whiteboard

An assisted map of every change. For a PR too big to hold in your head, map
renders the change as a board a reviewer can inhabit: touched modules and their
import neighborhood laid out as a graph, blast radius computed from edges rather
than asserted, and every claim pinned to evidence. A confirmed gap hangs on the
node its test breaks; a covered behavior points at the test that covers it. The
skeleton is computed from ASTs and import surfaces; a model may annotate prose
onto structure it cannot alter. The diagram never lies about scope. Review stops
being a diff read top to bottom and becomes a map walked node by node.

The map is the surface a loop runs on: a gap gets confirmed, a fix agent
proposes the repair, prove re-runs the same test, and the node flips from red to
green with both runs pinned to it. The demo's `--fixed` flag is this loop in
miniature today. Agents collaborate the way reviewers should, on the map instead
of in a transcript: proposer, critic, repairer, peer reviewer, each leaving
evidence on nodes, with a human arbitrating the whole change in view. Many
agents, one map, every claim still executed.

The review gate is the first honest citizen of that loop: agents that
design small features out loud, on a board, under the same rule the gate
enforces on tests, lifted one level up. **No argument is admissible without
evidence attached.** "This API shape is simpler" owes two spike diffs.
"This won't scale" owes the executed probe. Everything else is tagged
opinion and waits for a human. The goal was never AI writing 8,000 lines;
it is AI decomposing a problem into small verified iterations, pausing at
every decision a human hasn't made, and self-documenting the whole arc.
The graveyard of "AI software team" tools died of discussion theater:
transcripts of plausible debate with nothing load-bearing in them. The
antidote is the discipline this repo already runs on.

The sequence, one evidence-gated layer at a time:

- **0.6.0, the `underspecified` verdict.** The gate learns the design
  session's core sentence: "the intent doesn't say; here is what the code
  does, with the test that proves it; a human should rule." (Spec above.)
- **0.7.0, evidence-gated arguments.** An Argument is a claim plus an
  attached executed artifact (benchmark, spike diff, probe result) or an
  explicit `opinion` flag. The board renders arguments, not chat.
- **0.8.0, first design session.** Two agents debate one small feature,
  conflicts escalate to a human, and the session's output is a complete
  intent. Dogfooded on this repo's own roadmap. Pass/fail is concrete:
  a decision log you would show another engineer, or theater.
- **0.9.0, the closed loop.** Session produces intent, implementation
  follows, the gate verifies it, the board is the audit trail. Intent
  stops being incomplete, because producing complete intent is what the
  session is for.

The staged pieces, already built or stubbed:

- [x] **Fix stage (wired, not yet proven on a real run).** For a confirmed gap,
  `FixAgent` proposes a minimal CodeChange and `TransitionVerifier.verify_transition`
  judges it with the finding's own red test: baseline -> test alone RED (tautology
  guard) -> fix+test GREEN -> no regression. Driver: `examples/run_review_fix.py`.
  Two agents agreeing is never the proof; the gate is. Next: first verified fix on
  a real gap (the composite-FK gap is the natural candidate).
- [ ] **Conflict -> human gate on the board.** When agents disagree on a fix, or a
  fix regresses, surface it as a Conflict and pause for a human decision. Logic is
  stubbed; not wired to the fix stage. This is the escalation primitive 0.7.0 runs on.
- [ ] **Everything on the board.** Findings, proposed fixes, the gate's verdict on
  each fix, conflicts, human decisions, all projected to `board.html` as the audit
  trail. This is the rendering surface 0.6.0 formalizes.
- [ ] **Memory (last).** Cache verdicts by (intent + code-hash) to skip settled work.
  Must feed the AGENT (skip re-proposing), never the GATE (always re-runs the real
  test), and must invalidate on code change. Build only after the loop above is
  reliable; memory added earlier hides the variable reliability is measuring.

## Architecture debt

- Two parallel pipelines exist and are not yet reconciled: the review pipeline
  (`review.py`, `finding_verifier.py`) and the older proposal path
  (`state.py` Proposal/Snapshot, `transition_verifier.py`, wired into `loop.py`).
  The fix stage is where they meet: a confirmed review finding becomes the goal for
  a transition-verified fix. Unify the data models (ReviewFinding vs Proposal) and
  update the pytest tests (currently cover the old path only) as part of that work.

## Design invariants (do not break)

1. **The verifier is deterministic and external.** No LLM in the accept/reject path.
2. **Correctness comes from the code, checked fresh**, not from memory, not from a
   second model agreeing, not from a test the proposing agent also authored without
   the red-on-baseline / green-after / no-regression transition check.
3. **A second model detects disagreement; it never votes correctness.** Disagreement
   is surfaced as a Conflict for a human, never averaged away or used to approve.
4. **Per-proposal isolation.** Every change is verified against a clean tree.