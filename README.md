# agentboard

**The LLM proposes tests. A deterministic gate decides.**

Evals for AI agents are flaky because a model sits in the judgment path.
agentboard takes the model out of it: an LLM reads intent (an issue, a PR
description) and proposes the behaviors that should hold — each as a real
test — and a deterministic, external gate runs every test against the actual
code in a clean checkout. A proposal only becomes a *finding* if its test
compiles, runs, and fails. A model that writes a bad test cannot manufacture
a bug. No LLM sits in the accept/reject path.

## What it has actually done

Pointed at [supabase/mcp](https://github.com/supabase/mcp) — the pipeline
participated in a real upstream fix, end to end:

1. **Found the bug.** Proposed test cases against a live PR; the gate turned
   two red by reproducing them against `main`. The headline: composite foreign
   keys returned as a **cartesian product** — an N-column FK reported N² column
   pairings, fabricating relationships that don't exist in the schema. Traced
   to a `pg-meta` SQL cross-join.
2. **Confirmed upstream.** Reported as
   [supabase/mcp#317](https://github.com/supabase/mcp/pull/317) and reproduced
   independently; the review converged on a redesigned output shape — each
   constraint's columns grouped into ordered arrays.
3. **Implemented the redesign** — `unnest(conkey, confkey) WITH ORDINALITY`
   so column pairing is positional by construction, with a regression test
   using deliberately non-alphabetical column order.
4. Validated the fix with the same pipeline. Across 3 review runs, the
   reviewer + critic proposed 15 distinct behaviors - including cases no
   human test covered: FKs referencing unique constraints rather than primary
   keys, empty-set fabrication guards, self-referential composite FKs,
   cross-schema visibility from either side, and output stability across
   repeated calls. The gate executed every one against the branch: 0 gaps.
   Individual runs sample 12–13 of the 15 - coverage is a sampling process,
   so run the reviewer more than once.

The gate is what separates these findings from the proposals that were just
opinions.

## The loop

1. **propose** — an LLM reads the intent (issue / PR description) and the PR
   diff, and proposes the behaviors it thinks should hold, each as a test.
   A second-pass critic (different prompt) hunts gaps in that coverage.
2. **gate** — each test runs against the real code in a clean checkout.
   A finding is `confirmed_gap` only if its test compiles, runs, and fails.
   Deterministic, external, no LLM.
3. **classify** — handled / confirmed_gap / broken_test / already covered,
   projected to a review board.
4. **audit (advisory)** — a *different* model reviews each confirmed gap for
   false positives. Advisory only; it never changes a verdict.

## Honest status

- The gate works and is the reliable part.
- Coverage is a sampling problem: the proposer reaches the *topic* reliably but
  samples *which* edge case (measured at 3-of-5 runs on the original
  composite-FK shape — measured, not estimated). Run it more than once.
- The advisory auditor under-commits and is not yet trustworthy.
- A fix stage (propose a fix, verify red→green→no-regression) is built and
  unit-tested but not yet wired end to end.

See ROADMAP.md for the full state and next steps.

## Run it

```
pip install -e .
# review a PR (needs OPENAI_API_KEY and a local clone of the target repo):
python examples/run_review_composite_fk.py
```

<!-- TODO Phase 1: replace with `pip install agentboard` + zero-key
     `agentboard demo` (bundled target, planted bug, no API key for the gate)
     before any public launch. -->

The verifier logic is unit-testable without any API key:

```
PYTHONPATH=src python -m pytest tests/ -q
```

## Design invariants

1. The verifier is deterministic and external. No LLM in the accept/reject path.
2. Correctness comes from the code, checked fresh — not from memory, not from a
   second model agreeing, not from a test the proposing model also authored
   without a real red→green transition.
3. A second model may flag disagreement; it never votes on correctness.
   Conflicts are surfaced for a human, never averaged away.
4. Every proposal is verified against a clean tree.