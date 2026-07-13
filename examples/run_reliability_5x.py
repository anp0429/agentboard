"""Reliability measurement — run the SAME config N times and COUNT.

Two hand runs both caught the composite-FK bug. Two is not reliable. This freezes
the config, runs it N times with NO memory (the agent re-proposes freshly every
run — that IS the experiment), and reports the only numbers that matter:

  - how many of N runs produced a composite-FK finding that the gate turned RED
  - how many produced one but it slipped through (handled / weak assertion)
  - false-positive count per run (noise you'd be handing a human)

Do NOT add memory here. Memory suppresses re-proposing, which would fake
consistency by hiding the variable being measured.

Uses reuse_warm=True so install/build happens ONCE for all N runs, not per run.

    PR_HEAD=HEAD PR_BASE=upstream/main CLONE=/path python examples/run_reliability_5x.py
    N=5 python examples/run_reliability_5x.py     # override run count
"""
import os
import re

from agentboard.ingestion.intent import resolve_intent
from agentboard.ingestion.pr_diff import load_pr_diff, diff_blob
from agentboard.agents.reviewer_agent import ReviewerAgent
from agentboard.agents.critic_agent import CriticAgent
from agentboard.verifiers.finding_verifier import FindingVerifier
from agentboard.verifiers.vitest_verifier import RepoProfile
from agentboard.review import ReviewRun

# --- frozen config (same as run_review.py) -----------------------------------
CLONE = os.environ.get("CLONE", "/path/to/your/clone")
TARGET = "packages/mcp-server-supabase/src/tools/database-docs-tools.ts"
TESTS = "packages/mcp-server-supabase/src/server.test.ts"
ISSUE_URL = "https://github.com/supabase/mcp/issues/277"
PR_HEAD = os.environ.get("PR_HEAD", "HEAD")
PR_BASE = os.environ.get("PR_BASE", "upstream/main")
REVIEWER_MODEL = "gpt-5.5"
CRITIC_MODEL = "gpt-5.5"
N = int(os.environ.get("N", "5"))
# -----------------------------------------------------------------------------

# a finding is "about composite FK" if its behavior mentions a compound/composite
# foreign key. Detection is on the BEHAVIOR text, deterministic.
_FK = re.compile(r"(composite|compound|multi[- ]?column).*(foreign key|fk)"
                 r"|(foreign key|fk).*(composite|compound|multi[- ]?column)", re.I)


def is_composite_fk(behavior: str) -> bool:
    return bool(_FK.search(behavior or ""))


def main():
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("missing OPENAI_API_KEY")

    intent = resolve_intent(issue_url=ISSUE_URL, goal=None)
    change = diff_blob(load_pr_diff(CLONE, head=PR_HEAD, base=PR_BASE))
    print(f"config frozen. {N} runs. diff={len(change)} chars. memory OFF.\n")

    profile = RepoProfile.pnpm_vitest("supabase-mcp",
        filter="@supabase/mcp-server-supabase", project="unit", build=False)
    profile.build_cmd = ["pnpm", "--filter", "@supabase/mcp-utils", "build"]
    # reuse_warm: install/build ONCE across all N runs
    verifier = FindingVerifier(CLONE, profile, tests_file=TESTS, timeout=2400, reuse_warm=True)

    src = open(f"{CLONE}/{TARGET}", encoding="utf-8").read()
    tst = open(f"{CLONE}/{TESTS}", encoding="utf-8").read()

    fk_proposed = fk_confirmed = fk_slipped = 0
    per_run = []

    try:
        for i in range(1, N + 1):
            print(f"--- run {i}/{N} ---")
            reviewer = ReviewerAgent(CLONE, TARGET, TESTS, model=REVIEWER_MODEL)
            findings = reviewer.review(intent, change=change)
            critic = CriticAgent(model=CRITIC_MODEL)
            gaps = critic.critique(intent, src, tst, findings)
            all_findings = findings + gaps

            review = ReviewRun(intent=intent, target=TARGET, findings=all_findings)
            verifier.run(review)

            fk = [f for f in review.findings if is_composite_fk(f.behavior)]
            fk_gap = [f for f in fk if f.status == "confirmed_gap"]
            fk_miss = [f for f in fk if f.status in ("handled", "skipped_covered")]
            false_pos = sum(1 for f in review.findings
                            if f.status == "confirmed_gap" and not is_composite_fk(f.behavior))

            proposed = bool(fk)
            caught = bool(fk_gap)
            slipped = bool(fk_miss) and not caught
            fk_proposed += proposed
            fk_confirmed += caught
            fk_slipped += slipped

            status = ("CAUGHT (red)" if caught else
                      "SLIPPED (handled)" if slipped else
                      "not proposed")
            per_run.append((i, status, len(review.gaps), false_pos))
            print(f"  composite FK: {status} | total gaps: {len(review.gaps)} "
                  f"| non-FK gaps (noise): {false_pos}\n")
    finally:
        verifier.close()

    print("=" * 56)
    print(f"RELIABILITY over {N} runs (same config, memory off):")
    print(f"  composite FK proposed ....... {fk_proposed}/{N}")
    print(f"  composite FK CAUGHT (red) ... {fk_confirmed}/{N}   <- the number that matters")
    print(f"  composite FK slipped ........ {fk_slipped}/{N}")
    print()
    print(f"  {'run':>3}  {'composite FK':<18} {'total gaps':>10} {'noise gaps':>11}")
    for i, status, gaps, noise in per_run:
        print(f"  {i:>3}  {status:<18} {gaps:>10} {noise:>11}")
    print("=" * 56)
    if fk_confirmed == N:
        print("CAUGHT every run. Reliability solved by this config.")
    else:
        print(f"CAUGHT {fk_confirmed}/{N}. Not reliable yet — this is the number the "
              "shape-enumeration loop must move to N/N.")


if __name__ == "__main__":
    main()
