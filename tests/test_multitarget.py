"""Multi-target review foundation — the gate reviews a SET of files, not one.

This is the prerequisite for blast-radius scoping: once review can gate the
changed file plus its impacted files, the graph just chooses which files go
in the set. Single-target stays the default (one-item set), so nothing about
today's behavior changes.
"""

from __future__ import annotations

import os
import tempfile
import types

from agentboard.cli import _resolve_targets
from agentboard.review import ReviewFinding, ReviewRun


def _repo_with(*files):
    d = tempfile.mkdtemp()
    for f in files:
        open(os.path.join(d, f), "w").close()
    return d


def test_single_target_is_the_default():
    d = _repo_with("a.ts", "a.test.ts")
    args = types.SimpleNamespace(also=[])
    assert _resolve_targets(d, "a.ts", "a.test.ts", args) == [("a.ts", "a.test.ts")]


def test_also_adds_a_pair_with_autodetected_tests():
    d = _repo_with("a.ts", "a.test.ts", "b.ts", "b.test.ts")
    args = types.SimpleNamespace(also=["b.ts"])
    pairs = _resolve_targets(d, "a.ts", "a.test.ts", args)
    assert ("a.ts", "a.test.ts") in pairs and ("b.ts", "b.test.ts") in pairs


def test_also_accepts_explicit_tests():
    d = _repo_with("a.ts", "a.test.ts", "b.ts", "custom.test.ts")
    args = types.SimpleNamespace(also=["b.ts:custom.test.ts"])
    pairs = _resolve_targets(d, "a.ts", "a.test.ts", args)
    assert ("b.ts", "custom.test.ts") in pairs


def test_also_skips_a_file_with_no_findable_tests():
    d = _repo_with("a.ts", "a.test.ts", "orphan.ts")  # no orphan.test.ts
    args = types.SimpleNamespace(also=["orphan.ts"])
    pairs = _resolve_targets(d, "a.ts", "a.test.ts", args)
    assert pairs == [("a.ts", "a.test.ts")]  # orphan skipped, not crashed


def test_findings_carry_their_source_file():
    """Merged multi-target findings must know which file they're about."""
    run = ReviewRun(intent="x", target="a.ts")
    fa = ReviewFinding(behavior="b1", source_file="a.ts")
    fb = ReviewFinding(behavior="b2", source_file="b.ts")
    run.findings.extend([fa, fb])
    by_file = {f.source_file for f in run.findings}
    assert by_file == {"a.ts", "b.ts"}
