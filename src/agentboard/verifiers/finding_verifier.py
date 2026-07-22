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

This module owns the gate SEMANTICS only. Everything framework-specific —
injection, naming, run commands, output parsing, what counts as a named
assertion failure — lives behind the Harness seam (harness.py). The default
harness is vitest, so every existing caller is unchanged.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time

from ..review import ReviewFinding, ReviewRun
from .harness import Harness, VitestHarness
from .vitest_verifier import (RepoProfile, _proc_tail, scrubbed_env,
                              unfrozen_install)

# Back-compat aliases: the vitest injection rules moved into VitestHarness
# (harness.py) with their provenance comments; these names stay importable
# here because tests and older callers pin them.
_VITEST = VitestHarness()
_inject = _VITEST.inject
_strip_imports = _VITEST.strip_imports
_test_title = _VITEST.test_title


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


class FindingVerifier:
    def __init__(
        self,
        repo_root: str,
        profile: RepoProfile,
        tests_file: str,
        timeout: int = 1800,
        reuse_warm: bool = False,
        project_dir: str = ".",
        log=print,
        harness: Harness | None = None,
    ):
        self.repo_root = repo_root
        # print-shaped narration sink; the caller picks where lines go (the
        # CLI passes print, the MCP server a per-call buffer). See api.py.
        self.log = log
        # Repo-relative dir the toolchain runs in. The warm copy is still
        # the whole repo (tests_file and the result file stay repo-relative),
        # but install/build/smoke/test all execute HERE, so a package nested
        # inside a larger repo works. "." for every single-package repo.
        self.project_dir = project_dir
        self.profile = profile
        # The framework seam. Default vitest so every existing caller keeps
        # its exact behavior; api.py selects from the profile.
        self.harness = harness or VitestHarness()
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

    def _workdir(self, repo: str) -> str:
        return os.path.normpath(os.path.join(repo, self.project_dir))

    def _run(self, args, cwd):
        # scrubbed_env: model-provider keys never reach executed code — the
        # gate has no LLM in it, so nothing it spawns needs them. The warm
        # root also hosts this run's private npm/pnpm caches (see
        # scrubbed_env for the isolation-over-warmth tradeoff).
        env = scrubbed_env(self.profile.env, cache_root=self._warm_root)
        return subprocess.run(
            args, cwd=cwd, env=env, capture_output=True, text=True, timeout=self.timeout
        )

    def _fresh_result_path(self, repo: str) -> str:
        """Where this run's machine-readable results go — with any stale
        artifact from a previous finding removed first. A runner that dies
        before writing (config error, killed on timeout) must yield "no
        output", never a silently re-read verdict from the last finding."""
        out = os.path.join(repo, self.harness.result_file)
        try:
            os.remove(out)
        except FileNotFoundError:
            pass
        return out

    # -- warm base: copy + install + build ONCE ------------------------------
    def _ensure_warm(self) -> None:
        """Build the warm base if it doesn't exist: one copy, one install, one
        build. Every finding reuses this dependency tree; only the tests file
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
        # install + build ONCE. A hang here is the same loud prep-error as a
        # nonzero exit — the smoke probe below always caught TimeoutExpired,
        # but install/build let it escape as a traceback through the run.
        # An empty install_cmd means the profile declares no install step
        # (python repos: the running environment is assumed provisioned).
        if self.profile.install_cmd:
            t0 = time.monotonic()
            try:
                inst = self._run(self.profile.install_cmd, self._workdir(repo))
                retry = unfrozen_install(self.profile.install_cmd)
                if inst.returncode != 0 and retry is not None:
                    # stale lockfile, most likely — degrade to the permissive
                    # install rather than benching the run, but say so out loud.
                    self.log("  install: frozen lockfile install failed; "
                             "retrying with --no-frozen-lockfile")
                    inst = self._run(retry, self._workdir(repo))
                phases.append(f"install {time.monotonic() - t0:.1f}s")
                if inst.returncode != 0:
                    self._prep_error = f"install failed: {_proc_tail(inst)}"
            except subprocess.TimeoutExpired:
                self._prep_error = f"install did not finish within {self.timeout}s"
        if not self._prep_error and self.profile.build_cmd:
            t0 = time.monotonic()
            try:
                bld = self._run(self.profile.build_cmd, self._workdir(repo))
                phases.append(f"build {time.monotonic() - t0:.1f}s")
                if bld.returncode != 0:
                    self._prep_error = f"build failed: {_proc_tail(bld)}"
            except subprocess.TimeoutExpired:
                self._prep_error = f"build did not finish within {self.timeout}s"
        # functional smoke probe: prove the runner starts before judging
        # anything. An exit code can lie across toolchain versions; a probe
        # that actually launches the runner cannot.
        if not self._prep_error and getattr(self.profile, "smoke_cmd", None):
            t0 = time.monotonic()
            try:
                smoke = self._run(self.profile.smoke_cmd, self._workdir(repo))
                phases.append(f"smoke {time.monotonic() - t0:.1f}s")
                if smoke.returncode != 0:
                    self._prep_error = (
                        "environment smoke probe failed: "
                        f"{_proc_tail(smoke)}"
                    )
            except subprocess.TimeoutExpired:
                self._prep_error = (
                    f"environment smoke probe did not finish within {self.timeout}s"
                )
        self.log("  warm base: " + ", ".join(phases))
        self._warm_repo = repo

    def close(self) -> None:
        """Delete the warm base. Called at the end of run() unless reuse_warm."""
        if self._warm_root:
            shutil.rmtree(self._warm_root, ignore_errors=True)
        self._warm_root = self._warm_repo = self._pristine_tests = None
        self._prep_error = ""

    def classify(self, finding: ReviewFinding) -> ReviewFinding:
        """Inject this finding's test into the warm base's pristine tests file
        and run ONLY it. Reuses the shared dependency tree; resets the tests
        file to pristine first so no finding sees another's injected test."""
        if finding.covered_by_existing:
            finding.status = "skipped_covered"
            return finding
        self._ensure_warm()
        if self._prep_error:  # install/build failed -> nothing can run
            finding.status = "broken_test"
            finding.observed = self._prep_error
            return finding
        title = self.harness.test_title(finding.test_code or "")
        if not title:
            finding.status = "broken_test"
            finding.observed = "could not read test name"
            return finding
        injected, err = self.harness.inject(self._pristine_tests or "",
                                            finding.test_code or "")
        if injected is None:
            finding.status = "broken_test"
            finding.observed = err
            return finding
        repo = self._warm_repo
        assert repo is not None  # set by _ensure_warm when prep succeeded
        tpath = os.path.join(repo, self.tests_file)
        try:
            # write pristine + THIS finding's test (clean start every time)
            with open(tpath, "w", encoding="utf-8") as f:
                f.write(injected)
            out = self._fresh_result_path(repo)
            try:
                self._run(
                    self.harness.serial_command(self.profile, self.tests_file,
                                                title, out),
                    self._workdir(repo),
                )
            except subprocess.TimeoutExpired:
                finding.status = "timed_out"
                finding.observed = (
                    f"did not finish within {self.timeout}s (subprocess limit)"
                )
                return finding
            finding.status, finding.observed = self.harness.read_verdict(out)
            return finding
        finally:
            # restore pristine so the base is clean for the next finding/run
            if self._pristine_tests is not None:
                with open(tpath, "w", encoding="utf-8") as f:
                    f.write(self._pristine_tests)

    # -- verdict brain back-compat -------------------------------------------
    # The classification and parsing bodies moved into VitestHarness; these
    # staticmethods stay because tests (and the determinism harness) pin the
    # names, and because they document that the DEFAULT gate is vitest.

    @staticmethod
    def _classify_failure(fm: str) -> tuple[str, str]:
        return _VITEST.classify_failure(fm)

    @staticmethod
    def _read(out: str) -> tuple[str, str]:
        return _VITEST.read_verdict(out)

    # -- batched gate ---------------------------------------------------------

    _MARK = "___ab{i}___"
    _MARK_PREFIX = "___ab"
    # anything in a PROPOSAL that looks like one of our marks. Attribution
    # matches marks by substring in the executed test's title, so a proposal
    # whose own title carries a lookalike (___ab0___) would hijack finding
    # 0's verdict. Stripping the pattern before the gate adds its own mark
    # guarantees the only mark in any executed title is gate-injected.
    _MARK_RE = re.compile(r"___ab\d+___")

    def _classify_batch(self, findings: list[ReviewFinding]) -> set[int]:
        """Inject every finding's test at once (uniquely marked titles), run
        the suite ONCE filtered to the mark, attribute results per finding.

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
            title = self.harness.test_title(f.test_code or "")
            if not title:
                continue  # serial path will report it properly
            mark = self._MARK.format(i=i)
            # The proposal is de-marked first (see _MARK_RE) so it cannot
            # smuggle another finding's mark into its own title; the harness
            # then stamps the gate's own mark into the test opener.
            code = self.harness.mark_title(
                self._MARK_RE.sub("", f.test_code or ""), mark)
            if code is None:
                continue
            injected, _err = self.harness.inject(content, code)
            if injected is None:
                continue
            content, marked[i] = injected, mark
        if not marked:
            return set(pending)

        repo = self._warm_repo
        assert repo is not None  # set by _ensure_warm when prep succeeded
        tpath = os.path.join(repo, self.tests_file)
        try:
            with open(tpath, "w", encoding="utf-8") as fh:
                fh.write(content)
            out = self._fresh_result_path(repo)
            try:
                self._run(
                    self.harness.batch_command(self.profile, self.tests_file,
                                               self._MARK_PREFIX, out),
                    self._workdir(repo),
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
        for serial. Verdict logic is the same shared brain as serial (the
        harness's classify_failure)."""
        results = self.harness.read_batch(out)
        if results is None:
            return set()
        # collect every executed test whose title carries one of our marks
        per: dict[int, list] = {}
        for r in results:
            for i, mark in marked.items():
                if mark in r.title:
                    per.setdefault(i, []).append(r)
        done: set[int] = set()
        for i, rs in per.items():
            f = pending[i]
            gap = timeout = load = None
            ran = 0
            for r in rs:
                if r.status in ("passed", "failed"):
                    ran += 1
                if r.status == "failed":
                    kind, first = self.harness.classify_failure(r.failure)
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
                        f.observed = "not executed: environment failure (see banner above)"
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
                self.log(
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
