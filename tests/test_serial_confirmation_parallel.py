# sig EDGEVERDICT_PARALLEL_CONFIRM_TESTS_V1
# v12: batch-gap confirmation runs in parallel lanes, each against a PRIVATE
# copy of the warm repo. Sequential remains the authority: one gap, one
# worker, no warm repo, or failed copies all fall back to the proven path.
# These tests drive _confirm_batch_gaps end to end with a stubbed classify.
from __future__ import annotations

import threading

from edgeverdict.review import ReviewFinding, ReviewRun
from edgeverdict.verifiers.finding_verifier import FindingVerifier


def _fv(tmp_path=None):
    fv = FindingVerifier.__new__(FindingVerifier)
    fv.log = lambda *a, **k: None
    fv._warm_repo = None
    if tmp_path is not None:
        repo = tmp_path / "warm" / "repo"
        repo.mkdir(parents=True)
        (repo / "src.ts").write_text("export const x = 1\n")
        fv._warm_repo = str(repo)
    return fv


def _review(n_gaps):
    r = ReviewRun.__new__(ReviewRun)
    r.findings = []
    for i in range(n_gaps):
        f = ReviewFinding.__new__(ReviewFinding)
        f.status = "confirmed_gap"
        f.observed = f"batch observed {i}"
        f.covered_by_existing = False
        r.findings.append(f)
    return r


def test_parallel_lanes_get_private_repo_copies(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGEVERDICT_CONFIRM_WORKERS", "3")
    monkeypatch.setenv("EDGEVERDICT_CONFIRM_PARALLEL", "1")
    fv = _fv(tmp_path)
    seen: list[str | None] = []
    lock = threading.Lock()

    def fake_classify(f, repo_override=None):
        with lock:
            seen.append(repo_override)
        f.status = "confirmed_gap"
        return f

    fv.classify = fake_classify
    fv._confirm_batch_gaps(_review(6), leftover=set())
    assert len(seen) == 6
    overrides = {s for s in seen if s}
    assert overrides, "parallel path never engaged"
    assert all(o != fv._warm_repo for o in overrides), \
        "a lane ran against the SHARED warm repo"
    assert len(overrides) <= 3  # never more lanes than CONFIRM_WORKERS


def test_lane_copies_are_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGEVERDICT_CONFIRM_WORKERS", "2")
    monkeypatch.setenv("EDGEVERDICT_CONFIRM_PARALLEL", "1")
    fv = _fv(tmp_path)
    lanes: list[str] = []
    lock = threading.Lock()

    def fake_classify(f, repo_override=None):
        if repo_override:
            with lock:
                lanes.append(repo_override)
        f.status = "confirmed_gap"
        return f

    fv.classify = fake_classify
    fv._confirm_batch_gaps(_review(4), leftover=set())
    import os
    assert lanes and all(not os.path.exists(p) for p in lanes)


def test_workers_1_stays_sequential(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGEVERDICT_CONFIRM_WORKERS", "1")
    fv = _fv(tmp_path)
    overrides: list[str | None] = []

    def fake_classify(f, repo_override=None):
        overrides.append(repo_override)
        f.status = "confirmed_gap"
        return f

    fv.classify = fake_classify
    fv._confirm_batch_gaps(_review(4), leftover=set())
    assert overrides == [None, None, None, None]


def test_single_gap_stays_sequential(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGEVERDICT_CONFIRM_WORKERS", "4")
    fv = _fv(tmp_path)
    overrides: list[str | None] = []

    def fake_classify(f, repo_override=None):
        overrides.append(repo_override)
        f.status = "confirmed_gap"
        return f

    fv.classify = fake_classify
    fv._confirm_batch_gaps(_review(1), leftover=set())
    assert overrides == [None]


def test_no_warm_repo_stays_sequential_for_monkeypatched_classify(monkeypatch):
    # the pre-v12 unit tests monkeypatch classify with a SINGLE-ARG fake and
    # never build a warm repo; the parallel path must not engage there.
    monkeypatch.setenv("EDGEVERDICT_CONFIRM_WORKERS", "4")
    fv = _fv(tmp_path=None)
    calls = []

    def fake_classify(f):
        calls.append(f)
        f.status = "confirmed_gap"
        return f

    fv.classify = fake_classify
    fv._confirm_batch_gaps(_review(3), leftover=set())
    assert len(calls) == 3


def test_copy_failure_falls_back_to_sequential(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGEVERDICT_CONFIRM_WORKERS", "4")
    fv = _fv(tmp_path)
    from edgeverdict.verifiers import finding_verifier as fv_mod

    def boom(*a, **k):
        raise OSError("no space")

    monkeypatch.setattr(fv_mod.shutil, "copytree", boom)
    overrides: list[str | None] = []

    def fake_classify(f, repo_override=None):
        overrides.append(repo_override)
        f.status = "confirmed_gap"
        return f

    fv.classify = fake_classify
    review = _review(3)
    fv._confirm_batch_gaps(review, leftover=set())
    assert overrides == [None, None, None]
    assert all(f.status == "confirmed_gap" for f in review.findings)


def test_artifact_and_confirmation_notes_survive_parallel(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGEVERDICT_CONFIRM_WORKERS", "2")
    monkeypatch.setenv("EDGEVERDICT_CONFIRM_PARALLEL", "1")
    fv = _fv(tmp_path)

    def fake_classify(f, repo_override=None):
        # first finding survives, the rest pass in isolation
        if f.observed == "batch observed 0":
            f.status = "confirmed_gap"
        else:
            f.status = "handled"
            f.observed = ""
        return f

    fv.classify = fake_classify
    review = _review(3)
    fv._confirm_batch_gaps(review, leftover=set())
    survived = [f for f in review.findings if f.status == "confirmed_gap"]
    artifacts = [f for f in review.findings if f.status == "handled"]
    assert len(survived) == 1 and len(artifacts) == 2
    assert "[serially confirmed in isolation]" in survived[0].observed
    for f in artifacts:
        assert "shared-" in f.observed and "Batch had observed" in f.observed


def test_confirm_workers_env_parsing(monkeypatch):
    fv = FindingVerifier
    monkeypatch.delenv("EDGEVERDICT_CONFIRM_WORKERS", raising=False)
    assert fv._confirm_workers() == 4
    monkeypatch.setenv("EDGEVERDICT_CONFIRM_WORKERS", "8")
    assert fv._confirm_workers() == 8
    monkeypatch.setenv("EDGEVERDICT_CONFIRM_WORKERS", "0")
    assert fv._confirm_workers() == 1
    monkeypatch.setenv("EDGEVERDICT_CONFIRM_WORKERS", "banana")
    assert fv._confirm_workers() == 1


def test_default_is_sequential_no_copies(tmp_path, monkeypatch):
    # sig EDGEVERDICT_CONFIRM_INPLACE_V1: with the direct runner each re-gate
    # is ~0.2s, so the copy-per-lane path is off by default. Even with 4
    # workers and 4 gaps, confirm runs in-place (repo_override=None) and
    # copies nothing.
    monkeypatch.setenv("EDGEVERDICT_CONFIRM_WORKERS", "4")
    monkeypatch.delenv("EDGEVERDICT_CONFIRM_PARALLEL", raising=False)
    fv = _fv(tmp_path)
    overrides: list[str | None] = []

    def fake_classify(f, repo_override=None):
        overrides.append(repo_override)
        f.status = "confirmed_gap"
        return f

    fv.classify = fake_classify
    fv._confirm_batch_gaps(_review(4), leftover=set())
    # all in-place: no private-copy repo_override was ever used
    assert overrides == [None, None, None, None]


def test_parallel_opt_in_restores_copies(tmp_path, monkeypatch):
    # the copy-per-lane path is still available behind the flag
    monkeypatch.setenv("EDGEVERDICT_CONFIRM_WORKERS", "3")
    monkeypatch.setenv("EDGEVERDICT_CONFIRM_PARALLEL", "1")
    fv = _fv(tmp_path)
    overrides: list[str | None] = []

    def fake_classify(f, repo_override=None):
        overrides.append(repo_override)
        f.status = "confirmed_gap"
        return f

    fv.classify = fake_classify
    fv._confirm_batch_gaps(_review(4), leftover=set())
    # at least one lane ran against a private copy (repo_override set)
    assert any(o is not None for o in overrides)
