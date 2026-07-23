# How prove spent its first day: breaking itself

July 22, 2026. The `prove` subcommand was written in one evening: an
agent (or a human) changes code, `agentboard prove` tries to break the
change, and the output is a failing test you can run, or "tried,
executed, couldn't." Before merging it, we pointed it at its own
uncommitted diff. This note is the ledger of what happened, because it
turned out to be the best argument for the tool.

## Round one (run fingerprint 94669614e770c539)

70 proposed behaviors across 4 changed files. 14 executed to a verdict,
21 were recognized as already covered by the repo's own tests, 33 broke
before running (the reviewer model hallucinated imports and fixtures —
including borrowing a helper from our test files without defining it —
and the gate killed every one; a broken proposal cannot manufacture a
finding). 7 confirmed gaps, each an executed failing test.

Triage: 4 real, 2 false positives, 1 judgment call.

The real ones:

1. **The classifier could fabricate false positives.** `classify_failure`
   decided "assertion" by checking whether the string "AssertionError"
   appeared anywhere in the failure report. A crash whose traceback
   merely mentioned that string became a confirmed gap. This bug sat in
   the verdict path's plumbing — the exact component whose honesty the
   whole tool depends on — and the tool found it in itself. The fix
   classifies by the exception actually raised, read from the report's
   raising line or pytest's location tail.
2. Import matching for test discovery could be fooled by import-looking
   text inside docstrings, and couldn't see multiline aliased imports.
   Fixed by matching real AST import nodes instead of text.
3. An `__init__.py` target produced the stem `test___init__.py`, a file
   nobody has ever written on purpose. Fixed: the package directory's
   name is the stem.
4. A run where nothing executed printed STOPPED but exited 0. Fixed:
   the exit code and the verdict line always tell the same story.

The false positives were themselves informative: one was a NameError
wearing a confirmed-gap costume — put there by bug #1, and flagged
likely_false_positive by the advisory auditor before we knew why.

Every fix landed with the gate's own failing scenario converted into a
permanent regression test.

## Round two (run fingerprint ff36b2226dfda3eb)

Same command, a few hours later, against the fixes. Gaps went 7 to 5;
behaviors recognized as covered went 21 to 47, because round one's
regression tests now preempt half the reviewer's ideas. Of the 5, four
were real again — and one was a hole in that day's own fix:

1. The new classifier checked timeout wording before exception identity,
   so an assertion whose message legitimately mentioned "timed out"
   classified as a timeout. The reviewer found the flaw in the patch
   within hours of the patch. Order fixed: an identified AssertionError
   wins outright.
2. Injection deduplicated imports by text match at any indentation, so a
   local import inside a test body could be silently deleted, rewriting
   the proposal. Now only column-0 duplicates are stripped.
3. Test-title extraction used a regex over raw text and could pick a
   docstring example that looks like a test def. Now the title comes from
   the AST.
4. Class-based test proposals needed the class-qualified node id
   (`TestX::test_y`) for exact serial selection. Same AST fix.

The fifth was an opinion about output formatting, kept in the specimen
jar for the assertion-quality work.

## Round three (run fingerprint e4a011add5ff924c)

The first fix of the trust layer was giving the reviewer the target's
real import surface — module path plus public names, read from the AST —
after 33 and then 29 proposals had died of hallucinated imports in the
first two rounds. The next self-review ran with that data in the prompt:
**zero broken proposals.** Every test the model wrote imported correctly,
executed, and reached a verdict. One prompt line of deterministic facts
retired the entire failure class.

And then the recursion went one turn deeper: with imports finally
working, the freed-up proposals interrogated the newest function in the
repo — `import_surface` itself — and found five real gaps in its model
of "importable," every one flagged likely_real by the advisory auditor:

1. Annotated constants (`ENABLED: bool = True`) are bindings; the first
   version only read plain assignments.
2. Tuple and compound assignments (`FIRST, SECOND = 1, 2`) bind every
   name; the first version saw none of them.
3. Lowercase public names (`router = APIRouter()`) are exactly what real
   tests import; an ALL-CAPS filter excluded them.
4. A package `__init__.py`'s re-exports (`from .mod import Thing`) are
   the most common public surface in Python packaging; imports were
   ignored entirely.
5. Namespace packages (PEP 420) have no `__init__.py`, so requiring one
   at every level truncated `company.product.feature` to a path that
   does not import.

Our own unit test had blessed `src.pkg.mod` as a module path; the review
corrected the test too — `pkg.mod` is what pip installs. All five fixed
the same hour, each with the review's scenario as a regression test.

## The ledger

Sixteen real defects found by the tool in and around its own code in one
day, across three self-review rounds: four in the verdict path itself,
one in a fix made hours earlier, and five in the very function built to
fix the reviewer's imports. Broken proposals went 33, 29, 0 across the
rounds. Every defect is now a named test in the suite. The merge of the
`prove` feature was blocked, by rule, until the tool's own verdict on its
own diff came back clean of real gaps — prove gated its own birth, and
then kept gating its own growth.

None of this required trusting a model's opinion. Each finding above was
a test that compiled, ran, and failed against the real code, and each
fix was proven by the same mechanism. That is the whole thesis of the
tool, demonstrated on the least flattering codebase available: ours.
