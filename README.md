# agentboard

An LLM proposes what to test; a **deterministic, external gate** decides whether
each concern is real by reproducing it against the actual code. No LLM sits in the
accept/reject path.

The point is not "an AI reviews your PR" — everything does that now. The point is
that a proposed finding only becomes a *finding* if a real test, run against the
real code, actually fails. A model that writes a bad test cannot manufacture a bug.

## What it has actually done

Pointed at a live pull request in the Supabase MCP server, agentboard's pipeline
proposed test cases and the gate turned two of them red by reproducing them:

- **Composite foreign keys returned as a cartesian product.** A multi-column FK was
  reported as every source-column paired with every target-column (N² rows instead
  of N), fabricating relationships that don't exist. Traced to a `pg-meta` SQL
  cross-join, reproduced on `main`, fixed, and submitted upstream.
- **Trigger-returning functions misclassified** as standalone functions when
  triggers weren't requested — the classification was coupled to an unrelated
  request flag.

Both were confirmed by hand and reported. The gate is what separates these from the
proposals that were just opinions.

## The loop

1. **propose** — an LLM reads the intent (issue) and the PR diff, and proposes the
   behaviors it thinks should hold, each as a test.
2. **gate** — each test is run against the real code in a clean checkout. A finding
   is `confirmed_gap` only if its test compiles, runs, and fails. Deterministic,
   external, no LLM.
3. **classify** — handled / confirmed_gap / broken_test, projected to a board.

## Honest status

- The gate works and is the reliable part.
- Coverage is a sampling problem: on the composite-FK case, the proposer reached the
  bug-triggering shape in **3 of 5 runs**. It reaches the *topic* reliably but
  samples *which* edge case. This is measured, not estimated.
- The advisory precision layer (an auditor) under-commits and is not yet trustworthy.
- A fix stage (propose a fix, verify red→green→no-regression) is built and
  unit-tested but not yet wired end to end.

See ROADMAP.md for the full state and next steps.

## Run it

```bash
pip install -e .
# review a PR (needs OPENAI_API_KEY and a local clone of the target repo):
CLONE=/path/to/repo PR_HEAD=HEAD PR_BASE=main python examples/run_review.py
```

The verifier logic is unit-testable without any API key:

```bash
PYTHONPATH=src python -m pytest tests/ -q
```

## Design invariants

1. The verifier is deterministic and external. No LLM in the accept/reject path.
2. Correctness comes from the code, checked fresh — not from memory, not from a
   second model agreeing, not from a test the proposing model also authored without
   a real red→green transition.
3. A second model may flag disagreement; it never votes on correctness. Conflicts
   are surfaced for a human, never averaged away.
4. Every proposal is verified against a clean tree.
