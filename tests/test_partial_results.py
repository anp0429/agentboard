"""Partial-results survival and output grouping, the two lessons a real
quota death taught: (a) executed evidence from finished files must survive
a later file's failure — aborting mid-loop once discarded six real
verdicts; (b) one cause with many corpses prints once with a count, not N
identical lines. Everything model-shaped is stubbed; only run_review's
control flow is under test."""

import os
import subprocess
import tempfile
import types

import pytest

import agentboard.api as api
from agentboard.review import ReviewFinding, ReviewRun


class _StubVerifier:
    def __init__(self, *a, **k):
        pass

    def run(self, sub: ReviewRun) -> None:
        for f in sub.findings:
            f.status = f.status or "handled"


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    """A run_review wired to stubs: two targets, per-target proposal
    behavior injectable via the returned dict."""
    repo = str(tmp_path)
    for name in ("a.py", "b.py", "tests"):
        p = os.path.join(repo, name)
        if name == "tests":
            os.makedirs(p)
        else:
            open(p, "w").write("X = 1\n")
    open(os.path.join(repo, "tests", "test_a.py"), "w").write("import a\n")
    open(os.path.join(repo, "tests", "test_b.py"), "w").write("import b\n")
    for cmd in (["init", "-q", "-b", "main"],
                ["config", "user.email", "t@t"],
                ["config", "user.name", "t"],
                ["add", "-A"],
                ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", repo, *cmd], check=True,
                       capture_output=True)

    cfg = types.SimpleNamespace(base_url="", base="", run_critic=False,
                                reviewer_model="stub", critic_model="stub")
    monkeypatch.setattr(api, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(api, "preflight", lambda **k: [])
    monkeypatch.setattr(api, "detect_project_dir", lambda *a: ".")
    monkeypatch.setattr(api, "build_profile",
                        lambda *a, **k: types.SimpleNamespace(
                            harness_notes="", test_base=[]))
    monkeypatch.setattr(api, "harness_for_profile", lambda *a, **k: None)
    monkeypatch.setattr(api, "FindingVerifier", _StubVerifier)
    monkeypatch.setattr(api, "render_review_html",
                        lambda run, path: path)
    monkeypatch.setattr(api, "current_branch", lambda *a: "main")
    monkeypatch.setattr(api, "fork_point", lambda *a: "main")
    monkeypatch.setattr(
        api, "ReviewerAgent",
        lambda *a, **k: types.SimpleNamespace(base_url=""))

    proposals: dict[str, list] = {}

    def _propose(reviewer, critic, **k):
        # keyed by which target's turn it is, in pair order
        tgt = order.pop(0)
        return proposals.get(tgt, [])

    order: list[str] = []
    real = api.propose_or_cached
    monkeypatch.setattr(api, "propose_or_cached", _propose)

    def request(**over):
        order[:] = ["a.py", "b.py"]
        req = api.ReviewRequest(
            repo=repo, target="a.py", tests="tests/test_a.py",
            also=["b.py:tests/test_b.py"], intent="stub intent",
            no_audit=True, no_critic=True,
            board=os.path.join(tempfile.mkdtemp(), "board.html"),
        )
        for k, v in over.items():
            setattr(req, k, v)
        return req

    return types.SimpleNamespace(repo=repo, proposals=proposals,
                                 request=request, real_propose=real)


def _log_to(lines):
    # the api's log is PRINT-SHAPED: zero-arg calls emit blank lines
    def log(*parts):
        lines.append(" ".join(str(p) for p in parts))
    return log


def _f(behavior, status="handled", observed=""):
    return ReviewFinding(behavior=behavior, status=status, observed=observed)


def test_finished_evidence_survives_a_later_files_failure(wired):
    lines: list[str] = []
    wired.proposals["a.py"] = [_f("a works"), _f("a boundary",
                                                 "confirmed_gap", "boom")]
    wired.proposals["b.py"] = []  # the quota-death shape
    result = api.run_review(wired.request(), log=_log_to(lines))
    assert result.exit_code == 0
    assert result.run is not None
    assert len(result.run.findings) == 2  # a.py's evidence kept
    out = "\n".join(lines)
    assert "PARTIAL RUN" in out
    assert "b.py (0 behaviors proposed)" in out
    assert "1 of 2 file(s)" in out


def test_every_file_failing_is_still_the_hard_stop(wired):
    lines: list[str] = []
    wired.proposals["a.py"] = []
    wired.proposals["b.py"] = []
    result = api.run_review(wired.request(), log=_log_to(lines))
    assert result.exit_code == 1
    assert result.run is None
    assert "every file" in "\n".join(lines)


def test_identical_broken_causes_print_once_with_a_count(wired):
    lines: list[str] = []
    cause = "NameError: name '_mk' is not defined"
    wired.proposals["a.py"] = [
        _f("p1", "broken_test", cause),
        _f("p2", "broken_test", cause),
        _f("p3", "broken_test", cause),
        _f("p4", "broken_test", "SyntaxError: unique"),
        _f("ok", "handled"),
    ]
    wired.proposals["b.py"] = [_f("b ok", "handled")]
    api.run_review(wired.request(), log=_log_to(lines))
    out = "\n".join(lines)
    assert out.count(cause) == 1
    assert "(x3 proposals share this cause)" in out
    assert "SyntaxError: unique" in out  # singletons keep their own arrow


def test_forecast_line_announces_the_plan_for_multi_file_runs(wired):
    lines: list[str] = []
    wired.proposals["a.py"] = [_f("a ok")]
    wired.proposals["b.py"] = [_f("b ok")]
    api.run_review(wired.request(), log=_log_to(lines))
    assert any(line.startswith("plan: 2 files to review") for line in lines)


def test_worktree_diff_named_tests_rescue_failed_resolution(wired, monkeypatch):
    """Catch 4b: the diff-tests fallback must be reachable in WORKTREE
    mode. Resolution fails, the dirty tree's own diff names exactly one
    test file, and the run proceeds with it instead of stopping."""
    monkeypatch.setattr(api, "_default_tests_for",
                        lambda *a, **k: "tests/nope.py")
    # DIRTY the tree: worktree mode diffs the working tree, and a clean
    # tree names nothing (the first version of this test forgot that,
    # which is the fixture-vs-world gap in miniature)
    with open(os.path.join(wired.repo, "a.py"), "a") as fh:
        fh.write("Y = 2\n")
    with open(os.path.join(wired.repo, "tests", "test_a.py"), "a") as fh:
        fh.write("assert a\n")
    wired.proposals["a.py"] = [_f("a works")]
    wired.proposals["b.py"] = [_f("b works")]
    lines: list[str] = []
    req = wired.request(tests="", worktree=True)
    result = api.run_review(req, log=_log_to(lines))
    assert result.exit_code == 0
    assert any("the change's own diff names it" in ln for ln in lines)
