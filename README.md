# edgeverdict

[![CI](https://github.com/anp0429/edgeverdict/actions/workflows/ci.yml/badge.svg)](https://github.com/anp0429/edgeverdict/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

<!-- README_V11_MONOREPO_SPEED -->

A review gate that proposes edge cases and executes them before
judging. A model proposes the tests; the verdict comes from running
them.

1. A coding agent changes a function.
2. `edgeverdict prove` generates change-specific edge-case tests and
   executes them against the real code, in a sandbox.
3. One test fails. The gap is reported with the failing test itself,
   so you can read exactly what broke and run it again.
4. The agent fixes the code.
5. The next run returns `HELD`.

<!-- demo GIF goes here: record the keyless `edgeverdict demo` /
     `demo --fixed` pair with vhs so the tape lives in the repo and
     the GIF regenerates when CLI output changes -->

That loop runs in about twenty seconds with no API key:

```
pip install edgeverdict
edgeverdict demo
edgeverdict demo --fixed
```

The demo needs no Docker, no image, and no keys: its target ships
inside this package and is trusted by construction, so it runs with
the local backend. Reviewing any real repository defaults to the
sandbox described in Sandboxed execution below.

It does not grade diffs and does not run your existing suite:
the edge cases come from an LLM reading your intent and your change, and
a deterministic harness runs each proposed test against the real code in
a clean checkout. A behavior is reported as a gap only if its test compiles, runs,
and fails. No model is involved in the pass or fail decision.

Two verbs, one engine. `prove` is the author-side verb: an agent (or you)
changes code, prove tries to break it before it merges. Zero flags — it
reads your dirty tree or your branch, picks targets from the diff, derives
intent from your commits, and answers **BROKEN** (N confirmed gaps,
each a failing test you can run yourself), **HELD** (N executed attempts, none broke it —
quantified absence, never certification), or **STOPPED** (what happened
instead, cause first). Exit codes 0/2/1, built for agent loops: write,
prove, fix the failing test, prove again. `review` is the maintainer-side
verb for pull requests and CI: same engine, always-advisory exit codes.
Nobody is replaced at either end.

```
edgeverdict prove
edgeverdict review --target src/parser.ts --intent "handle empty input"
```

## Why

LLM code reviewers read a diff and return an opinion. There is nothing to
check the opinion against. edgeverdict's verdict is an executed test: it
passed, it failed, it did not compile, or it timed out. Every reported gap
comes with a test you can run yourself and watch fail.

## Quick start

Install (Python 3.11+):

```
pip install edgeverdict
```

The package and the command are both `edgeverdict`. Before July 2026 the
project was named agentboard and the package shipped as `reviewgate`; it now
lives under one name. Old repository links redirect, and files under
`notes/` keep the old names as written at the time.

Or from source for development: `pip install -e ".[dev]"`.

The demo runs without an API key:

```
edgeverdict demo
edgeverdict demo --fixed
```

It gates a small bundled project with one planted bug. Proposals are
pre-generated, so the demo exercises the deterministic part of the pipeline:
the gate finds the gap, and finds it resolved on the fixed variant. A live
run needs `OPENAI_API_KEY` (reviewer) and, for the advisory auditor,
`ANTHROPIC_API_KEY` — or point `OPENAI_BASE_URL` at Ollama and pay nobody.

Then, in any repo with uncommitted work or a feature branch:

```
EDGEVERDICT_SANDBOX_NETWORK=install edgeverdict prove
```

Generated tests and the target repo's own lifecycle scripts run in a
hardened, network-isolated Docker sandbox by default; the env var above
grants network access to the install step only, which most real repos
need. Details, flags, and the explicit local opt-out are in
[SECURITY.md](SECURITY.md).

## Usage

Add an `.edgeverdict.toml` at your repo root, or skip it: the profile is
auto-detected from your lockfile.

```toml
base = "main"
project = "unit"
harness_notes = "Tests already import the framework; reuse existing helpers."
```

Reviewing a repo you don't own? edgeverdict never needs to write into it.
Run `edgeverdict init --user` to keep the config in your user dir
(`~/.config/edgeverdict/repos/<repo>.toml`), or pass `--config path.toml`
explicitly. The review itself also leaves the tree untouched: the board and
all run artifacts default to the system temp dir.

Review a change before pushing:

```
edgeverdict review --repo . --target src/parser.ts --intent "handle empty input"
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
server below. One guard: on a clean checkout (a `gh pr checkout`, a
committed branch), working-tree-vs-HEAD is a near-empty diff, and a review
of nothing would still "run" on intent alone. If the worktree diff does not
touch the target and no `--base` was given, the run aborts and names the
fix, rather than gating a phantom.

## Multi-file reviews

Add files explicitly:

```
edgeverdict review --target src/parser.ts --also src/lexer.ts --also src/ast.ts:tests/ast.test.ts
```

`--also` is repeatable. Tests are auto-detected per file; pass `file:tests`
to override. Files with no findable tests are skipped with a note.

Or select files by the blast radius of the change:

```
edgeverdict review --target src/parser.ts --scope all --depth 2
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

The workflow lives at `.github/workflows/edgeverdict-review.yml` with the
comment renderer at `scripts/render_pr_comment.py`. It picks the first
changed source file in the PR, skips quietly when a PR changes no reviewable
code, and skips fork PRs entirely (repository secrets are withheld from
forks, so the model key is unavailable there; that is correct, not a bug).

This repository runs it on itself:
[PR #1](https://github.com/anp0429/edgeverdict/pull/1) is edgeverdict
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

Install with pipx so the server binary lands on your PATH regardless of
which environment your agent runs in:

```
pipx install "edgeverdict[mcp]"
```

For Claude Code:

```
claude mcp add edgeverdict -- edgeverdict-mcp
```

If you installed into a plain venv instead of pipx, register with the
absolute path, since the agent's shell will not have your venv activated:

```
claude mcp add edgeverdict -- /path/to/venv/bin/edgeverdict-mcp
```

If your agent asks what this server is before adding it, the Trust and
provenance section below is a paste-ready answer.

For any other MCP client (Cursor, etc.), the server config is:

```json
{ "edgeverdict": { "command": "edgeverdict-mcp" } }
```

This exposes two tools. `prove` is the agent's loop verb: hand it a repo
(and an `intent` for uncommitted work), get back the artifact with a
one-line `verdict` first — BROKEN with runnable failing tests attached,
HELD with the executed-attempt count, or STOPPED with the cause. The
wording is shared with the CLI by construction and cannot drift. `review`
returns the same schema_version-1 artifact as `--json-out`. It defaults to `--worktree` mode: the diff is the
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


## Trust and provenance

Coding agents increasingly gate unfamiliar MCP servers behind a provenance
question before registering them. That is correct behavior, and this section
exists to answer it, for the agent and for you.

What this server is: a thin adapter over the same CLI. It builds the CLI's
own arguments and calls the same `review()` and `prove()` paths; a parity
test fails if the two ever accept different flags. There is no logic in the
server that the CLI does not have.

Where the code lives: source is this repository, and the package is
`edgeverdict` on PyPI. What you install is what you can read.

What it touches: tests execute locally, in a worktree sandbox of your
repository. Network calls go to exactly two places: the model provider you
configured with your own key (or a local Ollama, in which case none), and
the GitHub API when reviewing a pull request, to read that PR's stated
intent. Nothing else is contacted, and no telemetry is sent anywhere.

What it will not do: it never auto-approves, never raises on a confirmed
gap, and never blocks anything by itself. Verdicts are advisory evidence
handed back to the calling agent and ultimately to a human.

If your agent asks "what is this MCP server?", this is the paste-ready
answer:

> edgeverdict-mcp is the MCP adapter for edgeverdict (PyPI), source at
> github.com/anp0429/edgeverdict. It runs generated edge-case tests locally
> in a worktree sandbox and returns advisory verdicts; it never approves or
> blocks on its own. Network access is limited to the model provider key
> the user configured and the GitHub API for PR intent. The server is a
> parity-tested thin wrapper over the project's own CLI.

## How it works

1. Propose. An LLM reads the intent and the diff and proposes behaviors, each
   as a runnable test. A second-pass critic looks for gaps in that coverage.
2. Gate. Each test runs against the real code in a clean checkout. The gate
   is deterministic and contains no LLM. Verdicts: `handled`,
   `confirmed_gap`, `broken_test`, `timed_out`, `skipped_covered`.
   JS/TS repos are gated under vitest; Python repos are gated under
   pytest with the same verdict taxonomy. The pytest path is newer, and
   was hardened the honest way: five self-review rounds and a
   ten-stranger-repo gauntlet (see notes/prove-birth.md and
   notes/gauntlet.md).
3. Board. Verdicts render to an HTML review board. A run fingerprint lets any
   two runs be compared with one string.
4. Precision. Two advisory layers annotate confirmed gaps without touching
   verdicts. A deterministic pass flags gaps that fail with the verbatim
   same message: real gaps fail in their own words, artifacts fail in
   unison, so N identical failures are reported as one suspected setup
   cause, not N bugs (found the hard way when nine "gaps" shared one
   unwrapping mistake). Separately, a different model audits each confirmed
   gap for wrong assertions. Neither ever changes a verdict.

## What it runs on

Verdicts require executing tests, so support is defined by harnesses, not
adjectives. Today: **vitest** (TS/JS — npm and pnpm, workspaces, `.test`
and `.spec` suites, tests matched by path or by the target's exported
names when filenames don't cooperate) and **pytest** (Python — src and
flat layouts, PEP 735 dependency groups). Proven against a ten-stranger-
repo launch gauntlet; the scorecard, misses included, is in
`notes/gauntlet.md`.

One guarantee that holds on any repo in the world: prove never lies about
scope. It either reaches an executed verdict, or it STOPS with the actual
cause as the first line — never a silent pass, never a model's guess
dressed as one. Every environment that defeats it becomes a named fix
with that repo as the permanent regression test; robustness ratchets the
same way recall does.

Your stack next? Open an issue with a clonable repo. A harness is one
file behind three contracts: find the tests, execute a proposal, classify
the raised error.

## Monorepos and integration tests

A repository's most valuable tests are often the ones that talk to a real
database, and monorepos are where most real code lives. edgeverdict gates
pnpm workspaces natively: the target's package is resolved from the
workspace layout, the install runs at the workspace root the way the repo's
own CI would, and test execution is scoped to the package that owns the
resolved test file, invoking that package's own runner binary directly
rather than re-resolving the workspace on every call. On
supabase/supabase, a 28-project pnpm workspace, that scoping is the
difference between one test worker and a whole-workspace discovery pass;
the gate itself runs in seconds.

Three opt-in pieces, all off by default, make service-backed suites
gateable:

**Reaching a database.** The sandbox has no network, so a generated test
that needs Postgres cannot reach one. Setting `EDGEVERDICT_DB_URL` grants
the test phase (never the install) a bridge network with a host gateway
and injects the URL as `DATABASE_URL`, rewriting `localhost` to the
container's host alias so tests reach a database already running on your
machine. With the variable unset, execution is byte-identical to the
hardened default. This hands model-written test code a network and a live
database, so it is for repositories you trust and a disposable database
only; the threat model is in [SECURITY.md](SECURITY.md).

**A warm base across runs.** Installing a monorepo's dependencies and
smoke-verifying the environment are properties of the repository's
dependency state, not of the change under review, so
`EDGEVERDICT_WARM_CACHE=1` caches both across runs. The cache stores the
whole materialized workspace and restores it as one tree, so pnpm's
relative symlink forest survives intact and no re-link step runs on
restore. Measured on supabase/supabase: the first run pays the full
workspace install (about 65 seconds with install networking); every warm
run after it restores the installed workspace in about 29 seconds and
skips install, re-link, and smoke entirely. Details in Caching and cost
below.

**Room to install.** A whole-workspace install can overflow the
sandbox's RAM-backed `/tmp`. `EDGEVERDICT_TMPFS_SIZE` raises it (default
`512m`); raise `EDGEVERDICT_SANDBOX_MEMORY` (default `2g`) with it,
since tmpfs pages count against container memory.

| Variable | Default | Effect |
| --- | --- | --- |
| `EDGEVERDICT_DB_URL` | unset | test phase may reach a database you provide |
| `EDGEVERDICT_WARM_CACHE` | unset | `1` reuses the installed workspace + smoke verification across runs |
| `EDGEVERDICT_TMPFS_SIZE` | `512m` | sandbox `/tmp` size (RAM-backed) |
| `EDGEVERDICT_SANDBOX_MEMORY` | `2g` | container memory limit |
| `EDGEVERDICT_SANDBOX_CPUS` | `2` | container CPU limit |

A run on defaults gets a warning up front when the target is a workspace:
a whole-workspace install does not fit in 2g/512m, and an OOM-killed
install used to surface as a mysterious mid-install failure. The repo
ships `prove.sh`, a one-line wrapper with the workspace-sized environment
baked in (8g memory, 8g tmpfs, 4 CPUs, install networking, warm cache);
it echoes the environment it runs with and any exported variable
overrides it.

The harness also parses test titles through wrapper functions. Suites
that need services usually wrap the runner
(`withTestDatabase('title', async ({db}) => ...)` instead of a literal
`test(...)`), and a title parser that only matched `test(`/`it(` failed
on exactly the repositories this mode exists for. Titles now resolve
through arbitrary wrappers, outermost first.

Proven end to end on the supabase/supabase monorepo, twice over. The
`pg-meta` package, whose suite runs hundreds of integration tests against
a live Postgres, gated with generated tests injected through the repo's
own `withTestDatabase` wrapper, executed against the live database, real
verdicts returned. And the `studio` app, where a prove run against a
merged pull request produced two confirmed gaps that turned out to be
real bugs, filed upstream. See Results from real repositories below.

The honest boundary: today you stand up the database and set the URL
yourself. Detecting what services a package needs and provisioning them
from the package's own test compose is the next step, in
[ROADMAP.md](ROADMAP.md).

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

Install and smoke verification cache across runs, opt-in with
`EDGEVERDICT_WARM_CACHE=1`. Both are properties of the repository's
dependency state, not of the change under review, so the cache is keyed
on the lockfile hash plus the install command and package directory; on
a hit, install, smoke, and workspace re-linking are all skipped and the
run goes straight to injection and gating. For workspace repos the cache
stores the whole materialized dependency tree, the root store and every
per-package `node_modules`, captured as one unit after a verified
install. It restores as one copy-on-write clone, which is why no
re-link is needed: pnpm's symlinks are relative, and a tree that moves
together stays valid. The repository source is always freshly copied
(code is never served stale), the smoke marker is written last so a
half-written cache is never served, a structural guard detects and
discards a cache entry whose store is incomplete rather than serving it,
and any key mismatch falls back to a normal install.

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
# .edgeverdict.toml
reviewer_model = "qwen3.6:27b"            # or "moonshotai/kimi-k2.6", etc.
critic_model = "devstral-small-2"         # a different lineage decorrelates
base_url = "http://localhost:11434/v1"    # optional: pin this repo's endpoint
```

Because one environment variable redefines what every model name means,
the run log always names the endpoint each model will talk to
(`reviewer qwen3.6:27b via localhost:11434`), so a stray export from
another session is visible instead of silent. A repo can also pin its
endpoint with `base_url` in config (an explicitly set `OPENAI_BASE_URL`
still wins), and preflight rejects the one unambiguous mistake up front:
an OpenRouter-shaped key (`sk-or-...`) with no base URL set would only be
refused by api.openai.com, so the run stops in two seconds with the fix
instead of failing mid-review.

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

- Its own repository, first: before `prove` merged, it reviewed its own
  uncommitted diff — and across five self-review rounds found twenty-five
  real defects in and around its own code, including a false-positive
  generator inside its own verdict classifier and a hole in the fix to
  that bug hours later. Broken proposals went 33, 29, 0, 0, 0 as the
  rounds taught the proposer its own repo. The full ledger, severity-
  labeled, with run fingerprints: [notes/prove-birth.md](notes/prove-birth.md).
- [supabase/mcp#317](https://github.com/supabase/mcp/pull/317): proposed
  tests reproduced a bug on main. Composite foreign keys in `list_tables`
  returned the cartesian product of column pairs, so an N-column key reported
  N² pairings, most of which do not exist in the schema. The fix and the
  generated regression tests (self-referential, cross-schema,
  non-primary-unique, multi-FK, three-column) are merged into main.
- [supabase/supabase#49545](https://github.com/supabase/supabase/issues/49545)
  and [#49546](https://github.com/supabase/supabase/issues/49546): a prove
  run against a merged pull request in the `studio` app (two human
  approvals, an AI reviewer, full CI green) produced confirmed gaps in the
  notebook diff derivation that renders the approval preview for
  assistant-edited notebooks. Two were verified by hand as real bugs: an
  insert anchored on a previously-moved cell resolves against the anchor's
  pre-move position, and a downgraded no-op move displaces a removed entry.
  Both contradict invariants stated in the PR's own description. The
  failing tests reproduced unchanged across a schema refactor, four
  follow-up PRs, and ten days of active development before being reported
  upstream with the tests attached.

Every finding above was produced by executing tests, and every one is
reproducible by hand.

## Benchmark

[BENCHMARK.md](BENCHMARK.md) measures the harder question: can edgeverdict
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
edgeverdict review --target src/parser.ts --intent "handle empty input" --dataset
```

Each row stores the proposal and the executed verdict. The honest label is
`ran` (did the test execute), derived only from the gate's status; the
advisory audit is stored alongside but never overwrites it. Collection is
opt-in and append-only, writing to `~/.edgeverdict/dataset.jsonl` by default.
Existing `--json-out` artifacts can be backfilled, so a corpus can start from
runs that predate the collector (the benchmark seeds ~260 rows on its own).

The data is not yet used in the loop; it is the substrate for the model work
in [ROADMAP.md](ROADMAP.md) (run an open model against the same benchmark,
then train the proposer on gate outcomes). Rows collected from public repos
are clean to keep; any future company deployment keeps its own data local and
never comingled, by design.

The collaboration-loop code that predates the gate (fix agents proposing
patches, multi-model argument with executed tests as referee) lives under
`edgeverdict.experimental` pending that roadmap.

## Reliability

The classification path is checked for byte-identical verdicts across 1,000
runs per verdict class on every CI push. A falsifier test (`expect(1).toBe(2)`)
must always classify as a real failure; if an impossible test ever reads as
passing, CI fails. Batched and serial gate paths are asserted
verdict-identical by fingerprint.

Environment failures report both output streams, labeled. `stderr or
stdout` once hid a real install error behind a harmless package-manager
warning; the stream you drop is the stream holding the cause, so neither
is dropped.

## Limitations

- The execution trust model: reviewing a repo executes that repo's
  dependency lifecycle scripts and the generated tests. By default this
  happens inside a hardened Docker container: no network (opt-in for the
  install step only), read-only root, non-root user, dropped
  capabilities, and an environment filter where the secret-name deny
  rule overrides the allowlist. Running outside the sandbox requires an
  explicit unsafe opt-in. The boundary is a container, not a VM; treat
  repositories you review with the same judgment you would give any
  code you run. See [SECURITY.md](SECURITY.md).
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
- Database-backed integration suites run only with the explicit
  `EDGEVERDICT_DB_URL` opt-in and a database you provide. The tool does
  not provision services yet; that is roadmap.
- The audit pass is advisory and not yet load-bearing.
- Vitest (pnpm or npm, including workspaces) and pytest are the two
  harnesses. Jest is not supported: a jest suite is reported as an
  unsupported environment, not gated. Repos that mix both (jest on the
  frontend, pytest on the backend) gate on their Python side today.
- Current vitest is the supported target. Very old checkouts (vitest 0.2x
  era) tend to fail at environment preparation for toolchain reasons that
  predate edgeverdict; the run reports this as an environment failure rather
  than producing verdicts.
- pnpm repos run under the version the repo's own config parses: the
  `packageManager` pin is honored when modern (>= 9); with no pin there, a
  modern pin in `mise.toml` or `.tool-versions` is honored next (pins
  migrate — supabase/mcp dropped `packageManager` for mise mid-2026); and
  only then does it fall back to a pinned `pnpm@9`.
  A single hardcoded pin broke in both directions (old pnpm dies on Node 22;
  pnpm 9 rejects a pnpm 10/11 `pnpm-workspace.yaml` config), so the rule is
  "run under the version this config was written for."
- Monorepos with multiple vitest projects usually work unscoped now (the
  environment probe and the proposals run beside the resolved tests file,
  inside whichever project owns it — zod's workspace gates unscoped). A
  one-line `.edgeverdict.toml` naming the `project` or `filter` remains the
  right call when a repo boots special environments (browser projects,
  custom pools), the same way you would scope CI.

## Design invariants

1. The verifier is deterministic and external. No LLM sits in the accept or
   reject path.
2. Correctness comes from running the code fresh, not from memory, not from a
   second model agreeing, and not from a test that never made a red to green
   transition.
3. A second model may flag disagreement. It never votes, and conflicts
   surface for a human instead of being averaged away.
4. Every proposal is verified against a clean tree.
## Sandboxed execution

Reviewing a repository means executing untrusted code twice over: the
repository's own dependency lifecycle and build scripts, and the tests
a model just wrote. Both run inside a hardened Docker container by
default. Proposals (the model calls) happen on the host with your key;
the key never enters the container.

What the container gets: a bind mount of the throwaway working copy,
and a small allowlist of non-secret environment variables where the
secret-name deny rule overrides the allowlist, so a typo in the
allowlist cannot leak a token. What it does not get: your host
environment, your home directory, the Docker socket, or a network
(unless you grant one, below). It runs as a non-root user on a
read-only root filesystem with dropped capabilities and CPU, memory,
process, file-size, and output limits.

### Setup

One-time, since the image is never pulled automatically (Docker must
be installed and running; on Apple Silicon, Docker Desktop may prompt
for Rosetta):

```
docker build -f docker/Dockerfile.sandbox -t edgeverdict-sandbox:latest .
```

A different image can be named with `EDGEVERDICT_SANDBOX_IMAGE`.

### The three variables

| Variable | Values | Default | Effect |
| --- | --- | --- | --- |
| `EDGEVERDICT_EXECUTION_BACKEND` | `docker`, `local` | `docker` | where lifecycle commands and generated tests run |
| `EDGEVERDICT_SANDBOX_NETWORK` | `none`, `install`, `all` | `none` | what, if anything, gets network access |
| `EDGEVERDICT_ALLOW_UNSAFE_LOCAL` | `1` | unset | required for the local backend; without it, local execution refuses to run |

The network policy deserves the extra sentence. `none` is the only
mode intended for repositories you do not trust, and it requires
dependencies to already be present: vendored, in the image, or in an
offline cache. `install` grants network to recognized package-install
commands only, but install is exactly when lifecycle scripts execute,
so use it only with repositories you trust. `all` is for debugging
trusted projects. The full threat model is in
[SECURITY.md](SECURITY.md).

### The runs you will actually type

```
# untrusted repo, deps already present: fully offline sandbox
edgeverdict prove

# most real repos: sandboxed, network granted to the install step only
EDGEVERDICT_SANDBOX_NETWORK=install edgeverdict prove

# explicit escape hatch: no sandbox, runs as you (trusted repos only)
EDGEVERDICT_EXECUTION_BACKEND=local EDGEVERDICT_ALLOW_UNSAFE_LOCAL=1 edgeverdict prove
```

### When something fails

- `Docker is required for the default hardened backend` — build the
  image with the command above, or use the explicit local escape
  hatch for a repo you trust.
- Install fails or hangs under the default policy — most repositories
  need `EDGEVERDICT_SANDBOX_NETWORK=install`; the default assumes
  dependencies are already present. A preflight now refuses in under a
  second, naming the variable, when an install will need network it was
  not granted — instead of letting DNS retries burn minutes first.
- Environment failures are triaged on first failure, not retried blind:
  a DNS failure names the network variable, an OOM-killed or
  disk-full install names `EDGEVERDICT_SANDBOX_MEMORY` and
  `EDGEVERDICT_TMPFS_SIZE`, and the retry ladder is skipped when
  retrying cannot help. A frozen-lockfile install that fails for any
  other reason is still retried without the frozen flag, and the log
  names both attempts and the cause. Repair never runs after an
  environment failure, so no tokens are spent on a run the
  environment already doomed.
- A frozen-lockfile install that fails is retried automatically
  without the frozen flag; the run log names both attempts.
- The demo never needs any of this: its bundled target runs with the
  local backend by design.
