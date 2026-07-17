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

from ..review import ReviewFinding, ReviewRun
from .vitest_verifier import RepoProfile, _tail


def _inject(pristine: str, test_code: str) -> tuple[str | None, str]:
    """Inject the agent's test into the pristine tests-file content (PURE).

    Works on the pristine content in memory (not the file on disk) so the warm
    base can inject a DIFFERENT test per finding, each starting from a clean file
    — never stacking one finding's test on top of another's. Insert before the
    file's final closing `});` (the describe block's close) so the new test
    inherits every import and helper, exactly as if added by hand.
    """
    if not test_code:
        return None, "no test supplied"
    idx = pristine.rstrip().rfind("\n});")
    if idx == -1:
        return None, "could not find describe-block close to inject before"
    return pristine[:idx] + "\n\n" + test_code.rstrip() + "\n" + pristine[idx:], ""




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
        shutil.copytree(
            self.repo_root,
            repo,
            ignore=shutil.ignore_patterns(
                ".git", "node_modules", "dist", "__pycache__"
            ),
        )
        # the sandbox must BE the repo — a dropped file here would make
        # verdicts depend on copy luck. Fail loudly, never review a ghost.
        diffs = _copy_discrepancies(self.repo_root, repo)
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
        inst = self._run(self.profile.install_cmd, repo)
        if inst.returncode != 0:
            self._prep_error = f"install failed: {_tail(inst.stderr or inst.stdout)}"
        elif self.profile.build_cmd:
            bld = self._run(self.profile.build_cmd, repo)
            if bld.returncode != 0:
                self._prep_error = f"build failed: {_tail(bld.stderr or bld.stdout)}"
        # functional smoke probe: prove the runner starts before judging
        # anything. An exit code can lie across toolchain versions; a probe
        # that actually launches the runner cannot.
        if not self._prep_error and getattr(self.profile, "smoke_cmd", None):
            try:
                smoke = self._run(self.profile.smoke_cmd, repo)
                if smoke.returncode != 0:
                    self._prep_error = (
                        "environment smoke probe failed: "
                        f"{_tail(smoke.stderr or smoke.stdout)}"
                    )
            except subprocess.TimeoutExpired:
                self._prep_error = (
                    f"environment smoke probe did not finish within {self.timeout}s"
                )
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
                    + ["-t", title, "--reporter=json", f"--outputFile={out}"],
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
                    first = fm.strip().splitlines()[0][:200] if fm.strip() else ""
                    # vitest's per-test timeout ("Test timed out in 30000ms") is
                    # AMBIGUOUS evidence — not a broken test, not a gap. Surface
                    # it as its own status and let the human decide.
                    if "timed out" in fm.lower():
                        timeout_msg = first
                    # a vitest assertion failure reads "AssertionError: ..."; a thrown
                    # runtime error reads differently. Treat AssertionError as a real gap.
                    elif "AssertionError" in fm or "expected" in fm.lower():
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

    def run(self, review: ReviewRun) -> ReviewRun:
        """Classify all findings against one warm base. Install/build happens once
        here, not once per finding."""
        try:
            for f in review.findings:
                self.classify(f)
            return review
        finally:
            if not self.reuse_warm:
                self.close()