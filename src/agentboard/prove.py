"""prove: the author-side verb. An agent (or you) wrote code; prove tries
to break it and reports one line first: BROKEN, HELD, or STOPPED.

This module is the deliberately boring part: everything here is pure or
git-read-only, so the wording rules that make prove trustworthy are pinned
by unit tests, not by hope. The engine is untouched — cli.prove() builds a
ReviewRequest from a ProvePlan and api.run_review does what it always does.

Honesty rules (tested below in tests/test_prove_plan.py):
- "HELD" never claims correctness. It reports executed attempts that
  failed to break the change, and says so in those words.
- Zero executed attempts is never silently green. If nothing new ran, the
  line says what happened instead (covered / broken / nothing proposed).
- broken_test is agentboard's problem, not the user's: it appears as a
  parenthetical count, never in the headline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .config import (
    current_branch,
    fork_point,
    intent_from_commits,
    targets_from_diff,
    working_tree_dirty,
)
from .review import ReviewRun

def prove_board_path(now=None) -> str:
    """A per-verb, non-overwriting board name: prove_board_<stamp>.html in
    the system temp dir. Timestamped so tonight's board can sit next to
    yesterday's — the board is the primary human surface, and comparing
    runs is half its value."""
    import tempfile
    import time
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
    return os.path.join(tempfile.gettempdir(),
                        f"agentboard_prove_board_{stamp}.html")


NO_KEY_SCREEN = """\
No LLM configured. prove needs a model to propose tests
(the verdict itself never uses one). Pick an exit:

  export OPENAI_API_KEY=sk-...     any OpenAI API key
  export OPENAI_BASE_URL=http://localhost:11434/v1
                                   free + local via Ollama (or any
                                   OpenAI-compatible server)
  agentboard demo                  watch it work right now, no key at all
"""


def llm_configured(environ=None, config_base_url: str = "") -> bool:
    """True when a proposer model is reachable in principle: a key, or a
    base URL from env or repo config (local servers need no key)."""
    env = os.environ if environ is None else environ
    return bool(
        env.get("OPENAI_API_KEY", "").strip()
        or env.get("OPENAI_BASE_URL", "").strip()
        or config_base_url.strip()
    )


@dataclass
class ProvePlan:
    """Everything prove decided so the user didn't have to type it."""

    worktree: bool
    head: str
    base: str
    targets: list[str] = field(default_factory=list)
    intent: str = ""
    intent_source: str = ""  # "flag" | "commits" | "" (missing)
    diffed: str = ""         # human sentence: what was compared


def plan_prove(repo_root: str, intent_arg: str = "") -> ProvePlan:
    """Fill in prove's zero-flag defaults. Dirty tracked tree -> worktree
    mode against HEAD (the state on disk is the thing under test). Clean
    tree -> current branch against its fork point. Targets come from the
    corresponding diff, tests excluded, sorted."""
    if working_tree_dirty(repo_root):
        head = current_branch(repo_root)
        plan = ProvePlan(
            worktree=True, head=head, base="HEAD",
            targets=targets_from_diff(repo_root, "HEAD", worktree=True),
            diffed=f"working tree vs HEAD (uncommitted changes on {head})",
        )
    else:
        head = current_branch(repo_root)
        base = fork_point(repo_root, head) or "main"
        plan = ProvePlan(
            worktree=False, head=head, base=base,
            targets=targets_from_diff(repo_root, base, head),
            diffed=f"branch {head} vs its fork point {base[:12]}",
        )
    if intent_arg.strip():
        plan.intent, plan.intent_source = intent_arg.strip(), "flag"
    elif not plan.worktree:
        derived = intent_from_commits(repo_root, plan.base, plan.head)
        if derived:
            plan.intent, plan.intent_source = derived, "commits"
    return plan


def verdict_block(run: ReviewRun) -> str:
    """The one-line-first summary. Counting rules: an attempt was
    'executed' only if its test ran to a real result (handled or
    confirmed_gap). broken_test and skipped_covered never count as
    executed; timed_out is reported as inconclusive, never as either
    side's win."""
    n = {"handled": 0, "confirmed_gap": 0, "broken_test": 0,
         "skipped_covered": 0, "timed_out": 0}
    for f in run.findings:
        if f.status in n:
            n[f.status] += 1
    return verdict_from_counts(n, run.env_error)


def verdict_from_counts(counts: dict, env_error: str = "") -> str:
    """The same summary from a verdict_counts dict — the shape the schema
    v1 artifact carries — so adapters that hold the artifact (the MCP
    server) speak the identical wording without duplicating the rules."""
    n = {"handled": 0, "confirmed_gap": 0, "broken_test": 0,
         "skipped_covered": 0, "timed_out": 0}
    n.update({k: v for k, v in (counts or {}).items() if k in n})
    gaps, held = n["confirmed_gap"], n["handled"]
    executed = gaps + held
    extras = []
    if n["skipped_covered"]:
        extras.append(f"{n['skipped_covered']} already covered by existing tests")
    if n["broken_test"]:
        extras.append(f"{n['broken_test']} proposals broke before running")
    if n["timed_out"]:
        extras.append(f"{n['timed_out']} timed out (inconclusive)")
    tail = f" ({'; '.join(extras)})" if extras else ""

    if env_error:
        return f"STOPPED: {env_error.splitlines()[0]}"
    if gaps:
        plural = "tests" if gaps != 1 else "test"
        return (f"BROKEN: {gaps} failing {plural}, "
                f"{executed} attempts executed{tail}")
    if executed:
        return (f"HELD: {executed} executed attempts, 0 broke it{tail}\n"
                f"      (this is absence of a counterexample among "
                f"{executed} attempts, not a proof of correctness)")
    if n["skipped_covered"]:
        return (f"NOTHING NEW EXECUTED: {n['skipped_covered']} proposed "
                f"behaviors are already covered by your existing tests"
                + (f"; {n['broken_test']} proposals broke before running"
                   if n["broken_test"] else ""))
    if n["broken_test"] or n["timed_out"]:
        return ("STOPPED: no proposal executed cleanly" + tail +
                " — this is agentboard's problem, not evidence about your code")
    return "STOPPED: nothing was proposed, so nothing was tested"


def gap_details(run: ReviewRun) -> list[str]:
    """One entry per failing test: what broke and where to see it. The
    runnable evidence lives in the board; here we surface the behavior and
    the observed failure, first line only."""
    out = []
    for f in run.gaps:
        first = (f.observed or "").strip().splitlines()
        out.append(f"  - {f.behavior}\n"
                   f"    observed: {first[0] if first else '(see board)'}")
    return out


def exit_code_for(run: ReviewRun | None) -> int:
    """0 = held or nothing-new-covered, 2 = broken, 1 = could not run OR
    ran but produced no evidence at all. Distinct codes because prove's
    caller is often an agent loop, and BROKEN must be programmatically
    distinguishable from HELD. The last rule exists because the gate's own
    first self-review caught the mismatch: a run where every proposal
    broke printed STOPPED but exited 0. The exit code and the verdict line
    must always tell the same story."""
    if run is None:
        return 1
    if run.env_error:
        return 1
    executed = covered = 0
    for f in run.findings:
        if f.status in ("handled", "confirmed_gap"):
            executed += 1
        elif f.status == "skipped_covered":
            covered += 1
    if run.gaps:
        return 2
    if executed or covered:
        return 0
    return 1  # STOPPED: only broken/timed-out/nothing — no evidence exists
