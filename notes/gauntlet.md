# The launch gauntlet — run sheet (increment 8)

Bar (from notes/prove.md): >=9/10 stranger repos reach BROKEN / HELD /
clean STOPPED unassisted; 0 false BROKENs; <3 min to first verdict;
run 11 = no-key machine gets the three-exit screen and a working demo.
Every miss becomes a fix + a named regression test, then ONE rerun of
that repo. 0.5.0 ships to PyPI only when this sheet is green.

## Setup (once, AFTER tonight's round-six merge — test what strangers get)

python3 -m venv /tmp/gauntlet-venv
source /tmp/gauntlet-venv/bin/activate
pip install "git+https://github.com/anp0429/agentboard"
agentboard --help

## Protocol per repo (the replay trick)

Replay the repo's own most recent real change as if an agent just wrote
it: clone, pop the latest non-docs commit back into the working tree,
hand prove that commit's message as the intent.

git clone <URL> /tmp/g<N>
cd /tmp/g<N>
git log --oneline -8

Pick the newest commit that touches source (skip docs/chore/release).
If it is not HEAD, first: git checkout -b gauntlet <sha>

git reset HEAD~1
agentboard prove --intent "<that commit's subject line>"

Eligibility check before counting a repo (2 min): tests exist, the test
runner is vitest or pytest, `npm install` / `pip install -e .` succeeds.
A repo that fails eligibility is swapped, not scored — the gauntlet
measures prove, not npm's mood. A repo that PASSES eligibility and then
STOPS dirty counts as a miss unless the STOPPED cause is honest and
actionable (that is what "clean STOPPED" means).

## Candidate repos (suggestions — swap freely, mark what you ran)

TS/vitest lane:
1. unjs/ufo            (known ground: #360 came from here)
2. colinhacks/zod      (known ground: #6211/#6212)
3. vueuse/vueuse
4. unjs/defu
5. sindresorhus/ky

Python/pytest lane:
6. python-humanize/humanize   (known ground: #356)
7. marshmallow-code/marshmallow (known ground: #3005)
8. python-attrs/attrs
9. jd/tenacity
10. dateutil/dateutil

Two "known ground" per lane is deliberate: they calibrate (we know what
honest output looks like there); the other six are true strangers.

## Scorecard (fill per run; this table IS the launch evidence)

| # | repo | sha replayed | t-to-verdict | verdict | false BROKENs | broken proposals | notes |
|---|------|--------------|--------------|---------|----------------|------------------|-------|

False-BROKEN check, by hand, for every BROKEN: read the failing test —
does it assert the code's real contract, or the proposer's assumption?
(The auditor's annotation helps; the human decides. One false BROKEN
fails the whole gauntlet — that is the product promise.)

## Run 11 — the no-key machine

deactivate the venv key however it is set:
unset OPENAI_API_KEY
agentboard prove
Expect: the three-exit screen (key / OPENAI_BASE_URL=Ollama / demo).
agentboard demo
Expect: BROKEN with the failing test, under ~30s after npm's first run.

## Cost + time budget

Spend is proposals only (~1 min sampling per changed file; the gate is
seconds). Ten runs at 1-4 changed files ≈ 30-60 min wall clock and a
few dollars. If a run's diff is huge (release commits), pick a smaller
commit — the gauntlet tests cold-start honesty, not endurance.

## After

Green → tag 0.5.0, publish to PyPI, README prove section in the same
PR, Monday-night count freeze in notes/prove-birth.md, HN Tue/Wed with
this scorecard linked. Not green → fix + regression per miss, rerun the
misses once; if still short, the review-gate HN ships as drafted and
the gauntlet finishes on its own schedule. Either way the sheet is
published — misses included. Receipts, not memory.
