"""Proposal cache tests.

The cache may only ever save tokens — never change what the pipeline sees.
Three properties guarantee that:
  1. key sensitivity: any input byte changes -> different key (stale hits
     structurally impossible)
  2. roundtrip purity: what comes out is what went in, proposal fields only
  3. verdict fields are never cached: a loaded finding always starts pending
"""

from __future__ import annotations

import json
import os

from agentboard.proposal_cache import load, propose_or_cached, proposal_key, save
from agentboard.review import ReviewFinding

_BASE = dict(
    intent="i", change="c", source="s", tests="t",
    reviewer_model="m1", critic_model="m2",
    harness_notes="h", run_critic=True,
)


def test_key_is_stable_and_sensitive_to_every_input():
    k = proposal_key(**_BASE)
    assert k == proposal_key(**_BASE)
    for field in _BASE:
        mutated = dict(_BASE)
        mutated[field] = (not mutated[field]) if field == "run_critic" else "X"
        assert proposal_key(**mutated) != k, f"key ignores {field}"


def test_roundtrip_preserves_proposals_and_resets_verdicts(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTBOARD_CACHE_DIR", str(tmp_path))
    f = ReviewFinding(
        behavior="b", axis="consistency", covered_by_existing=False,
        coverage_note="none found", test_path="x.test.ts",
        test_code="test('t', () => {})",
    )
    # verdict-side facts from a previous gate run must NOT survive the cache
    f.status = "confirmed_gap"
    f.observed = "AssertionError: ..."
    save("k1", [f])
    (out,) = load("k1")
    assert (out.behavior, out.axis, out.test_code) == (
        "b", "consistency", "test('t', () => {})"
    )
    assert out.status == "pending"
    assert out.observed == ""


def test_corrupt_entry_is_a_miss_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTBOARD_CACHE_DIR", str(tmp_path))
    with open(tmp_path / "bad.json", "w", encoding="utf-8") as fh:
        fh.write("{ not json")
    assert load("bad") is None
    assert load("never-saved") is None


class _FakeReviewer:
    model = "m1"
    harness_notes = "h"

    def __init__(self):
        self.calls = 0

    def review(self, intent, change=""):
        self.calls += 1
        return [ReviewFinding(behavior=f"sampled #{self.calls}")]


def test_hit_skips_the_model_and_fresh_resamples(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTBOARD_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENTBOARD_FRESH", raising=False)
    r = _FakeReviewer()
    args = dict(intent="i", change="c", source="s", tests="t")

    first = propose_or_cached(r, None, **args)
    assert r.calls == 1 and first[0].behavior == "sampled #1"

    second = propose_or_cached(r, None, **args)
    assert r.calls == 1, "cache hit must not call the model"
    assert second[0].behavior == "sampled #1"
    assert "cache hit" in capsys.readouterr().out

    third = propose_or_cached(r, None, fresh=True, **args)
    assert r.calls == 2 and third[0].behavior == "sampled #2"

    changed = propose_or_cached(r, None, intent="DIFFERENT",
                                change="c", source="s", tests="t")
    assert r.calls == 3 and changed[0].behavior == "sampled #3"


def test_cache_file_holds_only_proposal_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTBOARD_CACHE_DIR", str(tmp_path))
    f = ReviewFinding(behavior="b")
    f.status = "handled"
    f.audit = "likely_real"
    save("k2", [f])
    raw = json.load(open(os.path.join(str(tmp_path), "k2.json")))
    row = raw["findings"][0]
    assert "status" not in row and "audit" not in row and "observed" not in row
