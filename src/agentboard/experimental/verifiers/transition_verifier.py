"""TransitionVerifier — the deterministic gate for self-tested changes.

How do you let an LLM both *propose a change* and *write the test for it*
without the test being worthless because the same model authored both? You do
NOT trust the test to be "correct." You verify the SHAPE of the behaviour change
it causes, by running things and comparing exit states. Three checks, all
deterministic, all repo- and intent-agnostic:

    1. RED ON BASELINE  — the new test must FAIL on the unchanged repo.
                          Kills tautological / vacuous tests.
    2. GREEN AFTER      — the new test must PASS once the change is applied.
    3. NO REGRESSION    — every test that passed before still passes.

No model is in this decision. The repo's behaviour decides. New tests are
identified by DIFFING test ids between runs, so nothing parses test source.

Honest ceiling: this proves the new test is non-trivial and the change satisfies
it without regressing. It does NOT prove the test asserts the *right* thing.
Judging that is a separate, softer layer (a second model's agree/disagree into
the Conflict surface) — advisory, never the gate.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from ..state import CodeChange, Rejection
from .vitest_verifier import RepoProfile, _parse_vitest_json, _tail


def _apply_change(change: CodeChange, repo: str, *, allow_create: bool = False) -> tuple[bool, str]:
    """Apply one CodeChange. An `append` to a non-existent path may CREATE the
    file (agents write brand-new test files)."""
    target = os.path.join(repo, change.path)
    if not os.path.isfile(target):
        if change.append is not None and allow_create:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(change.append.rstrip() + "\n")
            return True, ""
        return False, f"file not found: {change.path}"
    with open(target, encoding="utf-8") as f:
        content = f.read()
    if change.append is not None:
        content = content.rstrip() + "\n\n" + change.append + "\n"
    else:
        if change.find not in content:
            return False, f"anchor not found in {change.path}: {change.find!r}"
        content = content.replace(change.find, change.replace or "", 1)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)
    return True, ""


@dataclass
class _RunResult:
    passed: set
    failed: set
    infra: str

    @property
    def ids(self) -> set:
        return self.passed | self.failed


class TransitionVerifier:
    """Implements the ``Verifier`` protocol via red/green/no-regression checks."""

    _RESULT_FILE = "agentboard-transition-result.json"

    def __init__(self, repo_root: str, profile: RepoProfile, timeout: int = 1800):
        self.repo_root = repo_root
        self.profile = profile
        self.timeout = timeout
        self._baseline = None

    # ---- running -------------------------------------------------------

    def _fresh_copy(self) -> str:
        work = tempfile.mkdtemp(prefix="agentboard_transition_")
        dst = os.path.join(work, "repo")
        shutil.copytree(
            self.repo_root, dst,
            ignore=shutil.ignore_patterns(".git", "node_modules", "dist", "__pycache__"),
        )
        return dst

    def _run(self, args, cwd):
        env = {**os.environ, **self.profile.env}
        return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=self.timeout)

    def _install_build_test(self, repo: str) -> _RunResult:
        inst = self._run(self.profile.install_cmd, repo)
        if inst.returncode != 0:
            return _RunResult(set(), set(), f"install failed: {_tail(inst.stderr or inst.stdout)}")
        if self.profile.build_cmd:
            bld = self._run(self.profile.build_cmd, repo)
            if bld.returncode != 0:
                return _RunResult(set(), set(), f"build failed: {_tail(bld.stderr or bld.stdout)}")
        out = os.path.join(repo, self._RESULT_FILE)
        self._run(self.profile.test_base + ["--reporter=json", f"--outputFile={out}"], repo)
        if not os.path.isfile(out):
            return _RunResult(set(), set(), "test run produced no JSON output")
        failing, _msgs, infra = _parse_vitest_json(out)
        if infra:
            return _RunResult(set(), set(), infra)
        data = json.loads(open(out, encoding="utf-8").read())
        passed = set()
        for suite in data.get("testResults", []):
            for t in suite.get("assertionResults", []):
                if t.get("status") == "passed":
                    passed.add(" > ".join(t.get("ancestorTitles", []) + [t.get("title", "")]))
        return _RunResult(passed=passed, failed=failing, infra="")

    def _baseline_run(self) -> _RunResult:
        if self._baseline is None:
            repo = self._fresh_copy()
            try:
                self._baseline = self._install_build_test(repo)
            finally:
                shutil.rmtree(os.path.dirname(repo), ignore_errors=True)
        return self._baseline

    def _run_with(self, changes) -> _RunResult:
        repo = self._fresh_copy()
        try:
            for ch in changes:
                ok, err = _apply_change(ch, repo, allow_create=True)
                if not ok:
                    return _RunResult(set(), set(), err)
            return self._install_build_test(repo)
        finally:
            shutil.rmtree(os.path.dirname(repo), ignore_errors=True)

    # ---- the decision --------------------------------------------------

    def _check_transition(self, change: CodeChange, test_change: CodeChange):
        base = self._baseline_run()
        if base.infra:
            return False, f"baseline could not run: {base.infra}"

        with_test = self._run_with([test_change])      # test only, no impl
        if with_test.infra:
            return False, f"test-only run failed: {with_test.infra}"
        new_ids = with_test.ids - base.ids
        if not new_ids:
            return False, "no new test detected — the proposal added no test"
        not_red = new_ids & with_test.passed
        if not_red:
            return False, f"test passes without the change (tautological): {sorted(not_red)[0]}"

        with_both = self._run_with([change, test_change])  # test + impl
        if with_both.infra:
            return False, f"change+test run failed: {with_both.infra}"
        still_red = new_ids & with_both.failed
        if still_red:
            return False, f"change does not satisfy its own test: {sorted(still_red)[0]}"
        regressions = (with_both.failed & base.ids) - base.failed
        if regressions:
            return False, f"introduced regression: {sorted(regressions)[0]}"

        return True, f"verified: {len(new_ids)} new test(s) red->green, no regression"

    def _check_regression_only(self, change: CodeChange):
        base = self._baseline_run()
        if base.infra:
            return False, f"baseline could not run: {base.infra}"
        after = self._run_with([change])
        if after.infra:
            return False, after.infra
        regressions = (after.failed & base.ids) - base.failed
        if regressions:
            return False, f"introduced regression: {sorted(regressions)[0]}"
        return True, "no regression (no test supplied to prove new behaviour)"

    def verify_transition(self, change: CodeChange, test_change: CodeChange):
        """Public single-proposal entrypoint for the fix stage.

        Judges one (fix, test) pair with the full discipline:
        baseline -> test alone must be RED (tautology guard) -> fix+test must be
        GREEN -> nothing that passed at baseline may now fail. Returns
        (ok: bool, reason: str). No node/whiteboard model required.
        """
        return self._check_transition(change, test_change)

    def verify(self, proposals, nodes, committed):
        known = {n.id for n in nodes}
        accepted, rejected = [], []
        for p in proposals:
            if p.node_ref not in known:
                rejected.append(Rejection(p, f"references unknown node '{p.node_ref}'"))
                continue
            if p.change is None:
                accepted.append(p)
                continue
            if p.test_change is not None:
                ok, reason = self._check_transition(p.change, p.test_change)
            else:
                ok, reason = self._check_regression_only(p.change)
            if ok:
                accepted.append(p)
            else:
                rejected.append(Rejection(p, reason))
        return accepted, rejected