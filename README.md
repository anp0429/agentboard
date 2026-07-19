# agentboard

A review gate that verifies changes by executing tests, not by judging
diffs. An LLM proposes edge-case tests from your intent and your change. A
deterministic harness runs each test against the real code in a clean
checkout. A behavior is reported as a gap only if its test compiles, runs,
and fails. No model is involved in the pass or fail decision.

One tool, two moments: the author runs it before pushing and fixes what is
real, so the change arrives cleaner; the maintainer sees it on the pull
request and decides faster. Nobody is replaced at either end.

```
agentboard review --target src/parser.ts --intent "handle empty input"
```

## Why

LLM code reviewers read a diff and return an opinion. There is nothing to
check the opinion against. agentboard's verdict is an executed test: it
passed, it failed, it did not compile, or it timed out. Every reported gap
comes with a test you can run yourself and watch fail.

## Quick start

The demo runs without an API key:

```
pip install -e .   # Python 3.11+
agentboard demo
agentboard demo --fixed
```

It gates a small bundled project with one planted bug. Proposals are
pre-generated, so the demo exercises the deterministic part of the pipeline:
the gate finds the gap, and finds it resolved on the fixed variant.

## Usage

Add an `.agentboard.toml` at your repo root, or skip it: the profile is
auto-detected from your lockfile.

```toml
base = "main"
project = "unit"
harness_notes = "Tests already import the framework; reuse existing helpers."
```

Review a change before pushing:

```
agentboard review --repo . --target src/parser.ts --intent "handle empty input"
```

Defaults: head is your current branch, base is its fork point, and the tests
file is auto-detected by four rules in order: co-located, basename-matched
(with closest-path disambiguation in monorepos), the test file the reviewed
diff itself touches when the diff names exactly one, and, for an explicitly
named target only, the sole test file in the target's own directory. Intent
can come from `--intent`, from `--issue <url>`, or from the branch's commit
messages if you pass neither.

The JS toolchain runs from the target's project root, found by walking up to
the nearest ancestor with a lockfile (else a package.json), so a package
nested inside a larger repository works, and workspace repos still install
at their top level. A preflight validates refs,
files, and keys before any tokens are spent.

## Multi-file reviews

Add files explicitly:

```
agentboard review --target src/parser.ts --also src/lexer.ts --also src/ast.ts:tests/ast.test.ts
```

`--also` is repeatable. Tests are auto-detected per file; pass `file:tests`
to override. Files with no findable tests are skipped with a note.

Or select files by the blast radius of the change:

```
agentboard review --target src/parser.ts --scope all --depth 2
```

`--scope` computes which files a change impacts, using
[code-review-graph](https://github.com/tirth8205/code-review-graph) as the
graph engine. It is an optional dependency: if it is not installed, the
review falls back to the explicit targets. Scopes:

| Scope | Selects |
| --- | --- |
| `changed` | files changed in the diff |
| `test-gaps` | impacted files with no findable tests |
| `all` | every impacted file within `--depth` hops |

Before proposals begin, a per-depth cost curve prints the impacted file count
and test-gap count at each depth. Selections larger than `--max-files`
(default 20) require `--yes`. The graph decides only which files are in
scope; every selected file goes through the same gate as a single-target run.

## Reviewing pull requests in CI

The same gate runs as a GitHub Action and posts its findings as a PR
comment. The comment shows each confirmed gap with the failing test's source
and its observed output, so a reviewer can judge the evidence in seconds;
everything that passed or was already covered is collapsed. It is advisory
by design: gaps never fail the build, nothing is auto-approved, and the
decision stays with a human at both ends.

The workflow lives at `.github/workflows/agentboard-review.yml` with the
comment renderer at `scripts/render_pr_comment.py`. It picks the first
changed source file in the PR, skips quietly when a PR changes no reviewable
code, and skips fork PRs entirely (repository secrets are withheld from
forks, so the model key is unavailable there; that is correct, not a bug).

This repository runs it on itself:
[PR #1](https://github.com/anp0429/agentboard/pull/1) is agentboard
reviewing its own pull request, finding the demo's planted bug through three
functions with an executed failing test for each.

## Machine-readable output

`--json-out <path>` writes the run as a JSON artifact (`schema_version: 1`):
repo, base, head, intent, targets, `env_error`, verdict counts, and one
entry per finding with `behavior`, `status`, `observed`, `source_file`, and
`test_code`. `test_code` and `observed` are null for `skipped_covered`
findings, which never generated a test. Exit codes are advisory: 0 means
the run completed (gaps live in the JSON, never in the exit code), 1 means
it could not run. This artifact is the boundary every integration consumes;
the engine itself knows nothing about pull requests.

## How it works

1. Propose. An LLM reads the intent and the diff and proposes behaviors, each
   as a runnable test. A second-pass critic looks for gaps in that coverage.
2. Gate. Each test runs against the real code in a clean checkout. The gate
   is deterministic and contains no LLM. Verdicts: `handled`,
   `confirmed_gap`, `broken_test`, `timed_out`, `skipped_covered`.
3. Board. Verdicts render to an HTML review board. A run fingerprint lets any
   two runs be compared with one string.
4. Audit. A different model flags possible false positives on confirmed gaps.
   The audit is advisory and never changes a verdict.

## Caching and cost

Proposing tests is the only step that costs tokens, so it is the step that is
cached. Proposals are keyed by intent, diff, and target; re-running an
unchanged review reuses the cached set for zero tokens, and the cache id is
printed when that happens. The gate always re-runs, since executing tests is
cheap and re-verifying is the point.

The gate is batched: one harness invocation gates many behaviors, with a
serial fallback per behavior where batching cannot isolate a result. Batched
and serial paths are asserted verdict-identical by fingerprint. With
`--scope`, the cost curve prints before any spend.

## Results from real repositories

- [supabase/mcp#317](https://github.com/supabase/mcp/pull/317): proposed
  tests reproduced a bug on main. Composite foreign keys in `list_tables`
  returned the cartesian product of column pairs, so an N-column key reported
  N² pairings, most of which do not exist in the schema. The fix and the
  generated regression tests (self-referential, cross-schema,
  non-primary-unique, multi-FK, three-column) are merged into main.
- [colinhacks/zod#6181](https://github.com/colinhacks/zod/pull/6181): ran the
  same proposed suite against the base and the fix branch. The run confirmed
  the fix resolves a crash across 11 shapes and surfaced one residual case
  involving a `__proto__` path element, where bracket assignment sets the
  prototype instead of an own key. The residual was verified in plain
  JavaScript, and a remedy was verified red to green with no regressions.

Both findings were produced by executing tests and both are reproducible by
hand.

## Reliability

The classification path is checked for byte-identical verdicts across 1,000
runs per verdict class on every CI push. A falsifier test (`expect(1).toBe(2)`)
must always classify as a real failure; if an impossible test ever reads as
passing, CI fails. Batched and serial gate paths are asserted
verdict-identical by fingerprint.

## Limitations

- Proposal coverage is a sampling process. The proposer reaches the topic
  reliably but samples which edge cases; repeated runs find overlapping but
  not identical sets.
- Graph scoping depends on the engine's import resolution. Repositories that
  route exports through barrel files (`export * from` chains) can
  under-report their radius. The cost curve shows what the graph sees before
  anything is spent, and `--also` works regardless.
- Impacted files without their own tests are gated in the nearest existing
  test file, which can skew results toward `broken_test` until test
  scaffolding is implemented.
- The audit pass is advisory and not yet load-bearing.
- Vitest (pnpm or npm) is the supported harness.
- Current vitest is the supported target. Very old checkouts (vitest 0.2x
  era) tend to fail at environment preparation for toolchain reasons that
  predate agentboard; the run reports this as an environment failure rather
  than producing verdicts.
- pnpm repos are driven through a pinned modern pnpm (`npx pnpm@9`),
  deliberately ignoring the repo's `packageManager` field: corepack would
  otherwise re-pin to an old pnpm that cannot run on current Node.
## Design invariants

1. The verifier is deterministic and external. No LLM sits in the accept or
   reject path.
2. Correctness comes from running the code fresh, not from memory, not from a
   second model agreeing, and not from a test that never made a red to green
   transition.
3. A second model may flag disagreement. It never votes, and conflicts
   surface for a human instead of being averaged away.
4. Every proposal is verified against a clean tree.