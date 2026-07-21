"""The pytest spelling of the gate — same semantics, Python runtime.

Everything FindingVerifier believes still holds here: only a test that
actually runs and fails its assertion is a gap; a crash is the test's
problem; a hang is a human's problem. This module only answers the
framework questions for pytest:

  - injection is EOF-append at module level. Python has no describe-block
    scoping problem: module scope is always legal, host imports and helpers
    are in scope, and a later `def` never nests inside an earlier one, so
    the whole placement decision tree the vitest harness needs collapses to
    one rule.
  - proposal imports are KEPT, not stripped. Unlike ES modules, a Python
    import is legal at the injection point, and a proposal often genuinely
    needs one the host lacks (`from order_tool import clamp_page_size`).
    Only exact duplicates of lines the host already has are dropped.
  - results come from pytest's junit XML (--junit-xml), parsed with stdlib
    ElementTree — machine-readable without adding a dependency. The JSON
    plugins would be nicer; they would also be a new runtime dep, which the
    gate does not take.
  - serial runs select by NODE ID (file::name), not by -k expression: a
    node id matches exactly one collected test, so a name that happens to
    be a substring of a pre-existing test can never drag that test's
    failure into this finding's verdict. Batch runs use -k on the gate's
    injected marks, mirroring the vitest -t filter.
  - timeouts are the SUBPROCESS limit's job. pytest has no per-test
    timeout without a plugin (a new dep), so a hanging proposal times out
    the batch, everyone falls back to serial, and the hang times out its
    own serial run -> timed_out. Slower than vitest's in-runner timeout,
    same verdict, same determinism.

Classification is the conservative rule, pytest edition: only a failure
whose report names AssertionError counts as an assertion. pytest's assert
rewriting raises AssertionError for every bare `assert`, and the junit
longrepr ends with the exception's name, so both `assert x == y` and an
explicit raise qualify. A NameError/ImportError/collection error never
does. Known one-way cost: `pytest.fail(...)` and a pytest.raises that DID
NOT RAISE report pytest's own Failed exception, not AssertionError, so
they land in broken_test — a missed gap, never a minted one, which is the
direction this gate is allowed to be wrong in.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET

from ..review import Status
from .harness import BatchResult, Harness


class PytestHarness(Harness):
    name = "pytest"
    src_suffixes = (".py",)
    result_file = "agentboard-finding-result.xml"

    # a test opener: module-level or indented `def test_*(` / `async def`.
    # The name is what pytest collects and what the junit `name` attr reports.
    _DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(", re.M)

    # ---- injection ---------------------------------------------------------

    def inject(self, pristine: str, test_code: str) -> tuple[str | None, str]:
        """EOF-append at module level (PURE, mirrors the vitest contract).
        Two blank lines keep the file PEP8-shaped; nothing depends on it."""
        if not test_code:
            return None, "no test supplied"
        code = self.strip_imports(test_code, pristine).rstrip()
        if not code:
            return None, "test contained only imports"
        return pristine.rstrip() + "\n\n\n" + code + "\n", ""

    def strip_imports(self, test_code: str, pristine: str = "") -> str:
        """Drop only imports the host file already has (exact line match).

        Duplicated imports are legal in Python but noisy; imports the host
        LACKS are kept because they are legal at the injection point and a
        proposal may genuinely need them. Multi-line `from x import (...)`
        blocks are left untouched for the same reason."""
        have = {
            ln.strip() for ln in pristine.splitlines()
            if ln.strip().startswith(("import ", "from "))
        }
        out = []
        for line in test_code.splitlines():
            s = line.strip()
            if s.startswith(("import ", "from ")) and s in have:
                continue
            out.append(line)
        return "\n".join(out)

    # ---- naming ------------------------------------------------------------

    def test_title(self, test_code: str) -> str | None:
        m = self._DEF_RE.search(test_code or "")
        return m.group(1) if m else None

    def mark_title(self, test_code: str, mark: str) -> str | None:
        # stamp the mark into the function NAME (test_x -> test_x___ab0___):
        # the junit `name` attribute carries it, `-k` can filter on it, and
        # it cannot collide with a host test's name.
        code, n = self._DEF_RE.subn(
            lambda m: m.group(0).replace(m.group(1), m.group(1) + mark, 1),
            test_code,
            count=1,
        )
        return code if n else None

    # ---- run commands ------------------------------------------------------

    def serial_command(self, profile, tests_file: str, title: str,
                       out: str) -> list[str]:
        # node id, not -k: exactly one collected test can match, so a
        # pre-existing failure can never be misattributed to this finding.
        return profile.test_base + [
            f"{tests_file}::{title}", "-q", f"--junit-xml={out}",
        ]

    def batch_command(self, profile, tests_file: str, mark_prefix: str,
                      out: str) -> list[str]:
        # -k on the mark: only gate-stamped tests run, mirroring vitest -t.
        return profile.test_base + [
            tests_file, "-k", mark_prefix, "-q", f"--junit-xml={out}",
        ]

    # ---- reading results ---------------------------------------------------

    @staticmethod
    def _case(tc) -> tuple[str, str]:
        """One junit <testcase> -> (status, failure_text).

        status is "passed" | "failed" | "error" | "skipped". An <error>
        child is collection/setup breakage — the test never properly ran,
        so serial/verdict logic treats it as test breakage, never as a gap.
        failure_text is the message attribute plus the longrepr body: the
        message leads with the human line, the body ends with the exception
        name the classifier keys on."""
        for child in tc:
            if child.tag in ("failure", "error", "skipped"):
                fm = (child.get("message") or "")
                if child.text:
                    fm = fm + "\n" + child.text
                return ("failed" if child.tag == "failure" else child.tag), fm
        return "passed", ""

    def classify_failure(self, fm: str) -> tuple[str, str]:
        """One failure report -> (kind, first_line). Same conservative brain
        as the vitest harness: only a named AssertionError is an assertion.
        pytest's rewritten `assert` qualifies because the junit longrepr's
        final traceback line names AssertionError; a NameError or crash
        names itself instead and stays the test's problem."""
        first = fm.strip().splitlines()[0][:200] if fm.strip() else ""
        if "timed out" in fm.lower() or first.startswith("Failed: Timeout"):
            # no per-test timeouts without a plugin, but if one IS installed
            # in the repo's env, its verdict routes to ambiguity, not a gap.
            return "timeout", first
        if "AssertionError" in fm:
            return "assertion", first
        return "load_error", first

    def read_verdict(self, out: str) -> tuple[Status, str]:
        if not os.path.isfile(out):
            return "broken_test", "test run produced no junit XML output"
        try:
            root = ET.parse(out).getroot()
        except Exception as e:  # noqa: BLE001
            return "broken_test", f"could not parse results: {e}"
        failed_assertion = None
        load_error = None
        timeout_msg = None
        skipped_msg = None
        ran = 0
        for tc in root.iter("testcase"):
            status, fm = self._case(tc)
            if status == "error":
                # collection failure / setup error: the test never ran
                load_error = fm.strip().splitlines()[0][:200] if fm.strip() else ""
                continue
            if status == "skipped":
                # A skipped injected test verified nothing. The verdict is
                # broken_test either way (skipping can never mint a gap),
                # but say what actually happened: the first self-review run
                # flagged the old "name match failed" message as misleading
                # for this case.
                skipped_msg = fm.strip().splitlines()[0][:200] if fm.strip() else ""
                continue
            if status in ("passed", "failed"):
                ran += 1
            if status == "failed":
                kind, first = self.classify_failure(fm)
                if kind == "timeout":
                    timeout_msg = first
                elif kind == "assertion":
                    failed_assertion = first
                else:
                    load_error = first
        # same verdict priority as every harness: the strongest evidence
        # wins, and an empty run means the selection failed, not the tool.
        if failed_assertion:
            return "confirmed_gap", failed_assertion
        if timeout_msg:
            return "timed_out", timeout_msg
        if load_error:
            return "broken_test", load_error
        if ran == 0:
            if skipped_msg is not None:
                return ("broken_test",
                        "injected test was skipped, nothing verified"
                        + (": " + skipped_msg if skipped_msg else ""))
            return "broken_test", "injected test did not run (name match failed)"
        return "handled", "test passed — the tool already does this"

    def read_batch(self, out: str) -> list[BatchResult] | None:
        if not os.path.isfile(out):
            return None
        try:
            root = ET.parse(out).getroot()
        except Exception:  # noqa: BLE001
            return None
        results: list[BatchResult] = []
        for tc in root.iter("testcase"):
            status, fm = self._case(tc)
            # "error"/"skipped" records pass through with their own status:
            # the verifier counts only passed/failed as "ran", so an errored
            # marked test falls back to the serial path, same as vitest.
            results.append(BatchResult(
                title=tc.get("name") or "",
                status=status,
                failure=fm,
            ))
        return results

    # ---- discovery ---------------------------------------------------------

    @staticmethod
    def default_tests_for(repo: str, target: str,
                          dir_fallback: bool = True) -> str:
        """Python test-file conventions: test_foo.py or foo_test.py,
        co-located or under a tests/ dir. Returns the co-located test_ name
        when nothing is found (a clear file-not-found error later), the same
        contract as the vitest harness."""
        import glob as _glob

        if not target.endswith(".py"):
            return ""
        base = os.path.basename(target)[: -len(".py")]
        target_dir = os.path.dirname(os.path.join(repo, target))
        names = (f"test_{base}.py", f"{base}_test.py")

        # 1. co-located: pkg/foo.py -> pkg/test_foo.py or pkg/foo_test.py
        colocated = os.path.join(os.path.dirname(target), names[0])
        for name in names:
            cand = os.path.join(os.path.dirname(target), name)
            if os.path.isfile(os.path.join(repo, cand)):
                return cand

        def _clean(hits: list[str]) -> list[str]:
            # prune dot-dirs (.venv, .tox) and vendored trees — a hit inside
            # them is never the repo's own suite.
            return sorted({
                h for h in hits
                if "node_modules" not in h
                and not any(p.startswith(".")
                            for p in os.path.relpath(h, repo).split(os.sep))
            })

        # 2. the same basename under any tests dir, then anywhere; a unique
        # hit wins, ambiguity picks the closest (longest shared dir prefix
        # with the target), same tie-break the vitest harness earned on zod.
        for name in names:
            hits: list[str] = []
            for pat in (f"**/tests/**/{name}", f"**/test/**/{name}",
                        f"**/{name}"):
                hits += _glob.glob(os.path.join(repo, pat), recursive=True)
            hits = _clean(hits)
            if len(hits) == 1:
                return os.path.relpath(hits[0], repo)
            if len(hits) > 1:
                def _shared(h: str) -> int:
                    return len(os.path.commonpath([target_dir,
                                                   os.path.dirname(h)]))
                best = max(hits, key=_shared)
                if _shared(best) > len(repo):
                    return os.path.relpath(best, repo)

        # 3. sole test file in the target's own directory (the
        # one-suite-per-module-directory layout). Explicit targets only,
        # same guard and same reasoning as the vitest harness.
        if not dir_fallback:
            return colocated
        siblings = _clean(
            _glob.glob(os.path.join(target_dir, "test_*.py"))
            + _glob.glob(os.path.join(target_dir, "*_test.py"))
        )
        if len(siblings) == 1:
            return os.path.relpath(siblings[0], repo)

        return colocated  # co-located name for a clear error
