# prove: spec, kill list, launch gauntlet

The caveman sentence: an agent wrote your code, `agentboard prove` tries to
break it, and you see one of two things: a failing test you can run, or
"tried, executed, couldn't."

Same engine, same gate, new door. `prove` is the author's moment (me or my
agent, now, before pushing). `review` stays the maintainer/CI moment and
does not change. The MCP server exposes `prove` as the primary tool name,
`review` kept as an alias.

## UX contract

`agentboard prove` with zero flags, from anywhere inside a repo:

1. **Mode.** Dirty tracked tree -> worktree mode (the state on disk is the
   thing under test). Clean tree -> current branch vs its fork point.
   Both paths already exist (`--worktree`, `fork_point`).
2. **Targets.** `targets_from_diff`: changed source files, tests and
   deletions excluded, sorted. Zero targets -> one line saying what was
   diffed and that nothing reviewable changed, exit 0.
3. **Intent.** `--intent` if given, else `intent_from_commits` on the
   branch. Worktree mode with no commits and no `--intent` -> ask the one
   question, with the answer's shape shown, never two questions.
4. **Output.** Verdict line first, always one of:
   - `BROKEN: N failing tests (M attempts executed)`
   - `HELD: M executed attempts, 0 broke it`
   - `STOPPED: <one-sentence cause>, nothing executed`
   Below the fold: each failing test as a runnable command + observed
   output; the survived list collapsed; board path last; rough token cost
   at the end. During the run, say what is happening ("proposing ~N
   tests", "executing against a clean tree") — silence reads as hung.
5. **No-key screen.** No key and no base URL -> print exactly three exits
   and stop:
   - export OPENAI_API_KEY=...            (any OpenAI key)
   - export OPENAI_BASE_URL=...           (free/local via Ollama or any
     OpenAI-compatible server)
   - agentboard demo                      (watch it work right now, no key)
   Plus one parenthetical: the model proposes tests; the verdict never
   uses one. A set-but-bad key must die in seconds via the existing
   key-shape preflight (verify against /chat/completions, never /models).

## Honesty rules (unchanged, restated because the output is new)

- "HELD" is absence of a counterexample among M executed attempts. The
  wording never claims correctness, verification, or proof of absence.
- Every early exit states what was attempted and why it stopped. Green by
  doing nothing is the friction-#5 lie class and is banned.
- broken_test / skipped_covered / env plumbing are agentboard's problems:
  collapsed by default, never in the verdict line.

## Kill list (acceptance criteria, ranked by lethality)

1. **False BROKEN on first contact.** Wire the assertion lint
   (`verifiers/assertion_lint.py`, built + tested, currently unwired) into
   the prove path. Exact assertions on collections/counts; loose on
   rendered values. Zero false BROKENs across the gauntlet or no launch.
2. **The silent nothing.** Covered by the STOPPED line + zero-proposal
   hard stop. Every gauntlet run must end in BROKEN, HELD, or a
   one-sentence STOPPED.
3. **The environment wall.** Cause-first failure text (the `_tail`
   head+tail fix guarantees the first line survives). Never a stack trace
   as the opening line.
4. **The clock.** First output within seconds, verdict inside ~2 minutes
   on a normal repo, hard ceiling 3 minutes to first verdict or a STOPPED
   explaining the slowness. Progress lines during.
5. **The question it shouldn't ask.** At most one question per run, with
   the expected answer format shown inline.

## Launch gauntlet (the measurable version of "one shot")

Eleven runs, scored like the benchmark, before any public mention:

- Runs 1-10: fresh repos never configured for agentboard. Cold clone,
  make a small plausible change (or check out a real PR branch),
  `agentboard prove`, zero flags.
  - Pass bar: >= 9/10 reach BROKEN/HELD or a clean one-line STOPPED with
    no intervention; 0 false BROKENs; no run past 3 minutes to first
    verdict/STOPPED.
- Run 11: machine with no key configured. Pass = the three-exit screen
  instantly, then `agentboard demo` works.
- Every miss becomes a fix plus a named regression case (the Phase 1
  playbook).

Deadline stake: gauntlet green by Sunday night 2026-07-26 or the HN post
ships as the review gate on Tue/Wed 2026-07-28/29 and prove launches when
the gauntlet says so.

## Build order (each step lands with tests, suite green before push)

1. [x] Deterministic helpers: `working_tree_dirty`, `targets_from_diff`
   (+ `tests/test_prove_support.py`, 7 cases: dirty cue ignores untracked,
   three-dot base isolation, worktree diff, test-shape exclusion,
   deletion/non-source exclusion).
2. [ ] `prove` subcommand: thin adapter building a ReviewRequest from the
   defaults above, reusing `api.run_review`. Multi-target via the existing
   `--also` machinery. Mind the CLI<->ReviewRequest parity test.
3. [ ] Output shaping: verdict-line-first renderer over the run result +
   progress narration + token-cost line.
4. [ ] No-key screen + key preflight wiring in the prove path.
5. [ ] Assertion lint wired into prove (advisory gate on BROKEN
   promotion), with the benchmark's wrong-remedy row as the regression
   case.
6. [ ] MCP: `prove` tool name, `review` alias, parity test extended.
7. [ ] Demo rewrite: agent-writes-function story, bug found -> fixed ->
   held, both no-key, < 30s.
8. [ ] Gauntlet, then fixes, then gauntlet again until the bar clears.

## Out of scope this week

Underspecified verdict (0.6.0). VS Code extension. Dashboards. New verdict
classes. Any edit to the verifier's verdict logic. Hosted keys/proxy
(BYOK-or-local at launch; hosted is a post-launch decision made on
observed demand). Renaming the repo or package: `prove` is the verb, the
names stay until user confusion is observed, not predicted.
