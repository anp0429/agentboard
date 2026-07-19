# Benchmark

Can agentboard find a real bug in code it has never seen, from an intent
that does not name the bug? This measures that, on recent merged bugfix PRs
from active TypeScript repositories, and publishes the misses.

## Method

For each row:

1. Pick a bugfix PR that is already merged into an active repo.
2. Check out the PR's **parent** commit: the code exactly as it was before
   the fix. The bug is present.
3. Run agentboard with a **neutral intent** that describes what the module
   is for, never what the bug is. PR titles telegraph the answer, so the PR
   and its tests are ground truth, not input.
4. Score the confirmed gaps against the fix, by reading source.

The intent discipline is the whole experiment. "recursively merge an object
with default values" is a fair description of defu; "prevent prototype
pollution via `__proto__`" would be handing over the answer. If a skeptic
can argue the intent leaked the bug, the row does not count.

No row uses the bug agentboard was originally developed against
(supabase/mcp #317). Training on your test set is not a benchmark; #317
appears only as a disclosed reference case in the README.

Every gap in this table was adjudicated by reading the fix diff or the
source at the parent commit. The auditor's advisory calls were **not**
trusted as scores; see the auditor-inversion note below for why that
matters.

## Results

Tool version `e175535`. 12 distinct bugs across 8 repos (two rows are
security-axis twins). Zero environment failures.

| # | Repo | PR | Target module | Outcome |
|---|------|----|--------------|---------|
| s1 | supabase/mcp | #269 | content-api graphql | in-area catch |
| s2 | supabase/mcp | #329 | management-api errors | miss (4 clean false positives) |
| r1 | unjs/ufo | #335 | url base join/strip | missed #335, **re-found #360** |
| r2 | unjs/ufo | #313 | url base prefix match | **strict catch** |
| r3 | unjs/defu | #156 | recursive default merge | **strict catch** (`__proto__` pollution) |
| r4 | unjs/pathe | #246 | path resolve | **strict catch** (UNC authority) |
| r5 | unjs/pathe | #241 | path parse | in-area catch (node divergence) |
| r6 | pmndrs/jotai | #3354 | store subscribe | miss (subtle async) |
| r7 | pmndrs/jotai | #3326 | atomWithStorage | miss (component harness) |
| r8 | vueuse/vueuse | #5525 | useFetch | **in-area catch + 2 bonus bugs** |
| r9 | TanStack/query | #10812 | devtools sort | **strict catch** |
| r10 | colinhacks/zod | #5898 | object catchall | miss (wrong-remedy assertion) |

Security-axis twins: r3s (defu) reproduced the strict catch and added a
nested `__proto__` variant the default axis missed; r10s (zod) missed the
same way the default did.

**Summary: 8 of 12 rows produced a real confirmed bug. 4 were exact strict
catches (ufo #313, defu #156, pathe #246, query #10812). r8 over-delivered:
pointed at vueuse before a known fix, it caught the fix's neighborhood and
two additional real bugs the PR never touched (a falsy-`initialData` bug
from `||` where `??` was meant, and a header-array-merge bug). 4 rows
missed. 0 environment failures.**

### The two bonus bugs (r8)

vueuse `useFetch`, at the parent of #5525, neutral intent "fetch a URL
reactively with abort and refetch support":

- `const data = shallowRef<T | null>(initialData || null)`: `||` coerces a
  valid falsy `initialData` (`0`, `''`, `false`) to `null`. Should be `??`.
- `headersToObject` only handles `Headers` instances, silently dropping
  array-form `HeadersInit` (`[['authorization', ...]]`).

Neither is what #5525 fixed. Both are real, both were found from a neutral
intent, both have an executed failing test attached.

## The auditor inverted, twice, and that is the point

The auditor is a second model (Claude) that reads the source and flags
confirmed gaps it believes assert something the code never promised. It is
**advisory**: it never changes a verdict.

On two rows it called a **real strict catch** a false positive:

- **r2 (ufo #313):** the code matched `/api` as a prefix of `/api-v2`. The
  auditor called the failing test a wrong assumption.
- **r9 (query #10812):** the comparator `(a, b) => a < b ? 1 : -1` never
  returns `0` for ties, violating the comparator contract, the exact thing
  #10812 fixed. The auditor flagged it a false positive **and cited the
  buggy line as evidence**, mistaking the bug for the contract.

A calibration pass (a false-positive call must now quote the source line it
relies on, or it downgrades) made the auditor's output more useful but did
**not** stop this: it can cite the buggy code as if that code were the
specification. This is the benchmark's most important finding, and it is the
thesis proving itself inside the tool. The judging model was confidently
wrong. Execution was right. That is exactly why the verdict comes from the
gate and never from a model. The auditor is a lead, not a ruling, and every
gap in this table was adjudicated by a human reading source.

## Misses, in daylight

- **s2 (supabase #329):** four gaps, all false positives. The proposals
  asserted `.length` on a formatted string result. The real fix (a 403
  org-scoping error message) was never approached. The auditor correctly
  flagged all four.
- **r6 (jotai #3354):** a subscriber-notification bug that only manifests
  after a nested `store.set` with sub/unsub inside a write. Too subtle for
  the proposer; two false positives instead.
- **r7 (jotai #3326):** a React component target. The proposer wrote React
  Testing Library tests with wrong DOM assumptions; the gate correctly
  classified them `broken_test` rather than gaps. Complex component harnesses
  are a real reviewer-capability limit.
- **r10 (zod #5898):** the `__proto__` catchall bug is present, but the
  proposer asserted the wrong remedy (expected `__proto__` as a visible data
  key) rather than the real defect (prototype pollution). The bug class was
  right, the assertion was wrong. Same bug class as defu #156, opposite
  outcome (see below).

## Assertion quality is the frontier

defu #156 and zod #5898 are both `__proto__` bugs. defu's proposal asserted
*the prototype was not polluted* and caught the bug. zod's asserted
*`__proto__` should appear as a data key* and missed it, producing a false
positive on a real defect. Same vulnerability, opposite result, decided
entirely by what the model chose to assert. Improving assertion quality,
not reaching the topic, is the open problem this benchmark surfaces, and it
is the training signal behind the roadmap's model work.

## Determinism receipts

Every run prints a fingerprint over its verdicts. Six rows were run on two
separate days, through a proposal-cache hit and a changed advisory layer,
and reproduced byte-identical fingerprints:

| Row | fingerprint (both runs) |
|-----|-------------------------|
| s1 | `d64b434d0833afcf` |
| s2 | `3811dc33363f9751` |
| r1 | `6e9bb41299792a83` |
| r3 | `b6a7bd31e37e5bb7` |
| r4 | `35d6c88385a680c2` |
| r10 | `f7214bf3b971ad1c` |

The auditor changed between the two runs; the fingerprints did not, because
the auditor is advisory and never enters the verdict. When verdict-side code
changed, every row was rerun on one tool version rather than mixing.

## Reproduce

Each row is a fresh checkout at the parent commit, `--base HEAD` (empty
change, stranger-mode review), a neutral intent, and the auditor on:

```
git clone <repo> && cd <repo> && git checkout <parent-sha>
agentboard review --repo . --target <file> --tests <tests> \
  --head HEAD --base HEAD --intent "<neutral intent>" \
  --audit-model claude-sonnet-5 --json-out run.json
```

Parent SHAs, targets, and neutral intents for all rows are in
`bench/run_bench.sh`. Environment failures, when they occur, are reported as
failures, never retried until green.
