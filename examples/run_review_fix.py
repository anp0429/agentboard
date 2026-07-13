"""The full loop on one branch: find -> prove -> fix -> prove the fix.

Pipeline:
  1. ReviewerAgent + CriticAgent propose behaviors as tests.
  2. FindingVerifier (deterministic gate) turns each red or green.
  3. For each confirmed_gap: FixAgent proposes a minimal CodeChange.
  4. TransitionVerifier judges the fix with the finding's OWN red test:
       baseline -> test alone RED (tautology guard)
                -> fix + test GREEN
                -> nothing that passed at baseline fails now.
  5. Everything — finding, gate verdict, fix, fix verdict — on one board.

Invariants preserved: no LLM in any accept/reject path. The FixAgent only
proposes; both gates are deterministic and external.

COST WARNING: each fix verification is ~3 full install+build+test runs
(baseline is cached across fixes). FIX_LIMIT (default 1) caps how many gaps
get the fix stage per run. Raise it deliberately.

    CLONE=/path/to/clone PR_HEAD=HEAD PR_BASE=upstream/main \
      python examples/run_review_fix.py
"""
import os

from agentboard.ingestion.intent import resolve_intent
from agentboard.ingestion.pr_diff import load_pr_diff, diff_blob
from agentboard.agents.reviewer_agent import ReviewerAgent
from agentboard.agents.critic_agent import CriticAgent
from agentboard.agents.fix_agent import FixAgent
from agentboard.verifiers.finding_verifier import FindingVerifier
from agentboard.verifiers.transition_verifier import TransitionVerifier
from agentboard.verifiers.vitest_verifier import RepoProfile
from agentboard.review import ReviewRun, render_review_html
from agentboard.state import CodeChange

CLONE = os.environ.get("CLONE", "/path/to/your/clone")
TARGET = os.environ.get(
    "TARGET", "packages/mcp-server-supabase/src/tools/database-docs-tools.ts")
TESTS = os.environ.get(
    "TESTS", "packages/mcp-server-supabase/src/server.test.ts")
ISSUE_URL = os.environ.get(
    "ISSUE_URL", "https://github.com/supabase/mcp/issues/277")
PR_HEAD = os.environ.get("PR_HEAD", "HEAD")
PR_BASE = os.environ.get("PR_BASE", "upstream/main")
REVIEWER_MODEL = os.environ.get("REVIEWER_MODEL", "gpt-5.5")
FIX_MODEL = os.environ.get("FIX_MODEL", "gpt-5.5")
FIX_LIMIT = int(os.environ.get("FIX_LIMIT", "1"))


def main():
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("missing OPENAI_API_KEY")

    # ---- 1-2: review + gate (unchanged pipeline) -------------------------
    intent = resolve_intent(issue_url=ISSUE_URL, goal=None)
    change = diff_blob(load_pr_diff(CLONE, head=PR_HEAD, base=PR_BASE))
    print(f"CHANGE: {len(change)} chars of diff loaded ({PR_HEAD} vs {PR_BASE})")

    reviewer = ReviewerAgent(CLONE, TARGET, TESTS, model=REVIEWER_MODEL)
    findings = reviewer.review(intent, change=change)
    print(f"reviewer proposed {len(findings)} behaviors")
    critic = CriticAgent(model=REVIEWER_MODEL)
    src = open(os.path.join(CLONE, TARGET), encoding="utf-8").read()
    tst = open(os.path.join(CLONE, TESTS), encoding="utf-8").read()
    findings += critic.critique(intent, src, tst, findings)

    profile = RepoProfile.pnpm_vitest(
        "supabase-mcp", filter="@supabase/mcp-server-supabase",
        project="unit", build=False)
    profile.build_cmd = ["pnpm", "--filter", "@supabase/mcp-utils", "build"]

    review = ReviewRun(intent=intent, target=TARGET, findings=findings)
    FindingVerifier(CLONE, profile, tests_file=TESTS, timeout=2400).run(review)
    gaps = review.gaps
    print(f"\ngate: {len(gaps)} confirmed gap(s) of {len(review.findings)} findings")

    # ---- 3-4: fix stage on confirmed gaps --------------------------------
    if gaps:
        fixer = FixAgent(CLONE, model=FIX_MODEL)
        judge = TransitionVerifier(CLONE, profile, timeout=2400)
        for f in gaps[:FIX_LIMIT]:
            print(f"\nfix stage: {f.behavior[:70]}")
            if not f.test_code:
                f.fix_status, f.fix_note = "fix_not_attempted", "gap has no test to transition"
                continue
            change_prop, note = fixer.propose(
                f.behavior, f.observed, f.test_code, TARGET)
            if change_prop is None:
                f.fix_status, f.fix_note = "fix_not_attempted", note
                print(f"  no fix: {note}")
                continue
            print(f"  proposed: {note or change_prop.find[:60]}")
            test_change = CodeChange(path=TESTS, append="\n" + f.test_code + "\n")
            ok, reason = judge.verify_transition(change_prop, test_change)
            f.fix_status = "fix_verified" if ok else "fix_rejected"
            f.fix_note = reason
            f.fix_change = f"{change_prop.path}: `{change_prop.find[:80]}` -> `{(change_prop.replace or '')[:80]}`"
            print(f"  gate: {f.fix_status} — {reason}")
        for f in gaps[FIX_LIMIT:]:
            f.fix_status, f.fix_note = "fix_not_attempted", f"skipped (FIX_LIMIT={FIX_LIMIT})"

    # ---- 5: one board -----------------------------------------------------
    out = render_review_html(review, "./review_board.html")
    print(f"\nboard: {out}")


if __name__ == "__main__":
    main()
