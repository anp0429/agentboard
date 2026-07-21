"""The legacy loop-protocol face of the vitest runner.

VitestVerifier implements the old ``Verifier`` protocol (accept/reject
Proposals against a Board) by running a pnpm/npm + vitest suite with
baseline-delta acceptance: the repo's pre-existing failures are captured
once, and a change is rejected only if it introduces a NEW failure.

The repo facts (RepoProfile) and the execution helpers it runs on are not
legacy — the review gate uses them too — so they live in
``agentboard.verifiers.vitest_verifier`` and are imported (and re-exported,
for this subpackage's older callers) here.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from ...verifiers.vitest_verifier import (  # noqa: F401 - re-exports for legacy callers
    RepoProfile,
    SUPABASE_MCP,
    _parse_vitest_json,
    _tail,
    scrubbed_env,
    unfrozen_install,
)
from ..state import CodeChange, Node, Proposal, Rejection


# --- reuse PytestVerifier's edit semantics so behaviour is identical ---------

def _apply(change: CodeChange, work: str) -> tuple[bool, str]:
    target = os.path.join(work, change.path)
    if not os.path.isfile(target):
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


# --- the verifier ------------------------------------------------------------

class VitestVerifier:
    """Implements the ``Verifier`` protocol by running a vitest suite."""

    _RESULT_FILE = "agentboard-vitest-result.json"

    def __init__(self, repo_root: str, profile: RepoProfile, timeout: int = 1800):
        self.repo_root = repo_root
        self.profile = profile
        self.timeout = timeout
        self._baseline: set[str] | None = None  # failing-test ids on the clean repo

    # ---- subprocess + parsing ----------------------------------------------

    def _run(self, args: list[str], cwd: str) -> subprocess.CompletedProcess:
        # cwd is <run-temp>/repo; the run's temp area also hosts this run's
        # private npm/pnpm caches (see scrubbed_env for the tradeoff).
        env = scrubbed_env(self.profile.env, cache_root=os.path.dirname(cwd))
        return subprocess.run(
            args, cwd=cwd, env=env, capture_output=True, text=True, timeout=self.timeout
        )

    def _build_and_test(self, repo: str) -> tuple[set[str], dict[str, str], str]:
        """Install, (build), test. Returns (failing_ids, messages, infra_error).

        infra_error is non-empty only if the run could not produce results at all
        (install/build blew up, or vitest never wrote JSON) — that's an
        infrastructure failure, distinct from a test failure.
        """
        # a hang in any phase is the same infrastructure failure as a nonzero
        # exit — TimeoutExpired must never escape as a traceback mid-review.
        try:
            inst = self._run(self.profile.install_cmd, repo)
            retry = unfrozen_install(self.profile.install_cmd)
            if inst.returncode != 0 and retry is not None:
                # stale lockfile, most likely — degrade to the permissive
                # install rather than benching the run, but say so out loud.
                print("  install: frozen lockfile install failed; "
                      "retrying with --no-frozen-lockfile")
                inst = self._run(retry, repo)
        except subprocess.TimeoutExpired:
            return set(), {}, f"install did not finish within {self.timeout}s"
        if inst.returncode != 0:
            return set(), {}, f"install failed: {_tail(inst.stderr or inst.stdout)}"

        if self.profile.build_cmd:
            try:
                bld = self._run(self.profile.build_cmd, repo)
            except subprocess.TimeoutExpired:
                return set(), {}, f"build did not finish within {self.timeout}s"
            if bld.returncode != 0:
                return set(), {}, f"build failed: {_tail(bld.stderr or bld.stdout)}"

        out = os.path.join(repo, self._RESULT_FILE)
        cmd = self.profile.test_base + ["--reporter=json", f"--outputFile={out}"]
        try:
            self._run(cmd, repo)  # non-zero exit is normal when tests fail; we read JSON
        except subprocess.TimeoutExpired:
            return set(), {}, f"test run did not finish within {self.timeout}s"

        if not os.path.isfile(out):
            return set(), {}, "test run produced no JSON output"
        return _parse_vitest_json(out)

    def _ensure_baseline(self) -> set[str]:
        if self._baseline is None:
            work = tempfile.mkdtemp(prefix="agentboard_baseline_")
            try:
                dst = os.path.join(work, "repo")
                shutil.copytree(
                    self.repo_root, dst,
                    ignore=shutil.ignore_patterns(".git", "node_modules", "dist", "__pycache__"),
                )
                failing, _msgs, infra = self._build_and_test(dst)
                # If the baseline itself can't run, treat as empty and let each
                # change surface the infra error; don't silently pass everything.
                self._baseline = failing if not infra else set()
                self._baseline_infra = infra
            finally:
                shutil.rmtree(work, ignore_errors=True)
        return self._baseline

    def _run_change(self, change: CodeChange) -> tuple[bool, str]:
        baseline = self._ensure_baseline()
        work = tempfile.mkdtemp(prefix="agentboard_verify_")
        try:
            dst = os.path.join(work, "repo")
            shutil.copytree(
                self.repo_root, dst,
                ignore=shutil.ignore_patterns(".git", "node_modules", "dist", "__pycache__"),
            )
            ok, err = _apply(change, dst)
            if not ok:
                return False, err
            failing, msgs, infra = self._build_and_test(dst)
            if infra:
                return False, infra
            new = failing - baseline
            if new:
                first = sorted(new)[0]
                return False, _describe_failure(first, msgs.get(first, ""))
            return True, ""
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ---- the Verifier protocol ---------------------------------------------

    def verify(
        self,
        proposals: list[Proposal],
        nodes: list[Node],
        committed: list[Proposal],
    ) -> tuple[list[Proposal], list[Rejection]]:
        known_nodes = {n.id for n in nodes}
        accepted: list[Proposal] = []
        rejected: list[Rejection] = []

        for p in proposals:
            if p.node_ref not in known_nodes:
                rejected.append(Rejection(p, f"references unknown node '{p.node_ref}'"))
                continue
            if p.change is None:
                accepted.append(p)            # schema-level concern, nothing to run
                continue
            passed, reason = self._run_change(p.change)
            if passed:
                accepted.append(p)
            else:
                rejected.append(Rejection(p, reason))

        return accepted, rejected


def _describe_failure(test_id: str, message: str) -> str:
    """Human reason in the PytestVerifier house style: behaviour + assertion."""
    behavior = test_id.split(" > ")[-1] if test_id else "the test suite"
    first_line = (message or "").splitlines()[0].strip() if message else ""
    head = f"broke '{behavior}'"
    return f"{head} — {first_line[:90]}" if first_line else head
