# Proposer model comparison (informal)

One planted bug, three reviewer models, same gate. This is a kitchen
benchmark, not the published one: n=1 target, a synthetic repo, no audit
pass. It exists because the question "how much reviewer model do you
actually need?" deserves measured rows, not vibes, and these are the first
three.

## Setup

Target: a three-line `clampPageSize(n, lo, hi)` with a planted exclusive
upper bound (`Math.min(n, hi - 1)`), uncommitted in the working tree.
Intent: "clamp page size to an inclusive lo..hi range". One committed test
(in-range passthrough). Reviewed with `--worktree --no-audit`, critic same
model as reviewer. Gate and verdict path identical across runs; only the
proposer changes. Date: 2026-07-19, reviewgate 0.2.2/0.2.3.

## Results

| model | via | behaviors | real catches | wrong-assertion FPs | broken tests | cost |
| --- | --- | --- | --- | --- | --- | --- |
| kimi-k2.6 (1T MoE) | OpenRouter | 7 | 4 | 0 | 0 | ~1¢ |
| qwen3.6:27b | Ollama (local) | 5 | 2 | 0 | 0 | $0 |
| llama3.1:8b | Ollama (local) | 16 | 2 | ~8 | 4 | $0 |

"Real catches" are confirmed_gaps whose assertions reflect the intended
contract (all four of Kimi's independently pin the planted off-by-one:
upper bound, clamp-from-above, adjacent bounds, float between hi-1 and
hi). "Wrong-assertion FPs" are confirmed_gaps that executed and failed but
assert something the intent never promised (llama decided the function
returns a collection and asserted `.length` on a number, six times).

## What the rows say

The gate's trust guarantee held at every model size: llama's four
malformed tests landed in broken_test, not in the gap count, and its
batch-unattributable titles fell back to serial and still produced
verdicts. A weaker proposer cost recall and precision-of-assertion, never
verdict integrity. That is the designed degradation.

Assertion quality, not bug-reaching, separated the tiers (the same
frontier the published benchmark names), and the cliff sits between 8B
and 27B, not between local and cloud. llama reached the bug (twice) but
buried it in noise an auditor pass would have had to clean up. qwen at
27B, running free on a laptop, produced a signal-only report: every
confirmed gap real, nothing broken, correct use of skipped_covered. What
the extra scale of Kimi bought was breadth, not precision: four
independent angles on the same defect (including a float probe between
hi-1 and hi) versus qwen's two. More proposals per behavior means more
redundant executed evidence, which matters when one wrong assertion could
mislead; it did not change who found the bug.

The practical read: a 27B local model is a credible daily gate for a solo
developer at zero marginal cost; the hosted open-weight tier adds catch
redundancy for about a cent; the auditor exists for whatever tier you can
afford least to trust unaudited.

## Session finds (what dogfooding surfaced in one evening)

- Uncapped openai-path completions let a provider generate for minutes and
  made a metered router reserve the full output ceiling (402 on a small
  balance). Fixed: caps in 0.2.3.
- A propose that failed (dead key) cached its empty result, making the
  outage permanent for those inputs. Fixed: empty proposes are never
  cached, 0.2.3.
- A preflight test inherited the shell's `OPENAI_BASE_URL` and flipped its
  outcome. Fixed: the test scrubs it.
