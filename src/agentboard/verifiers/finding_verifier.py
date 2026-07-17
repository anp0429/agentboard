"""FindingVerifier — the deterministic judge for review findings.

A reviewer agent writes a test asserting some behavior the intent implies. This
runs that test against the branch and classifies the result. The classification
is the whole trust anchor, because a red test is ambiguous on its own:

    - PASS                 -> the tool already does the right thing  -> "handled"
    - ASSERTION failure    -> the tool did the WRONG thing           -> "confirmed_gap"
    - compile/load/crash   -> the TEST is broken, not the tool       -> "broken_test"
    - did not finish       -> nobody knows yet                       -> "timed_out"

The fourth bucket is deliberately NOT auto-resolved: a timeout is ambiguous
evidence (slow test? hung tool? starved sandbox?) and resolving ambiguity is
the human's job. The gate reports the limit it hit and stops. No retries,
no guessing — the board is where a person decides.

That third bucket is what keeps a red result meaningful: a model that writes a
garbage test must NOT be able to manufacture a "gap." Only a test that actually
runs and fails its assertion counts. No LLM is in this decision.

Note the honest ceiling: a confirmed_gap means the tool violated a *stated*
assertion that compiled and ran. It does NOT prove the assertion itself is the
*right* thing to assert — judging that is the second agent / human layer. This
gate confirms "the test is real and the tool fails it," nothing more.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from ..review import ReviewFinding, ReviewRun
from .vitest_verifier import RepoProfile, _tail


def _inject(pristine: str, test_code: str) -> tuple[str | None, str]:
    """Inject the agent's test into the pristine tests-file content (PURE).

    Works on the pristine content in memory (not the file on disk) so the warm
    base can inject a DIFFERENT test per finding, each starting from a clean file
    — never stacking one finding's test on top of another's.

    Placement is load-bearing (learned against zod): if the file wraps its
    tests in a describe block, insert before its final `});` so the new test
    inherits describe-scoped helpers. But if the file is TOP-LEVEL test()
    calls, that same `});` closes the LAST TEST — inserting there nests the
    proposal inside another test's body, where `-t` skipping means it never
    registers at runtime while a typecheck project still "passes" it
    statically. For describe-less files, append at end of file: module
    imports are inherited there regardless.
    """
    if not test_code:
        return None, "no test supplied"
    code = _strip_imports(test_code).rstrip()
    if not code:
        return None, "test contained only imports"
    if "\ndescribe(" in pristine or pristine.startswith("describe("):
        idx = pristine.rstrip().rfind("\n});")
        if idx == -1:
            return None, "could not find describe-block close to inject before"
        return pristine[:idx] + "\n\n" + code + "\n" + pristine[idx:], ""
    return pristine.rstrip() + "\n\n" + code + "\n", ""




def _strip_imports(test_code: str) -> str:
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


_COPY_IGNORES = {".git", "node_modules", "dist", "__pycache__"}


def _copy_discrepancies(src: str, dst: str, limit: int = 5) -> list[str]:
    """Compare two trees (pruning _COPY_IGNORES) by relative path and size.

    The warm sandbox is only trustworthy if it IS the repo. A copy step that
    silently drops a config file is non-determinism entering through the
    operational layer — the verdict would depend on which files survived the
    copy. This is a pure function so it can be tested without a sandbox.
    Returns up to `limit` human-readable discrepancies; empty means faithful.
    """

    def walk(root: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for cur, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _COPY_IGNORES]
            for name in files:
                full = os.path.join(cur, name)
                rel = os.path.relpath(full, root)
                try:
                    out[rel] = os.path.getsize(full)
                except OSError:
                    out[rel] = -1
        return out

    a, b = walk(src), walk(dst)
    diffs: list[str] = []
    for rel in sorted(set(a) | set(b)):
        if rel not in b:
            diffs.append(f"missing from sandbox: {rel}")
        elif rel not in a:
            diffs.append(f"extra in sandbox: {rel}")
        elif a[rel] != b[rel] and -1 not in (a[rel], b[rel]):
            diffs.append(f"size mismatch: {rel} ({a[rel]} -> {b[rel]} bytes)")
        if len(diffs) >= limit:
            diffs.append("...")
            break
    return diffs


def _test_title(test_code: str) -> str | None:
    m = re.search(r"""(?:test|it)\(\s*[`'"](.+?)[`'"]""", test_code or "")
    return m.group(1) if m else None


class FindingVerifier:
    _RESULT = "agentboard-finding-result.json"

    def __init__(
        self,
        repo_root: str,
        profile: RepoProfile,
        tests_file: str,
        timeout: int = 1800,
        reuse_warm: bool = False,
    ):
        self.repo_root = repo_root
        self.profile = profile
        self.tests_file = (
            tests_file  # where agent tests get injected (helpers in scope)
        )
        self.timeout = timeout
        self.reuse_warm = (
            reuse_warm  # keep the warm base across run() calls (for N runs)
        )
        # warm-base state (built once, reused per finding)
        self._warm_repo: str | None = None
        self._warm_root: str | None = None
        self._pristine_tests: str | None = None
        self._prep_error: str = ""

    def _run(self, args, cwd):
        env = {**os.environ, **self.profile.env}
        return subprocess.run(
            args, cwd=cwd, env=env, capture_output=True, text=True, timeout=self.timeout
        )

    # -- warm base: copy + install + build ONCE ------------------------------
    def _ensure_warm(self) -> None:
        """Build the warm base if it doesn't exist: one copy, one install, one
        build. Every finding reuses this node_modules; only the tests file
        changes per finding. This is the whole perf win — install/build stop
        being per-finding and become per-run (or, with reuse_warm, per-session).
        """
        if self._warm_repo is not None:
            return
        self._warm_root = tempfile.mkdtemp(prefix="agentboard_warm_")
        repo = os.path.join(self._warm_root, "repo")
        phases: list[str] = []
        t0 = time.monotonic()
        shutil.copytree(
            self.repo_root,
            repo,
            ignore=shutil.ignore_patterns(
                ".git", "node_modules", "dist", "__pycache__"
            ),
        )
        phases.append(f"copy {time.monotonic() - t0:.1f}s")
        t0 = time.monotonic()
        # the sandbox must BE the repo — a dropped file here would make
        # verdicts depend on copy luck. Fail loudly, never review a ghost.
        diffs = _copy_discrepancies(self.repo_root, repo)
        phases.append(f"fidelity {time.monotonic() - t0:.1f}s")
        if diffs:
            self._prep_error = "sandbox fidelity check failed: " + "; ".join(diffs)
            self._warm_repo = repo
            return
        # capture the pristine tests file ONCE, before any injection
        tpath = os.path.join(repo, self.tests_file)
        if not os.path.isfile(tpath):
            self._prep_error = f"tests file not found: {self.tests_file}"
            self._warm_repo = repo
            return
        with open(tpath, encoding="utf-8") as f:
            self._pristine_tests = f.read()
        # install + build ONCE
        t0 = time.monotonic()
        inst = self._run(self.profile.install_cmd, repo)
        phases.append(f"install {time.monotonic() - t0:.1f}s")
        if inst.returncode != 0:
            self._prep_error = f"install failed: {_tail(inst.stderr or inst.stdout)}"
        elif self.profile.build_cmd:
            t0 = time.monotonic()
            bld = self._run(self.profile.build_cmd, repo)
            phases.append(f"build {time.monotonic() - t0:.1f}s")
            if bld.returncode != 0:
                self._prep_error = f"build failed: {_tail(bld.stderr or bld.stdout)}"
        # functional smoke probe: prove the runner starts before judging
        # anything. An exit code can lie across toolchain versions; a probe
        # that actually launches the runner cannot.
        if not self._prep_error and getattr(self.profile, "smoke_cmd", None):
            t0 = time.monotonic()
            try:
                smoke = self._run(self.profile.smoke_cmd, repo)
                phases.append(f"smoke {time.monotonic() - t0:.1f}s")
                if smoke.returncode != 0:
                    self._prep_error = (
                        "environment smoke probe failed: "
                        f"{_tail(smoke.stderr or smoke.stdout)}"
                    )
            except subprocess.TimeoutExpired:
                self._prep_error = (
                    f"environment smoke probe did not finish within {self.timeout}s"
                )
        print("  warm base: " + ", ".join(phases))
        self._warm_repo = repo

    def close(self) -> None:
        """Delete the warm base. Called at the end of run() unless reuse_warm."""
        if self._warm_root:
            shutil.rmtree(self._warm_root, ignore_errors=True)
        self._warm_root = self._warm_repo = self._pristine_tests = None
        self._prep_error = ""

    def classify(self, finding: ReviewFinding) -> ReviewFinding:
        """Inject this finding's test into the warm base's pristine tests file
        and run ONLY it. Reuses the shared node_modules; resets the tests file to
        pristine first so no finding sees another's injected test."""
        if finding.covered_by_existing:
            finding.status = "skipped_covered"
            return finding
        self._ensure_warm()
        if self._prep_error:  # install/build failed -> nothing can run
            finding.status = "broken_test"
            finding.observed = self._prep_error
            return finding
        title = _test_title(finding.test_code)
        if not title:
            finding.status = "broken_test"
            finding.observed = "could not read test name"
            return finding
        injected, err = _inject(self._pristine_tests or "", finding.test_code)
        if injected is None:
            finding.status = "broken_test"
            finding.observed = err
            return finding
        repo = self._warm_repo
        tpath = os.path.join(repo, self.tests_file)
        try:
            # write pristine + THIS finding's test (clean start every time)
            with open(tpath, "w", encoding="utf-8") as f:
                f.write(injected)
            out = os.path.join(repo, self._RESULT)
            # run ONLY the injected test by name, so pre-existing suite failures
            # can never be misattributed to this finding.
            try:
                self._run(
                    self.profile.test_base
                    + ["-t", title, "--typecheck.enabled=false",
                       "--reporter=json", f"--outputFile={out}"],
                    repo,
                )
            except subprocess.TimeoutExpired:
                finding.status = "timed_out"
                finding.observed = (
                    f"did not finish within {self.timeout}s (subprocess limit)"
                )
                return finding
            finding.status, finding.observed = self._read(out)
            return finding
        finally:
            # restore pristine so the base is clean for the next finding/run
            if self._pristine_tests is not None:
                with open(tpath, "w", encoding="utf-8") as f:
                    f.write(self._pristine_tests)

    @staticmethod
    def _classify_failure(fm: str) -> tuple[str, str]:
        """One failed assertionResult -> (kind, first_line). The single
        shared brain for serial and batched paths — they must never diverge."""
        first = fm.strip().splitlines()[0][:200] if fm.strip() else ""
        if "timed out" in fm.lower():
            return "timeout", first
        if "AssertionError" in fm or "expected" in fm.lower():
            return "assertion", first
        return "load_error", first

    @staticmethod
    def _read(out: str) -> tuple[str, str]:
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
                    kind, first = FindingVerifier._classify_failure(fm)
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

    # -- batched gate ---------------------------------------------------------

    _MARK = "___ab{i}___"

    def _classify_batch(self, findings: list[ReviewFinding]) -> set[int]:
        """Inject every finding's test at once (uniquely marked titles), run
        vitest ONCE filtered to the mark, attribute results per finding.

        Returns the indexes it could NOT confidently attribute — the caller
        re-runs those through the proven serial path. Batch is an
        optimization layer; serial stays the verdict authority for anything
        ambiguous. A defective proposal that breaks collection of the whole
        file therefore poisons nothing: everyone falls back.
        """
        pending = {
            i: f for i, f in enumerate(findings)
            if not f.covered_by_existing
        }
        if not pending:
            return set()
        self._ensure_warm()
        if self._prep_error:
            for f in pending.values():
                f.status = "broken_test"
                f.observed = self._prep_error
            return set()

        content = self._pristine_tests or ""
        marked: dict[int, str] = {}
        for i, f in pending.items():
            title = _test_title(f.test_code)
            if not title:
                continue  # serial path will report it properly
            mark = self._MARK.format(i=i)
            # mark the title inside the test(...) opener itself — a naive
            # replace can hit a lookalike (comment, string) elsewhere and
            # leave the real test unmarked -> unattributed -> serial fallback
            code, n = re.subn(
                r"""((?:test|it)\(\s*[`'"])""",
                lambda m: m.group(1) + mark + " ",
                f.test_code,
                count=1,
            )
            if not n:
                continue
            injected, _err = _inject(content, code)
            if injected is None:
                continue
            content, marked[i] = injected, mark
        if not marked:
            return set(pending)

        repo = self._warm_repo
        tpath = os.path.join(repo, self.tests_file)
        out = os.path.join(repo, self._RESULT)
        try:
            with open(tpath, "w", encoding="utf-8") as fh:
                fh.write(content)
            try:
                self._run(
                    self.profile.test_base
                    + ["-t", "___ab", "--typecheck.enabled=false",
                       "--reporter=json", f"--outputFile={out}"],
                    repo,
                )
            except subprocess.TimeoutExpired:
                # the BATCH hit the subprocess limit — which test hung is
                # unknown, so nobody gets a batched verdict. Serial decides.
                return set(pending)
            attributed = self._attribute(out, marked, pending)
            return set(pending) - attributed
        finally:
            if self._pristine_tests is not None:
                with open(tpath, "w", encoding="utf-8") as fh:
                    fh.write(self._pristine_tests)

    def _attribute(
        self,
        out: str,
        marked: dict[int, str],
        pending: dict[int, ReviewFinding],
    ) -> set[int]:
        """Map batched results back to findings. Only a finding whose marked
        test demonstrably RAN gets a verdict here; everything else is left
        for serial. Verdict logic is the same shared brain as serial."""
        if not os.path.isfile(out):
            return set()
        try:
            with open(out, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001
            return set()
        # collect every assertion whose title carries one of our marks
        per: dict[int, list[dict]] = {}
        for suite in data.get("testResults", []):
            for t in suite.get("assertionResults", []):
                title = t.get("title") or t.get("fullName") or ""
                for i, mark in marked.items():
                    if mark in title:
                        per.setdefault(i, []).append(t)
        done: set[int] = set()
        for i, results in per.items():
            f = pending[i]
            gap = timeout = load = None
            ran = 0
            for t in results:
                if t.get("status") in ("passed", "failed"):
                    ran += 1
                if t.get("status") == "failed":
                    fm = (t.get("failureMessages") or [""])[0]
                    kind, first = self._classify_failure(fm)
                    if kind == "timeout":
                        timeout = first
                    elif kind == "assertion":
                        gap = first
                    else:
                        load = first
            if not ran:
                continue  # never ran -> serial decides
            if gap:
                f.status, f.observed = "confirmed_gap", gap
            elif timeout:
                f.status, f.observed = "timed_out", timeout
            elif load:
                f.status, f.observed = "broken_test", load
            else:
                f.status, f.observed = (
                    "handled", "test passed — the tool already does this"
                )
            done.add(i)
        return done

    def run(self, review: ReviewRun, batch: bool = True) -> ReviewRun:
        """Classify all findings against one warm base. With batch=True the
        gate runs ONE test invocation and serial-fallbacks anything it could
        not confidently attribute; batch=False is the original per-finding
        path. Both produce identical verdicts — tests/test_gate_e2e.py
        asserts fingerprint equality between the two modes.
        """
        try:
            for f in review.findings:
                if f.covered_by_existing:
                    f.status = "skipped_covered"
            self._ensure_warm()
            if self._prep_error:
                review.env_error = self._prep_error
                for f in review.findings:
                    if not f.covered_by_existing:
                        f.status = "broken_test"
                        f.observed = "blocked by environment failure (see banner)"
                return review
            if batch:
                t0 = time.monotonic()
                leftover = self._classify_batch(review.findings)
                t_batch = time.monotonic() - t0
                t0 = time.monotonic()
                for i in sorted(leftover):
                    self.classify(review.findings[i])
                t_serial = time.monotonic() - t0
                eligible = sum(
                    1 for f in review.findings if not f.covered_by_existing
                )
                print(
                    f"  gate: batch {t_batch:.1f}s"
                    + (
                        f" + serial fallback {t_serial:.1f}s for "
                        f"{len(leftover)}/{eligible} finding(s)"
                        if leftover else f", 0/{eligible} fell back"
                    )
                )
            else:
                for f in review.findings:
                    self.classify(f)
            return review
        finally:
            if not self.reuse_warm:
                self.close()