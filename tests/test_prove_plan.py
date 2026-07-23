"""prove's trustworthiness lives in two places: the plan (did it review
the right thing with zero flags) and the verdict wording (did it say only
what execution proved). Both are pure, so both get pinned here. The
honesty rules under test: HELD disclaims correctness in its own text,
zero-executed is never green silence, broken_test never leads a headline,
and BROKEN/HELD/STOPPED map to distinct exit codes for agent callers."""

import subprocess

import pytest

from agentboard.prove import (
    ProvePlan,
    exit_code_for,
    llm_configured,
    plan_prove,
    verdict_block,
)
from agentboard.review import ReviewFinding, ReviewRun


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    r = str(tmp_path)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (tmp_path / "a.ts").write_text("export const a = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return tmp_path


def _run(**counts) -> ReviewRun:
    run = ReviewRun(intent="i", target="t")
    for status, k in counts.items():
        for _ in range(k):
            run.findings.append(ReviewFinding(behavior=f"b-{status}",
                                              status=status,
                                              observed="expected 1 to be 2"))
    return run


# ---- plan --------------------------------------------------------------

def test_dirty_tree_plans_worktree_mode_against_head(repo):
    (repo / "a.ts").write_text("export const a = 2\n")
    plan = plan_prove(str(repo))
    assert plan.worktree is True
    assert plan.base == "HEAD"
    assert plan.targets == ["a.ts"]
    assert plan.intent == ""  # uncommitted work has no message to derive from


def test_clean_branch_plans_fork_point_and_derives_intent(repo):
    r = str(repo)
    _git(r, "checkout", "-q", "-b", "feat")
    (repo / "a.ts").write_text("export const a = 2\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "clamp page size at the maximum")
    plan = plan_prove(r)
    assert plan.worktree is False
    assert plan.targets == ["a.ts"]
    assert plan.intent_source == "commits"
    assert "clamp page size" in plan.intent


def test_intent_flag_beats_derivation(repo):
    r = str(repo)
    _git(r, "checkout", "-q", "-b", "feat")
    (repo / "a.ts").write_text("export const a = 2\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "commit words")
    plan = plan_prove(r, intent_arg="flag words")
    assert (plan.intent, plan.intent_source) == ("flag words", "flag")


# ---- no-key gate -------------------------------------------------------

def test_llm_configured_requires_key_or_base_url():
    assert llm_configured(environ={}) is False
    assert llm_configured(environ={"OPENAI_API_KEY": "sk-x"}) is True
    assert llm_configured(environ={"OPENAI_BASE_URL": "http://l:1/v1"}) is True
    assert llm_configured(environ={}, config_base_url="http://l:1/v1") is True
    assert llm_configured(environ={"OPENAI_API_KEY": "   "}) is False


# ---- verdict wording ---------------------------------------------------

def test_broken_headline_counts_gaps_and_executed_attempts():
    line = verdict_block(_run(confirmed_gap=2, handled=7, broken_test=3))
    assert line.startswith("BROKEN: 2 failing tests, 9 attempts executed")
    assert "3 proposals broke before running" in line


def test_held_discloses_its_own_limits_in_text():
    line = verdict_block(_run(handled=5, skipped_covered=2))
    assert line.startswith("HELD: 5 executed attempts, 0 broke it")
    assert "not a proof of correctness" in line


def test_zero_executed_is_never_silent_green():
    covered = verdict_block(_run(skipped_covered=8, broken_test=9))
    assert covered.startswith("NOTHING NEW EXECUTED: 8")
    only_broken = verdict_block(_run(broken_test=4))
    assert only_broken.startswith("STOPPED:")
    assert "not evidence about your code" in only_broken
    empty = verdict_block(_run())
    assert empty.startswith("STOPPED: nothing was proposed")


def test_timed_out_is_inconclusive_not_a_win_for_either_side():
    line = verdict_block(_run(handled=3, timed_out=2))
    assert line.startswith("HELD: 3 executed attempts")
    assert "2 timed out (inconclusive)" in line


def test_env_error_is_a_stop_with_the_cause_first():
    run = _run(handled=1)
    run.env_error = "npm install failed: ERESOLVE\nlong tail"
    assert verdict_block(run) == "STOPPED: npm install failed: ERESOLVE"


# ---- exit codes --------------------------------------------------------

def test_exit_codes_distinguish_broken_held_stopped():
    assert exit_code_for(None) == 1
    assert exit_code_for(_run(handled=3)) == 0
    assert exit_code_for(_run(confirmed_gap=1, handled=3)) == 2
    bad = _run(handled=1)
    bad.env_error = "boom"
    assert exit_code_for(bad) == 1


def test_plan_dataclass_defaults_are_safe():
    p = ProvePlan(worktree=True, head="h", base="HEAD")
    assert p.targets == [] and p.intent == ""


def test_stopped_runs_exit_nonzero_matching_their_verdict():
    only_broken = _run(broken_test=3)
    assert verdict_block(only_broken).startswith("STOPPED:")
    assert exit_code_for(only_broken) == 1
    covered_only = _run(skipped_covered=4, broken_test=2)
    assert verdict_block(covered_only).startswith("NOTHING NEW EXECUTED")
    assert exit_code_for(covered_only) == 0
    nothing = _run()
    assert exit_code_for(nothing) == 1


def test_prove_board_path_is_per_verb_and_non_overwriting():
    from agentboard.prove import prove_board_path
    a = prove_board_path(now=1000000000)
    b = prove_board_path(now=1000000060)
    assert "agentboard_prove_board_" in a
    assert a.endswith(".html")
    assert a != b


def test_verdict_from_counts_matches_the_run_path():
    from agentboard.prove import verdict_from_counts
    run = _run(confirmed_gap=2, handled=7, broken_test=3)
    counts = {"confirmed_gap": 2, "handled": 7, "broken_test": 3}
    assert verdict_from_counts(counts) == verdict_block(run)
    held_run = _run(handled=5, skipped_covered=2)
    assert verdict_from_counts({"handled": 5, "skipped_covered": 2}) == \
        verdict_block(held_run)


def test_verdict_from_counts_tolerates_missing_and_unknown_keys():
    from agentboard.prove import verdict_from_counts
    assert verdict_from_counts({}).startswith("STOPPED: nothing was proposed")
    line = verdict_from_counts({"handled": 1, "mystery_status": 9})
    assert line.startswith("HELD: 1 executed attempts")


def test_declaration_files_are_never_targets(tmp_path):
    # gauntlet catch 2: lib/defu.d.cts became a target and dead-ended the
    # run; type declarations have no runtime behavior to break
    import subprocess

    from agentboard.config import targets_from_diff
    r = str(tmp_path)
    for cmd in (["init", "-q", "-b", "main"],
                ["config", "user.email", "t@t"],
                ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", r, *cmd], check=True,
                       capture_output=True)
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "x.d.cts").write_text("export type X = 1\n")
    (tmp_path / "lib" / "real.ts").write_text("export const y = 1\n")
    subprocess.run(["git", "-C", r, "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", r, "commit", "-q", "-m", "i"], check=True,
                   capture_output=True)
    (tmp_path / "lib" / "x.d.cts").write_text("export type X = 2\n")
    (tmp_path / "lib" / "real.ts").write_text("export const y = 2\n")
    out = targets_from_diff(r, "HEAD", worktree=True)
    assert out == ["lib/real.ts"]


def test_tests_from_diff_is_worktree_aware(tmp_path):
    # gauntlet catch 4: worktree mode passes head="" and the triple-dot
    # diffed nothing, losing a test file the change itself had named
    import subprocess

    from agentboard.api import _tests_from_diff
    r = str(tmp_path)
    for cmd in (["init", "-q", "-b", "main"],
                ["config", "user.email", "t@t"],
                ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", r, *cmd], check=True,
                       capture_output=True)
    (tmp_path / "a.py").write_text("X = 1\n")
    (tmp_path / "test_a.py").write_text("import a\n")
    subprocess.run(["git", "-C", r, "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", r, "commit", "-q", "-m", "i"], check=True,
                   capture_output=True)
    (tmp_path / "a.py").write_text("X = 2\n")
    (tmp_path / "test_a.py").write_text("import a\nassert a\n")
    assert _tests_from_diff(r, "HEAD", "") == ["test_a.py"]
