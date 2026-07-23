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

import ast
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
            if (s.startswith(("import ", "from ")) and s in have
                    and not line[:1].isspace()):
                # column-0 duplicates only: an INDENTED import lives inside
                # a test body, and deleting it rewrites the proposal — the
                # gate proved this mutates test bodies (run ff36b2226dfda3eb)
                continue
            out.append(line)
        return "\n".join(out)

    # ---- naming ------------------------------------------------------------

    @staticmethod
    def _test_node(test_code: str):
        """The REAL test function in a proposal, found by parsing, not by
        regex over raw text: docstring example code that looks like a test
        def cannot be selected (the gate proved the regex could be fooled,
        run ff36b2226dfda3eb). Returns (class_name_or_None, fn_name,
        def_lineno) for the first test_* function, honoring class nesting
        so serial node ids select the exact collected testcase."""
        try:
            tree = ast.parse(test_code or "")
        except SyntaxError:
            return None
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))                     and node.name.startswith("test_"):
                return None, node.name, node.lineno
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef,
                                        ast.AsyncFunctionDef))                             and sub.name.startswith("test_"):
                        return node.name, sub.name, sub.lineno
        return None

    def test_title(self, test_code: str) -> str | None:
        found = self._test_node(test_code)
        if found:
            cls, name, _ = found
            # class-qualified so serial_command's {file}::{title} composes
            # into the exact pytest node id, TestX::test_y included
            return f"{cls}::{name}" if cls else name
        m = self._DEF_RE.search(test_code or "")  # unparsable: old behavior
        return m.group(1) if m else None

    def mark_title(self, test_code: str, mark: str) -> str | None:
        # stamp the mark into the function NAME (test_x -> test_x___ab0___):
        # the junit `name` attribute carries it, `-k` can filter on it, and
        # it cannot collide with a host test's name. The def line comes
        # from the ast, so docstring look-alikes are never stamped.
        found = self._test_node(test_code)
        if found:
            _, name, lineno = found
            lines = test_code.splitlines()
            i = lineno - 1
            if 0 <= i < len(lines) and name in lines[i]:
                lines[i] = lines[i].replace(name, name + mark, 1)
                return "\n".join(lines)
        code, n = self._DEF_RE.subn(  # unparsable: old behavior
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

    # Lines that ARE a raised exception, not lines that merely mention
    # one. Two shapes qualify: a raising line ("E   AssertionError: ...",
    # "NameError: name 'x' is not defined" — colon or end-of-line required
    # so prose and quoted source never match) and pytest's longrepr
    # location tail ("test_s.py:5: AssertionError"), which is the ONLY
    # place a bare rewritten `assert` names AssertionError at all.
    _EXC_LINE = re.compile(
        r"^(?:E\s+)?"
        r"([A-Za-z_][\w.]*(?:Error|Exception)|Failed|KeyboardInterrupt)"
        r"(?::|$)")
    _EXC_TAIL = re.compile(
        r"^\S+:\d+:\s+"
        r"([A-Za-z_][\w.]*(?:Error|Exception)|Failed|KeyboardInterrupt)$")

    def failure_headline(self, fm: str) -> str:
        """The line a human needs FIRST: the exception actually raised
        (the last raising-position line in the report), else the report's
        first line. Junit setup errors lead with the coordinate ("failed
        on setup with 'file X, line N'") while the ImportError that
        explains everything sits buried below — the gate's own boards
        proved how unreadable that is at scale."""
        best = ""
        for line in fm.splitlines():
            st = line.strip()
            if self._EXC_LINE.match(st) or self._EXC_TAIL.match(st):
                best = st
        if best:
            return re.sub(r"^E\s+", "", best)[:200]
        return fm.strip().splitlines()[0][:200] if fm.strip() else ""

    def classify_failure(self, fm: str) -> tuple[str, str]:
        """One failure report -> (kind, first_line). Only the RAISED
        exception decides, read from the last line that names one in
        raising position. Substring matching over the whole report was the
        original brain here, and the gate itself broke it (run
        94669614e770c539): a NameError whose traceback contained the text
        "AssertionError" was classified as an assertion gap, and an
        assertion whose text mentioned timeouts was classified as a
        timeout. A crash mentioning an exception is not that exception.
        Unrecognizable reports classify as load_error, never assertion:
        fabricating a gap is the one mistake this function must not make."""
        first = fm.strip().splitlines()[0][:200] if fm.strip() else ""
        exc = ""
        payload = ""
        for line in fm.splitlines():
            stripped = line.strip()
            m = self._EXC_LINE.match(stripped) or self._EXC_TAIL.match(stripped)
            if m:
                exc = m.group(1)
                payload = stripped
        # ORDER MATTERS, twice proven by the gate on itself: an identified
        # AssertionError wins outright — its human message may legitimately
        # talk about timeouts (run ff36b2226dfda3eb caught the first-line
        # heuristic firing before the exception check). Timeout wording is
        # consulted only on the exception's own line, or, when no exception
        # was identified at all, on the first line (runner-generated
        # reports like "Error: Test timed out in 5000ms" have no traceback).
        if exc == "AssertionError" or exc.endswith(".AssertionError"):
            return "assertion", first
        if exc == "Failed" and "Timeout" in payload:
            # pytest-timeout raises Failed: Timeout >Ns; plugin verdicts
            # route to ambiguity, not to a gap.
            return "timeout", first
        if exc.endswith("TimeoutError"):
            return "timeout", first
        if exc and "timed out" in payload.lower():
            return "timeout", first
        if not exc and ("timed out" in first.lower()
                        or first.startswith("Failed: Timeout")):
            return "timeout", first
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
                # collection failure / setup error: the test never ran.
                # Cause first: the raised exception, not the coordinate.
                load_error = self.failure_headline(fm)
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
                    load_error = self.failure_headline(fm)
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
        if base == "__init__":
            # a package's behavior is imported by the package's name, and
            # test___init__.py is a file nobody has ever written on purpose
            base = os.path.basename(os.path.dirname(target)) or base
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

        # 2.5. import matching, ported from the self-review workflow's
        # picker where it earned its keep: repos that name tests by
        # behavior (test_config_preflight.py for config.py) break every
        # basename rule, but the import statement is the real link. A
        # test-shaped file that imports the target's module IS its suite.
        # Selection is deterministic: unique hit wins; several hits prefer
        # filenames containing the stem; still several, closest shared
        # dir prefix; final tie, first in sorted order.
        import ast as _ast

        def _really_imports(path: str, stem: str) -> bool:
            # the AST, not the text: docstrings, comments, and quoted
            # examples cannot fabricate a test relationship (the gate
            # proved the regex version could be fooled by exactly those),
            # and multiline aliased from-imports are seen like any other.
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    tree = _ast.parse(fh.read())
            except (OSError, SyntaxError, ValueError):
                return False
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Import):
                    if any(stem in a.name.split(".") for a in node.names):
                        return True
                elif isinstance(node, _ast.ImportFrom):
                    if stem in (node.module or "").split("."):
                        return True
                    if any(a.name == stem for a in node.names):
                        return True
            return False

        candidates = _clean(
            _glob.glob(os.path.join(repo, "**", "test_*.py"), recursive=True)
            + _glob.glob(os.path.join(repo, "**", "*_test.py"), recursive=True)
        )
        importers = [c for c in candidates if _really_imports(c, base)]
        if importers:
            named = [h for h in importers
                     if base in os.path.basename(h)]
            pool = named or importers
            if len(pool) == 1:
                return os.path.relpath(pool[0], repo)

            def _shared_i(h: str) -> int:
                return len(os.path.commonpath([target_dir,
                                               os.path.dirname(h)]))
            best = max(pool, key=_shared_i)
            if _shared_i(best) > len(repo):
                return os.path.relpath(best, repo)
            return os.path.relpath(sorted(pool)[0], repo)

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
