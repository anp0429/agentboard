"""Per-file review — one focused reviewer call per changed file.

WHY (measured + reasoned):
  - One call over the whole 34k diff spreads the model's attention across ~10
    files; a rare case (composite FK) surfaced 3/5. Cost is also one big prompt.
  - Reviewing each changed file in its OWN call keeps every change (nothing
    omitted — the failure of "scope to one file") while giving each file a
    focused, cheaper prompt. Focus may also lift coverage of rare cases.

HONEST LIMIT: per-file review catches per-file bugs. A bug that only appears when
file A's change interacts with file B's needs a whole-change pass. The critic
(optional, below) is that pass. So: per-file for depth, one whole-change critic
pass for cross-file gaps.

Nothing about the GATE changes. Findings from all files are merged and each is
verified the same deterministic way.

    PR_HEAD=HEAD PR_BASE=upstream/main CLONE=/path python examples/run_review_perfile.py
"""
import os

from agentboard.ingestion.intent import resolve_intent
from agentboard.ingestion.pr_diff import load_pr_diff
from agentboard.agents.reviewer_agent import ReviewerAgent
from agentboard.verifiers.finding_verifier import FindingVerifier
from agentboard.verifiers.vitest_verifier import RepoProfile
from agentboard.review import ReviewRun, render_review_html

CLONE = os.environ.get("CLONE", "/path/to/your/clone")
TESTS = "packages/mcp-server-supabase/src/server.test.ts"
ISSUE_URL = "https://github.com/supabase/mcp/issues/277"
PR_HEAD = os.environ.get("PR_HEAD", "HEAD")
PR_BASE = os.environ.get("PR_BASE", "upstream/main")
REVIEWER_MODEL = "gpt-5.5"

# only review changed SOURCE files worth reviewing (skip lockfiles, snapshots…)
def _reviewable(path: str) -> bool:
    return path.endswith((".ts", ".tsx", ".js", ".sql")) and ".test." not in path


def main():
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("missing OPENAI_API_KEY")

    intent = resolve_intent(issue_url=ISSUE_URL, goal=None)
    diff = load_pr_diff(CLONE, head=PR_HEAD, base=PR_BASE)
    changed = [f for f in diff.files if _reviewable(f.path) and f.added.strip()]
    print(f"{len(diff.files)} changed files, {len(changed)} reviewable\n")

    profile = RepoProfile.pnpm_vitest("supabase-mcp",
        filter="@supabase/mcp-server-supabase", project="unit", build=False)
    profile.build_cmd = ["pnpm", "--filter", "@supabase/mcp-utils", "build"]
    verifier = FindingVerifier(CLONE, profile, tests_file=TESTS, timeout=2400)

    all_findings = []
    for cf in changed:
        # each file reviewed in its OWN call: the reviewer's source context is the
        # changed file itself, and `change` is just that file's added lines.
        print(f"reviewing {cf.path} ({len(cf.added)} chars changed)…")
        agent = ReviewerAgent(CLONE, target_path=cf.path,
                              existing_tests_path=TESTS, model=REVIEWER_MODEL)
        findings = agent.review(intent, change=cf.added)
        print(f"  -> {len(findings)} behaviors")
        for f in findings:
            f.coverage_note = f"[{cf.path}] {f.coverage_note}"[:200]
        all_findings += findings

    print(f"\nmerged {len(all_findings)} findings from {len(changed)} files. "
          f"running gate…\n")
    review = ReviewRun(intent=intent, target="(per-file)", findings=all_findings)
    verifier.run(review)

    for f in review.findings:
        print(f"  [{f.status:14}] {f.behavior[:60]}")
        if f.observed and f.status in ("confirmed_gap", "broken_test"):
            print(f"                  -> {f.observed[:90]}")

    out = render_review_html(review, "./review_board.html")
    print(f"\n{len(review.gaps)} confirmed gap(s). Board: {out}")


if __name__ == "__main__":
    main()
