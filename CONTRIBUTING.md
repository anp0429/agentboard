# Contributing to agentboard

Thanks for looking. agentboard is a review gate with one non-negotiable idea:
**a model may propose, but only executed tests decide.** Contributions are
welcome as long as they hold that line.

## The invariants (do not break these)

These are the whole reason the tool is trustworthy. A change that violates one
will not be merged, however useful it looks otherwise.

1. **The verdict is deterministic and model-free.** No LLM sits in the
   pass/fail path. A behavior becomes a `confirmed_gap` only if a proposed test
   compiles, runs, and fails its assertion on the real code.
2. **A garbage test cannot manufacture a finding.** A test that crashes or
   fails to load is classified against the test (`broken_test`), never against
   the code under review. Only a named assertion failure is a gap.
3. **The auditor is advisory.** A second model may flag a likely false
   positive, with a quoted source line. It never changes a verdict. It has been
   wrong (see `BENCHMARK.md`); the gate has the final word, always.
4. **Per-finding isolation.** Every proposed test runs against a clean checkout
   of the repo; no finding sees another's injected test.
5. **The engine knows nothing about GitHub.** CI and MCP are adapters over the
   `--json-out` schema. Keep PR-shaped concepts out of the core.

If your change touches the gate, the classifier, or the cache key, add a test
that pins the behavior. `tests/` has examples of the style, including
falsifier invariants (an impossible test must always read as a real failure).

## Development

```
git clone https://github.com/anp0429/agentboard
cd agentboard
pip install -e ".[dev]"
PYTHONPATH=src python -m pytest tests/ -q
```

The demo runs with no API key and exercises the deterministic gate:

```
agentboard demo
agentboard demo --fixed
```

A live review needs `OPENAI_API_KEY` (reviewer) and, for the auditor,
`ANTHROPIC_API_KEY`. Reviews on real repos run vitest, so Node is required for
those; the Python test suite does not need it.

## Before you open a PR

- `PYTHONPATH=src python -m pytest tests/ -q` passes.
- `ruff check src/ tests/` is clean (advisory today, tightening over time).
- New behavior has a test. New verdict-path behavior has a test that would fail
  without your change.
- If you changed anything the benchmark exercises, say so; `BENCHMARK.md`
  documents the method and the honest misses, and we keep it honest.

## What is especially welcome

- New `RepoProfile` presets or `.agentboard.toml` recipes for repos that need
  scoping (multiple vitest projects, per-package monorepos).
- A pytest gate (the current gate is vitest-only; `PytestVerifier` exists but
  speaks the older protocol).
- Benchmark rows: a merged bugfix PR, run at its parent commit with a neutral
  intent, scored honestly including misses.

## Reporting a security issue

Please use GitHub's private vulnerability reporting rather than a public issue.
