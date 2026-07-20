# agentboard

[![CI](https://github.com/anp0429/agentboard/actions/workflows/ci.yml/badge.svg)](https://github.com/anp0429/agentboard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

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

Install (Python 3.11+):

```
pip install reviewgate
```

Or from source for development: `pip install -e ".[dev]"`.

The demo runs without an API key:

```
agentboard demo
agentboard demo --fixed
```

It gates a small bundled project with one planted bug. Proposals are
pre-generated, so the demo exercises the deterministic part of the pipeline:
the gate finds the gap, and finds it resolved on the fixed variant. A live
review needs `OPENAI_API_KEY` (reviewer) and, for the advisory auditor,
`ANTHROPIC_API_KEY`.

## Usage

Add an `.agentboard.toml` at your repo root, or skip it: the profile is
auto-detected from your lockfile.

```toml
base = "main"
project = "unit"
harness_notes = "Tests already import the framework; reuse existing helpers."
```

Reviewing a repo you don't own? agentboard never needs to write into it.
Run `agentboard init --user` to keep the config in your user dir
(`~/.config/agentboard/repos/<repo>.toml`), or pass `--config path.toml`
explicitly. The review itself also leaves the tree untouched: the board and
all run artifacts default to the system temp dir.

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

To review uncommitted edits instead of committed refs, pass `--worktree`:
the diff becomes working tree vs `--base` (default `HEAD`), the sandbox
executes the same on-disk state it diffed, and `--intent` is required since
uncommitted work has no commit message to derive intent from. This is the
mode a coding agent uses mid-session; it is also the default for the MCP
server below.

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

## Using from a coding agent (MCP)

The gate runs as an MCP server, so a coding agent can gate its own edits
before committing them:

```
pip install "reviewgate[mcp]"
```

For Claude Code:

```
claude mcp add agentboard -- agentboard-mcp
```

For any other MCP client (Cursor, etc.), the server config is:

```json
{ "agentboard": { "command": "agentboard-mcp" } }
```

This exposes one tool, `review`, which returns the same schema_version-1
artifact as `--json-out`. It defaults to `--worktree` mode: the diff is the
working tree's uncommitted edits and the sandbox executes that same on-disk
state, which is the question an agent mid-session is actually asking.
`intent` is required: the calling agent states what its change is meant to
do; nothing is derived from commit messages.

The server is the same thin adapter as the GitHub Action: it builds the
CLI's own arguments and runs the same `review()` path, and a parity test
fails if the two ever accept different flags. Verdicts stay advisory here
too. The tool returns findings with their test source and observed output;
it never raises on a confirmed gap, because deciding what a gap means is
the calling agent's (and ultimately a human's) job.

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

## Local and open-weight models

Model routing is one rule: a model named `claude*` uses Anthropic; every
other name uses an OpenAI-compatible client. Setting `OPENAI_BASE_URL`
points that client anywhere, so the same install runs against a local
server or a hosted open-weight provider with no code change:

```
# Ollama (free, offline; no key needed)
export OPENAI_BASE_URL=http://localhost:11434/v1

# or a hosted provider (OpenRouter shown; needs its key)
export OPENAI_BASE_URL=https://openrouter.ai/api/v1
export OPENAI_API_KEY=sk-or-...
```

```toml
# .agentboard.toml
reviewer_model = "qwen3.6:27b"            # or "moonshotai/kimi-k2.6", etc.
critic_model = "devstral-small-2"         # a different lineage decorrelates
```

The design absorbs weaker proposers safely: a proposal that does not
compile or run is scored against the test (`broken_test`), never against
the code, so a smaller model can only cost recall, not trust. The verdict
path is unchanged because it never contained a model.

Two notes. With `OPENAI_BASE_URL` set, preflight cannot know whether the
endpoint requires auth (Ollama ignores keys; hosted providers need one), so
a missing provider key surfaces as a `[warn]` at propose time rather than a
preflight stop. And the advisory auditor is a `claude*` model by default;
point `--audit-model` at any open model, or `--no-audit` to skip it.

Both lanes are verified working: Kimi K2.6 via OpenRouter and local models
via Ollama, reviewing the same planted bug through the same gate. Measured
rows (catches, wrong-assertion false positives, cost per review) live in
[notes/model-comparison.md](notes/model-comparison.md). OpenRouter
gotchas learned the hard way: its keys start `sk-or-v1-` (an OpenAI
`sk-proj-` key fails as "missing authentication"), and its `/models`
endpoint answers without auth, so verify a key against `/chat/completions`,
not `/models`.

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
- [unjs/ufo#360](https://github.com/unjs/ufo/pull/360): proposed tests caught
  `withBase`/`withoutBase` treating `/` and `?` as base boundaries but not
  `#`, so a fragment directly after the base path broke both operations.
  Reported upstream with a fix and regression tests.

Every finding above was produced by executing tests, and every one is
reproducible by hand.

## Benchmark

[BENCHMARK.md](BENCHMARK.md) measures the harder question: can agentboard
find a real bug in code it has never seen, from an intent that does not name
the bug? It runs at the parent commit of recent merged bugfix PRs, with a
neutral intent, and scores against the fix.

On 12 bugs across 8 repositories, 8 rows produced a real confirmed bug and 4
were exact strict catches. One row, pointed at vueuse before a known fix,
caught the fix's neighborhood and two additional bugs the PR never touched.
Four rows missed and are documented in full. The benchmark also records the
tool's most useful failure: the advisory auditor twice called a real strict
catch a false positive, once citing the buggy line as if it were the
contract, which is exactly why the verdict comes from execution and never
from a model.

## Every run is training data

The gate is a reward function with no model in it, so every finding is a
labeled example: the inputs the proposer saw, the test it wrote, and the
executed verdict. `--dataset` appends one JSONL row per finding to a growing
corpus.

```
agentboard review --target src/parser.ts --intent "handle empty input" --dataset
```

Each row stores the proposal and the executed verdict. The honest label is
`ran` (did the test execute), derived only from the gate's status; the
advisory audit is stored alongside but never overwrites it. Collection is
opt-in and append-only, writing to `~/.agentboard/dataset.jsonl` by default.
Existing `--json-out` artifacts can be backfilled, so a corpus can start from
runs that predate the collector (the benchmark seeds ~260 rows on its own).

The data is not yet used in the loop; it is the substrate for the model work
in [ROADMAP.md](ROADMAP.md) (run an open model against the same benchmark,
then train the proposer on gate outcomes). Rows collected from public repos
are clean to keep; any future company deployment keeps its own data local and
never comingled, by design.

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
- pnpm repos run under the version the repo's own config parses: the
  `packageManager` pin is honored when modern (>= 9), and falls back to a
  pinned `pnpm@9` for ancient or absent pins that cannot run on current Node.
  A single hardcoded pin broke in both directions (old pnpm dies on Node 22;
  pnpm 9 rejects a pnpm 10/11 `pnpm-workspace.yaml` config), so the rule is
  "run under the version this config was written for."
- Monorepos with multiple vitest projects or per-package configs need a
  one-line `.agentboard.toml` naming the `project` or `filter`, the same way
  you would scope CI. Unscoped, the runner may boot an unrelated (e.g.
  browser) project and report an environment failure rather than a verdict.

## Design invariants

1. The verifier is deterministic and external. No LLM sits in the accept or
   reject path.
2. Correctness comes from running the code fresh, not from memory, not from a
   second model agreeing, and not from a test that never made a red to green
   transition.
3. A second model may flag disagreement. It never votes, and conflicts
   surface for a human instead of being averaged away.
4. Every proposal is verified against a clean tree.