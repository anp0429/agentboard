# agentboard

**Review your change by running tests against it — before you push.**

An LLM proposes the behaviors your change should satisfy, each as a real test.
A deterministic gate then runs every test against your actual code in a clean
checkout. A behavior is only reported as a gap if its test compiles, runs, and
fails. **No model sits in the pass/fail decision** — a model that writes a bad
test can't manufacture a bug, and one that misreads your code can't wave a real
one through.

Point it at a branch, a PR, or just your uncommitted-then-committed work:

```
agentboard review --target src/thing.ts --intent "what the change should do"
```

## Why not an LLM reviewer

LLM code reviewers read a diff and give an opinion. When they're wrong, they're
wrong silently — there's nothing to check the opinion against. agentboard's
verdict is an executed test: it passed, it failed, it didn't compile, it timed
out. Four facts, no opinions. If the gate says a behavior fails, you can run the
test yourself and watch it fail. That's the whole difference — **evidence you
can reproduce, not judgment you have to trust.**

## Try it in 30 seconds (no API key)

```
pip install -e .
agentboard demo          # gate a bundled buggy target — a gap appears
agentboard demo --fixed  # same gate, bug fixed — red to green
```

The demo runs the real gate against a small bundled project with one planted
bug. No key needed: the LLM's job (proposing tests) is pre-done, so what you
watch is the part that makes agentboard trustworthy — the gate deciding.

## Reviewing your own change

Drop an `.agentboard.toml` at your repo root once (profile is auto-detected
from your lockfile if you skip it):

```toml
base = "main"
project = "unit"          # if your repo uses vitest projects
harness_notes = "Tests already import the framework; reuse existing helpers."
```

Then, before you push:

```
agentboard review --repo . --target src/parser.ts --intent "handle empty input"
```

Head defaults to your current branch, base to its fork point, tests to the file
next to your target. Intent can come from `--intent`, an `--issue <url>`, or —
if you pass neither — your branch's own commit messages. A fail-fast pre-flight
checks refs, files, and keys before spending a token, so a misconfigured run
tells you in two seconds, not five minutes in.

## Early real-world runs

Pointed at live PRs in repos it had never seen, with no per-repo tuning beyond a
few lines of config:

- **[supabase/mcp#317](https://github.com/supabase/mcp/pull/317)** — proposed
  tests reproduced a bug on `main`: composite foreign keys returned as a
  cartesian product (an N-column key reported N² pairings, inventing
  relationships that don't exist). Reported, fixed, and the edge-case tests the
  tool generated — self-referential, cross-schema, non-primary-unique, multi-FK,
  three-column — went into the PR.
- **[colinhacks/zod#6181](https://github.com/colinhacks/zod/pull/6181)** — ran
  the same suite against both the base and a fix branch. It confirmed the fix
  resolved a crash across 11 shapes, and surfaced one residual case the fix
  didn't cover (a `__proto__` path element, where node creation via
  bracket-assignment sets the prototype instead of an own key). Verified in
  plain JS, then verified the remedy red→green with no regression.

Both findings were checked by running code, and both are reproducible by hand.

## The loop

1. **propose** — an LLM reads the intent and the diff and proposes behaviors,
   each as a test. A second-pass critic hunts gaps in that coverage.
2. **gate** — each test runs against the real code in a clean checkout.
   Deterministic, external, no LLM. Verdict: `handled`, `confirmed_gap`,
   `broken_test`, or `timed_out`.
3. **classify + board** — verdicts render to a review board; a run fingerprint
   lets any two runs be compared with one string.
4. **audit (advisory)** — a *different* model flags possible false positives on
   confirmed gaps. Advisory only; it never changes a verdict.

## Reliability

The gate's determinism is the product, so it's tested as such: the
classification path is checked for byte-identical verdicts across 1,000 runs per
verdict class on every CI push, and a falsifier test (`expect(1).toBe(2)`) must
always classify as a real failure — if an impossible test ever reads as passing,
CI fails. Batched and serial gate paths are asserted verdict-identical by
fingerprint, so speed work can never silently change a result.

## Honest status

- The gate is the reliable part. It runs fast (a batched run gates many
  behaviors in one invocation; unchanged inputs reuse cached proposals for zero
  tokens).
- Coverage is a sampling process: the proposer reaches the *topic* reliably but
  samples *which* edge cases. Run it more than once; different runs find
  overlapping-but-not-identical sets.
- One target file + one tests file per run today. Multi-file changes aren't
  scoped yet.
- The advisory auditor under-commits and isn't yet load-bearing.
- Vitest (pnpm/npm) is the supported harness today.

## Design invariants

1. The verifier is deterministic and external. No LLM in the accept/reject path.
2. Correctness comes from running the code fresh — not from memory, not from a
   second model agreeing, not from a test the proposing model authored without a
   real red→green transition.
3. A second model may flag disagreement; it never votes. Conflicts surface for a
   human, never averaged away.
4. Every proposal is verified against a clean tree.
