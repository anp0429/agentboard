# agentboard

**An LLM proposes what to test. A deterministic, external gate decides.**
No LLM sits in the accept/reject path — so a model that writes a bad test cannot manufacture a bug.

[![CI](https://github.com/anp0429/agentboard/actions/workflows/ci.yml/badge.svg)](https://github.com/anp0429/agentboard/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/anp0429/agentboard)](LICENSE)

Everything reviews your PR with AI now. The difference here is what counts as a *finding*:
a proposed concern only becomes a finding if a real test, run against the real code in a
clean checkout, actually fails. Opinions don't survive the gate. Reproductions do.

---

## It found a real bug in Supabase's MCP server

Pointed at a live PR in [supabase/mcp](https://github.com/supabase/mcp) (2.8k stars), the
pipeline proposed test cases and the gate turned two of them red by reproducing them
against `main`:

**1. Composite foreign keys returned as a cartesian product.**
An N-column FK was reported as every source column paired with every target column —
N² rows instead of N — fabricating schema relationships that don't exist. Any AI agent
reasoning over the database silently trusts this output. Root-caused to a cross-join in
`pg-meta/tables.sql`; fix with regression test submitted upstream
([supabase/mcp#317](https://github.com/supabase/mcp/pull/317)).

**2. Trigger-returning functions misclassified** as standalone functions when triggers
weren't requested — classification was coupled to an unrelated request flag.

Both confirmed by hand and reported. The gate is what separates these two from the
proposals that were just opinions.

### What a confirmed finding looks like

<!-- TODO(you): paste the REAL board/output from the Supabase run here.
     A fenced code block of the actual classify output — handled / confirmed_gap /
     broken_test — or a screenshot of the board. This is the ten-second payoff
     a visitor needs before they'll read anything else. Do not fabricate;
     use the genuine run artifact. -->

```
$ CLONE=~/code/mcp PR_HEAD=fix/composite-fk PR_BASE=main python examples/run_review.py

[paste real output here]
```

---

## The loop

1. **propose** — an LLM reads the intent (the issue) and the PR diff, and proposes the
   behaviors it thinks should hold — each one as a runnable test.
2. **gate** — each test runs against the real code in a clean checkout. A finding is
   `confirmed_gap` only if its test compiles, runs, and fails. Deterministic. External.
   No LLM.
3. **classify** — `handled` / `confirmed_gap` / `broken_test`, projected to a board.

## Design invariants

1. The verifier is deterministic and external. No LLM in the accept/reject path.
2. Correctness comes from the code, checked fresh — not from memory, not from a second
   model agreeing, not from a test the proposing model authored without a real
   red→green transition.
3. A second model may flag disagreement; it never votes on correctness. Conflicts are
   surfaced for a human, never averaged away.
4. Every proposal is verified against a clean tree.

## Run it

```bash
pip install -e .

# review a PR (needs OPENAI_API_KEY and a local clone of the target repo):
CLONE=/path/to/repo PR_HEAD=HEAD PR_BASE=main python examples/run_review.py
```

The verifier is unit-testable without any API key:

```bash
PYTHONPATH=src python -m pytest tests/ -q
```

## Honest status

- **The gate works and is the reliable part.**
- **Coverage is a sampling problem.** On the composite-FK case, the proposer reached the
  bug-triggering shape in 3 of 5 runs. It reaches the *topic* reliably but samples
  *which* edge case. This is measured, not estimated.
- The advisory precision layer (an auditor) under-commits and is not yet trustworthy.
- A fix stage (propose a fix, verify red→green→no-regression) is built and unit-tested
  but not yet wired end to end.

See [ROADMAP.md](ROADMAP.md) for the full state and next steps.

## Why this exists

AI code review has a trust problem: the same class of model that writes the code also
judges it. When the judge can hallucinate, "findings" are cheap. agentboard's bet is
that the only findings worth surfacing are the ones an external, deterministic check
can reproduce — the LLM is used for what it's good at (proposing hypotheses) and kept
out of what it's bad at (deciding what's true).

## License

See [LICENSE](LICENSE).
