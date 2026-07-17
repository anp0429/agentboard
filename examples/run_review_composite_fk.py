"""The review pipeline, end to end.

    intent  ->  ReviewerAgent (GPT derives behaviors + writes tests)
            ->  FindingVerifier (runs each test on the branch, classifies)
            ->  render_review_html (the audit board)

Set your key:  export OPENAI_API_KEY=sk-...
Then:          python examples/run_review.py

The intent is DATA (an issue URL or a string) — the agent is generic.
The agent is NOT told what to look for. The honest question this answers:
does it independently propose the composite-FK case (and others), and does the
gate confirm which are real gaps?
"""
import os

from agentboard.ingestion.intent import resolve_intent
from agentboard.agents.reviewer_agent import ReviewerAgent
from agentboard.agents.critic_agent import CriticAgent
from agentboard.agents.gap_auditor import GapAuditor
from agentboard.verifiers.finding_verifier import FindingVerifier
from agentboard.verifiers.vitest_verifier import RepoProfile, SUPABASE_MCP
from agentboard.fingerprint import verdict_summary
from agentboard.proposal_cache import propose_or_cached
from agentboard.review import ReviewRun, render_review_html


# --- edit these --------------------------------------------------------------
# --- edit these --------------------------------------------------------------
CLONE = os.environ.get("CLONE", "/Users/ankita/Documents/supabase-mcp")
TARGET = "packages/mcp-server-supabase/src/tools/database-operation-tools.ts"
TESTS = "packages/mcp-server-supabase/src/server.test.ts"
ISSUE_URL = None
GOAL_STRING = """Issue (the problem): list_tables verbose output represented composite
foreign keys incorrectly. Originally a pg-meta SQL cross-join paired every source
column with every target column (N^2 rows, fabricating relationships). An interim
fix paired columns positionally but still emitted one row per column pair, so
nothing signaled that two rows belong to one atomic constraint vs two independent FKs.

PR (the claimed fix): composite FKs are now grouped into a single constraint object:
{ name, source_table, source_columns: [...], target_table, target_columns: [...] }.
The SQL in pg-meta/tables.sql uses unnest(conkey, confkey) WITH ORDINALITY so column
pairing is positional by construction, and both arrays aggregate ordered by constraint
ordinality (source_columns[i] pairs with target_columns[i], in constraint-definition
order — not alphabetical, not attnum). Single-column FKs become one-element arrays.
source_table is kept because relationships include constraints where the listed table
is the target. The behavior under test lives in the SQL, exercised through the
list_tables tool output."""
PR_HEAD = "composite-fk-fix"
PR_BASE = "upstream/main"
REVIEWER_MODEL = "gpt-5.5"   # <- swap to "claude-opus-4-8" for the head-to-head
RUN_CRITIC = True            # <- set False to compare first-pass instinct only (one variable)
CRITIC_MODEL = "gpt-5.5"     # only used if RUN_CRITIC
AUDIT_MODEL = "claude-opus-4-8"  # precision layer — use a DIFFERENT model than the proposer
# -----------------------------------------------------------------------------


def main():
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("missing OPENAI_API_KEY")

    intent = resolve_intent(issue_url=ISSUE_URL, goal=GOAL_STRING)
    print("INTENT:\n", intent[:300], "\n")

    # what the PR actually changed (vs the merge-base) — reviewed against the intent
    change = ""
    if PR_HEAD:
        try:
            from agentboard.ingestion.pr_diff import load_pr_diff, diff_blob
            change = diff_blob(load_pr_diff(CLONE, head=PR_HEAD, base=PR_BASE))
            print(f"CHANGE: {len(change)} chars of diff loaded ({PR_HEAD} vs {PR_BASE})\n")
        except Exception as e:
            print(f"[warn] could not load PR diff ({e}); reviewing whole file instead\n")

    # build only the dependency the tests need; skip the tsc prebuild gate
    profile = RepoProfile.pnpm_vitest("supabase-mcp",
        filter="@supabase/mcp-server-supabase", project="unit", build=False)
    profile.build_cmd = ["pnpm", "--filter", "@supabase/mcp-utils", "build"]
    profile.harness_notes = SUPABASE_MCP.harness_notes
    agent = ReviewerAgent(
        repo_root=CLONE,
        target_path=TARGET,
        existing_tests_path=TESTS,
        model=REVIEWER_MODEL,
        harness_notes=profile.harness_notes,   # <- the new line
    )
    verifier = FindingVerifier(CLONE, profile, tests_file=TESTS, timeout=2400)

    src = open(f"{CLONE}/{TARGET}", encoding="utf-8").read()
    tst = open(f"{CLONE}/{TESTS}", encoding="utf-8").read()
    critic = CriticAgent(model=CRITIC_MODEL) if RUN_CRITIC else None
    print(f"Proposing (reviewer {REVIEWER_MODEL}"
          + (f" + critic {CRITIC_MODEL}" if RUN_CRITIC else "") + ")…")
    all_findings = propose_or_cached(
        agent, critic, intent=intent, change=change, source=src, tests=tst
    )
    print(f"  {len(all_findings)} behavior(s) to gate:")
    for g in all_findings:
        print(f"    + {g.behavior[:70]}")
    print(f"\nRunning {len(all_findings)} findings through the gate…\n")

    review = ReviewRun(intent=intent, target=TARGET, findings=all_findings)
    verifier.run(review)   # classifies each finding against the branch

    # Precision layer: a DIFFERENT model audits each confirmed_gap against the
    # source for false positives. ADVISORY ONLY — never changes the verdict.
    gaps = [f for f in review.findings if f.status == "confirmed_gap"]
    if gaps:
        print(f"\nAuditing {len(gaps)} confirmed gap(s) with {AUDIT_MODEL} (precision layer)…")
        # The auditor must see the SAME code the reviewer saw. A gap's mechanism
        # often lives in a DIFFERENT changed file than TARGET (the composite-FK
        # cartesian product is built in pg-meta, not in database-docs-tools.ts).
        # Feeding only TARGET made the auditor blind -> honest but useless
        # "uncertain" with a blank reason. Give it the whole change.
        target_src = open(f"{CLONE}/{TARGET}", encoding="utf-8").read()
        audit_src = (
            f"FILE UNDER REVIEW ({TARGET}):\n{target_src}\n\n"
            f"THE FULL CHANGE (all files this PR touched — the mechanism may live here):\n{change}"
            if change else target_src
        )
        GapAuditor(model=AUDIT_MODEL, max_source_chars=120000).audit_all(audit_src, review.findings)

    for f in review.findings:
        print(f"  [{f.status:14}] {f.axis:11} {f.behavior[:60]}")
        if f.observed:
            print(f"                  -> {f.observed[:90]}")
        if f.status == "confirmed_gap" and f.audit:
            print(f"                  AUDITOR: {f.audit} — {f.audit_reason[:80]}")

    out = render_review_html(review, "./review_board.html")
    print(f"\n{verdict_summary(review)}")
    print(f"{len(review.gaps)} confirmed gap(s). Board: {out}")


if __name__ == "__main__":
    main()