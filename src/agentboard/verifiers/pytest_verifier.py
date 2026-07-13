"""The verifier with teeth: it runs the project's own test suite.

For every proposal that carries a code change, this copies the repo to a throwaway
directory, applies the change, and runs pytest. Green -> committed. Red ->
rejected, and the reason is the actual failing test, not a model's opinion. This
is the whole thesis made physical: the world decides, not the agent.

Each change is tested against a clean copy of the base repo (independent
verification). A production version would test against the accumulated committed
tree; kept simple here on purpose.

Safety: the real repo is never touched. All work happens on per-proposal copies
in a temp dir.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

from ..state import CodeChange, Node, Proposal, Rejection


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


def _first_failure(output: str) -> str:
    test = ""
    m = re.search(r"FAILED \S+::(\w+)", output)
    if m:
        test = m.group(1)
    # turn test_edge_labels_attached -> "edge labels attached"
    behavior = re.sub(r"^test[_ ]?", "", test).replace("_", " ").strip() if test else ""
    loc = re.search(r"(\w[\w-]*\.py):(\d+):", output)
    where = f" ({loc.group(1)}:{loc.group(2)})" if loc else ""
    em = re.search(r"^E\s+(.+)$", output, re.MULTILINE)
    assertion = em.group(1).strip()[:90] if em else ""

    if behavior:
        head = f"broke '{behavior}'"
    else:
        head = "broke the test suite"
    tail = f" — {assertion}" if assertion else ""
    return f"{head}{tail}{where}"


class PytestVerifier:
    """Implements the ``Verifier`` protocol by running a test suite."""

    def __init__(self, repo_root: str, test_args: list[str] | None = None, timeout: int = 120):
        self.repo_root = repo_root
        self.test_args = test_args or ["-q", "--tb=line", "-rf"]
        self.timeout = timeout

    def _run_tests(self, change: CodeChange) -> tuple[bool, str]:
        work = tempfile.mkdtemp(prefix="agentboard_verify_")
        try:
            dst = os.path.join(work, "repo")
            shutil.copytree(self.repo_root, dst, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            ok, err = _apply(change, dst)
            if not ok:
                return False, err
            env = dict(os.environ, PYTHONPATH=dst + os.pathsep + os.environ.get("PYTHONPATH", ""))
            proc = subprocess.run(
                ["python3", "-m", "pytest", *self.test_args],
                cwd=dst, env=env, capture_output=True, text=True, timeout=self.timeout,
            )
            if proc.returncode == 0:
                return True, ""
            return False, _first_failure(proc.stdout + proc.stderr)
        finally:
            shutil.rmtree(work, ignore_errors=True)

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
                # a concern with no code to run — accept (schema-level)
                accepted.append(p)
                continue
            passed, reason = self._run_tests(p.change)
            if passed:
                accepted.append(p)
            else:
                rejected.append(Rejection(p, reason))

        return accepted, rejected
