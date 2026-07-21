"""The framework seam of the gate: everything a test framework decides.

FindingVerifier owns the *gate semantics* — warm sandbox, batch-then-serial
fallback, the four-verdict contract, and the conservative "no minted gaps"
rule. What it must NOT own is any opinion about vitest, because the next
repo is a pytest repo and the verdict logic is framework-independent by
design. This module names the boundary:

    RepoProfile  = repo facts       (install/build/test commands, env, smoke)
    Harness      = framework facts  (how to inject a test, how to name it,
                                     how to filter a run to it, how to read
                                     the runner's output, what a genuine
                                     assertion failure looks like)

A Harness answers exactly the questions the verifier asks per finding:

    inject          proposal code into the pristine tests file (PURE)
    strip_imports   the injection-legality rule for proposal imports
    test_title      the name the runner will know the proposal by
    mark_title      stamp a gate-owned mark into that name (batch attribution)
    serial_command  run ONLY one titled test, results to a file
    batch_command   run ONLY mark-stamped tests, results to a file
    read_verdict    one serial run's output -> (status, observed)
    read_batch      one batched run's output -> neutral per-test records
    classify_failure one failure message -> assertion | timeout | load_error
    default_tests_for the framework's test-file naming conventions

The classification hook is the load-bearing one: a confirmed_gap may only
ever come from a named assertion failure, and each framework spells its
assertion layer differently. The vitest spelling lives here now, moved
byte-for-byte from finding_verifier so the e2e fingerprint tests stay the
oracle that nothing observable changed.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..review import Status


@dataclass
class BatchResult:
    """One executed test from a batched run, framework-neutral.

    The verifier's attribution logic (mark matching, verdict priority) works
    on these records only, so it cannot grow a framework dependency back.
    `status` values other than passed/failed mean "did not properly run" —
    the verifier leaves those findings for the serial path to decide."""

    title: str    # the name the runner reported (carries the gate's mark)
    status: str   # "passed" | "failed" | anything else = did not run
    failure: str  # first failure message, "" when none


class Harness(ABC):
    """Framework facts, named instead of assumed. See the module docstring
    for the contract each method fulfills."""

    name: str = ""
    # source-file extensions this framework's repos use; drives dispatch in
    # default-tests discovery (api._default_tests_for).
    src_suffixes: tuple[str, ...] = ()
    # runner artifact filename, written into the sandbox repo root
    result_file: str = ""

    @abstractmethod
    def inject(self, pristine: str, test_code: str) -> tuple[str | None, str]:
        """Proposal code into the pristine tests-file content (PURE).
        Returns (new_content, "") or (None, reason)."""

    @abstractmethod
    def strip_imports(self, test_code: str, pristine: str = "") -> str:
        """Apply the framework's rule for proposal-carried imports before
        injection. `pristine` is the host file, for rules that depend on
        what is already imported there."""

    @abstractmethod
    def test_title(self, test_code: str) -> str | None:
        """The name the runner will report the proposal's test under, or
        None when none can be read (-> broken_test / serial skip)."""

    @abstractmethod
    def mark_title(self, test_code: str, mark: str) -> str | None:
        """Stamp `mark` into the proposal's test name so a batched run can
        be attributed. Returns marked code, or None when no test opener was
        found (the finding then falls back to the serial path)."""

    @abstractmethod
    def serial_command(self, profile, tests_file: str, title: str,
                       out: str) -> list[str]:
        """The runner invocation for ONE titled test, machine-readable
        results written to `out`."""

    @abstractmethod
    def batch_command(self, profile, tests_file: str, mark_prefix: str,
                      out: str) -> list[str]:
        """The runner invocation for every mark-stamped test at once,
        machine-readable results written to `out`."""

    @abstractmethod
    def read_verdict(self, out: str) -> tuple[Status, str]:
        """One serial run's output file -> (status, observed). The verdict
        priority (assertion > timeout > load error > never-ran) must match
        read_batch's records as consumed by the verifier: serial and batch
        may never diverge."""

    @abstractmethod
    def read_batch(self, out: str) -> list[BatchResult] | None:
        """One batched run's output file -> per-test records, or None when
        the output is missing/unreadable (-> nothing attributed, everyone
        falls back to serial)."""

    @abstractmethod
    def classify_failure(self, fm: str) -> tuple[str, str]:
        """One failure message -> (kind, first_line) where kind is
        "assertion" | "timeout" | "load_error". The single shared brain for
        serial and batched classification within this framework."""

    @staticmethod
    @abstractmethod
    def default_tests_for(repo: str, target: str,
                          dir_fallback: bool = True) -> str:
        """The framework's test-file naming conventions: find the tests file
        for a target, or return a best-guess path for a clear error."""


# ---------------------------------------------------------------------------
# vitest
# ---------------------------------------------------------------------------


class VitestHarness(Harness):
    """The vitest spelling of the gate. Every rule in here was learned
    against a real repo (zod, jotai, zustand, supabase/mcp) — the comments
    carry the provenance, moved verbatim from finding_verifier."""

    name = "vitest"
    src_suffixes = (".ts", ".tsx", ".js", ".jsx", ".mjs")
    result_file = "agentboard-finding-result.json"

    def inject(self, pristine: str, test_code: str) -> tuple[str | None, str]:
        """Inject the agent's test into the pristine tests-file content (PURE).

        Works on the pristine content in memory (not the file on disk) so the warm
        base can inject a DIFFERENT test per finding, each starting from a clean file
        — never stacking one finding's test on top of another's.

        Placement is load-bearing, and it is decided by the LAST top-level opener
        (column-0 describe/test/it), not by whether a describe exists anywhere:

        * Last opener is describe -> the file ENDS in a describe block; insert
          before its final column-0 close so the proposal inherits
          describe-scoped helpers (learned against zod, whose tests file is one
          wrapping describe).
        * Last opener is test/it -> the file ends in top-level tests; the final
          `})` closes the LAST TEST, and inserting there nests the proposal
          inside another test's body, where `-t` skipping means it never
          registers at runtime ("name match failed") while a typecheck project
          still "passes" it statically. Append at end of file instead: module
          scope is always legal and module imports/helpers are in scope.

        The second case includes MIXED files — describes early, top-level its at
        the end (jotai's store.test.tsx, found on benchmark row 6, where the old
        "any describe anywhere" routing nested all 10 proposals inside the final
        it). EOF-append was proven against jotai's real environment before this
        rule was written.
        """
        if not test_code:
            return None, "no test supplied"
        code = self.strip_imports(test_code, pristine).rstrip()
        if not code:
            return None, "test contained only imports"
        openers = re.findall(r"^(describe|test|it)\b", pristine, flags=re.M)
        if openers and openers[-1] == "describe":
            tail = pristine.rstrip()
            # Column-0 close, with or without a semicolon: prettier semi:false
            # repos (zustand was the one that surfaced this) end the block with
            # `})` not `});`. Indented closes still never match, so the top-level
            # placement guarantee is unchanged.
            idx = tail.rfind("\n});")
            if idx == -1:
                idx = tail.rfind("\n})")
            if idx == -1:
                return None, "could not find describe-block close to inject before"
            return pristine[:idx] + "\n\n" + code + "\n" + pristine[idx:], ""
        return pristine.rstrip() + "\n\n" + code + "\n", ""

    def strip_imports(self, test_code: str, pristine: str = "") -> str:
        """Remove module-level import statements from proposed test code.

        Proposals are injected INTO an existing tests file, never run standalone —
        and ES imports are only legal at module top level, so a proposal that
        carries its own imports fails the whole file's transform (three findings
        died this way against zod). The harness rule already tells the proposer
        to reuse the host file's imports; stripping enforces it mechanically.
        If a stripped import was genuinely needed, the test fails at runtime with
        a clear ReferenceError — still a correct broken_test, instead of a
        transform failure that poisons the batch.
        """
        out, skipping = [], False
        for line in test_code.splitlines():
            stripped = line.strip()
            if skipping:
                if stripped.endswith(";") or stripped.endswith('"') or stripped.endswith("'"):
                    skipping = False
                continue
            if stripped.startswith("import ") or stripped.startswith("import{"):
                # multi-line import: skip until the closing `from "..."` line
                if not (stripped.endswith(";") or " from " in stripped and
                        (stripped.endswith('";') or stripped.endswith("';")
                         or stripped.endswith('"') or stripped.endswith("'"))):
                    skipping = " from " not in stripped
                continue
            out.append(line)
        return "\n".join(out)

    def test_title(self, test_code: str) -> str | None:
        m = re.search(r"""(?:test|it)\(\s*[`'"](.+?)[`'"]""", test_code or "")
        return m.group(1) if m else None

    def mark_title(self, test_code: str, mark: str) -> str | None:
        # mark the title inside the test(...) opener itself — a naive
        # replace can hit a lookalike (comment, string) elsewhere and
        # leave the real test unmarked -> unattributed -> serial fallback.
        def _stamp(m: re.Match[str]) -> str:
            return m.group(1) + mark + " "

        code, n = re.subn(
            r"""((?:test|it)\(\s*[`'"])""",
            _stamp,
            test_code,
            count=1,
        )
        return code if n else None

    def serial_command(self, profile, tests_file: str, title: str,
                       out: str) -> list[str]:
        # run ONLY the injected test by name, so pre-existing suite failures
        # can never be misattributed to this finding.
        return profile.test_base + [
            "-t", title, "--typecheck.enabled=false",
            "--reporter=json", f"--outputFile={out}",
        ]

    def batch_command(self, profile, tests_file: str, mark_prefix: str,
                      out: str) -> list[str]:
        return profile.test_base + [
            "-t", mark_prefix, "--typecheck.enabled=false",
            "--reporter=json", f"--outputFile={out}",
        ]

    def classify_failure(self, fm: str) -> tuple[str, str]:
        """One failed assertionResult -> (kind, first_line). The single
        shared brain for serial and batched paths — they must never diverge.

        Only a named AssertionError counts as an assertion failure. The old
        heuristic also accepted any message containing "expected" — and
        "Unexpected token", a parse error, contains that substring, so a
        garbage test could mint a confirmed_gap. A runtime crash that is
        really the tool's fault still surfaces (as broken_test, where a human
        reads it); the gate stays conservative because an inflated gap is the
        one error class this design must never produce. vitest's assertion
        layer (@vitest/expect, chai, node:assert) names AssertionError in
        every genuine expect/assert failure."""
        first = fm.strip().splitlines()[0][:200] if fm.strip() else ""
        if "timed out" in fm.lower():
            return "timeout", first
        # vitest 3 serializes its per-test timeout through a stack-donor
        # placeholder: the reported failureMessage is the placeholder's stack,
        # headed "Error: STACK_TRACE_ERROR", and the human timeout message
        # does not survive into the JSON reporter. The placeholder header IS
        # the timeout signal. A user test could only fake it by throwing that
        # exact message, and the misfile would land in timed_out (ambiguity,
        # a human's job) — the conservative direction, never a minted gap.
        if first == "Error: STACK_TRACE_ERROR":
            return "timeout", "test timed out (vitest 3 placeholder serialization)"
        if "AssertionError" in fm:
            return "assertion", first
        return "load_error", first

    def read_verdict(self, out: str) -> tuple[Status, str]:
        if not os.path.isfile(out):
            return "broken_test", "test run produced no JSON output"
        try:
            data = json.loads(open(out, encoding="utf-8").read())
        except Exception as e:  # noqa: BLE001
            return "broken_test", f"could not parse results: {e}"
        # find the newly-added test's result; distinguish assertion-fail from load error
        failed_assertion = None
        load_error = None
        timeout_msg = None
        ran = 0
        for suite in data.get("testResults", []):
            msg = suite.get("message") or ""
            if suite.get("status") == "failed" and not suite.get("assertionResults"):
                load_error = msg  # suite failed to collect -> broken test
            for t in suite.get("assertionResults", []):
                if t.get("status") in ("passed", "failed"):
                    ran += 1
                if t.get("status") == "failed":
                    fm = (t.get("failureMessages") or [""])[0]
                    kind, first = self.classify_failure(fm)
                    if kind == "timeout":
                        timeout_msg = first
                    elif kind == "assertion":
                        failed_assertion = first
                    else:
                        load_error = first
        if failed_assertion:
            return "confirmed_gap", failed_assertion
        if timeout_msg:
            return "timed_out", timeout_msg
        if load_error:
            return "broken_test", load_error
        if ran == 0:
            return "broken_test", "injected test did not run (name match failed)"
        return "handled", "test passed — the tool already does this"

    def read_batch(self, out: str) -> list[BatchResult] | None:
        if not os.path.isfile(out):
            return None
        try:
            with open(out, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001
            return None
        results: list[BatchResult] = []
        for suite in data.get("testResults", []):
            for t in suite.get("assertionResults", []):
                results.append(BatchResult(
                    title=t.get("title") or t.get("fullName") or "",
                    status=t.get("status") or "",
                    failure=(t.get("failureMessages") or [""])[0],
                ))
        return results

    @staticmethod
    def default_tests_for(repo: str, target: str,
                          dir_fallback: bool = True) -> str:
        """Find the tests file for a target. Tries, in order: co-located
        (foo.test.ts), the same basename under any tests dir, and a
        singular/plural basename variant (errors.ts <-> error.test.ts, which is
        exactly the shape that tripped up the first real run on zod). Returns ""
        if nothing unambiguous is found — the caller then asks for --tests."""
        import glob as _glob

        for suffix in VitestHarness.src_suffixes:
            if not target.endswith(suffix):
                continue
            stem = target[: -len(suffix)]
            base = os.path.basename(stem)

            # 1. co-located: src/foo.ts -> src/foo.test.ts
            colocated = f"{stem}.test{suffix}"
            if os.path.isfile(os.path.join(repo, colocated)):
                return colocated

            # 2 & 3. search test dirs for <base>.test.<ext>, then singular/plural
            variants = [base]
            if base.endswith("s"):
                variants.append(base[:-1])       # errors -> error
            else:
                variants.append(base + "s")      # error  -> errors
            target_dir = os.path.dirname(os.path.join(repo, target))
            for name in variants:
                hits: list[str] = []
                for pat in (
                    f"**/tests/**/{name}.test{suffix}",
                    f"**/__tests__/**/{name}.test{suffix}",
                    f"**/test/**/{name}.test{suffix}",
                    f"**/{name}.test{suffix}",
                ):
                    hits += _glob.glob(os.path.join(repo, pat), recursive=True)
                hits = sorted({h for h in hits if "node_modules" not in h})
                if len(hits) == 1:
                    return os.path.relpath(hits[0], repo)
                if len(hits) > 1:
                    # ambiguous — pick the file sharing the longest directory
                    # prefix with the target (closest in the monorepo tree)
                    def _shared(h: str) -> int:
                        return len(os.path.commonpath([target_dir, os.path.dirname(h)]))
                    best = max(hits, key=_shared)
                    # only accept if it's meaningfully close (shares more than repo root)
                    if _shared(best) > len(repo):
                        return os.path.relpath(best, repo)

            # 4. sole test file in the target's own directory. Covers the common
            # one-suite-per-module-directory layout that neither co-location nor
            # basename matching reaches (agentboard's own demo fixture: a
            # directory holding order_tool.js and demo.test.js). "Exactly one"
            # is the guard: with two or more there is nothing to infer, so we
            # fall through to asking for --tests rather than guessing.
            #
            # Only for an explicitly named --target (dir_fallback=True). Files
            # added automatically (--also, blast-radius scoping) must find a
            # real match or be skipped: inferring at scale is how twenty
            # unrelated files end up gated against one suite.
            siblings: list[str] = []
            if not dir_fallback:
                return colocated
            for sfx in VitestHarness.src_suffixes:
                for pat in (f"*.test{sfx}", f"*.spec{sfx}"):
                    siblings += _glob.glob(os.path.join(target_dir, pat))
            siblings = sorted({s for s in siblings if "node_modules" not in s})
            if len(siblings) == 1:
                return os.path.relpath(siblings[0], repo)

            return colocated  # fall back to the co-located name for a clear error
        return ""


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


def harness_for_profile(profile) -> Harness:
    """The harness a profile's framework calls for. RepoProfile.kind is
    "pytest" for python repos (set by config.build_profile) and "vitest"
    otherwise; the default keeps every existing profile — including ones
    built by hand in tests — on the vitest path unchanged."""
    if getattr(profile, "kind", "") == "pytest":
        from .pytest_harness import PytestHarness
        return PytestHarness()
    return VitestHarness()


def harness_for_target(target: str) -> Harness | None:
    """The harness whose source-file conventions cover `target`, or None
    when no framework claims the extension (the caller then asks for
    --tests explicitly, exactly as before). Imports are local so the
    harness registry cannot create an import cycle."""
    from .pytest_harness import PytestHarness
    for cls in (VitestHarness, PytestHarness):
        if target.endswith(cls.src_suffixes):
            return cls()
    return None
