"""Real run: GPT proposes a change + its test, the TransitionVerifier gates it
against the actual supabase/mcp clone, and Claude gives the second opinion.

Set both keys before running:
    export OPENAI_API_KEY=sk-...
    export ANTHROPIC_API_KEY=sk-ant-...

Then:
    python examples/run_fk_graph.py

Expect the FIRST runs to REJECT. Writing a vitest test that genuinely fails on
the current code and passes after a real change to a TS monorepo is hard, and
the verifier is strict on purpose. A rejection with a clear reason is the system
WORKING, not failing — read the reason, tighten, rerun. Validate the plumbing
with SMOKE_GOAL first (an easy red->green change), then switch to FK_GOAL.
"""
import os
import time
from agentboard.loop import build_loop, initial_board
from agentboard.state import Node
from agentboard.agents.fix_with_test_agent import FixWithTestAgent
from agentboard.agents.peer_reviewer import PeerReviewer, anthropic_review_fn
from agentboard.verifiers.transition_verifier import TransitionVerifier
from agentboard.verifiers.vitest_verifier import RepoProfile
from agentboard.ingestion.intent import resolve_intent
from agentboard.whiteboards.html_adapter import HtmlWhiteboardAdapter

# --- edit these paths --------------------------------------------------------
CLONE = os.environ.get("CLONE", "/path/to/your/clone")          # your clone of the PR branch
TARGET = "packages/mcp-server-supabase/src/tools/database-docs-tools.ts"
# -----------------------------------------------------------------------------

# --- choose intent: an issue URL, OR a plain goal string (exactly one) --------
ISSUE_URL = "https://github.com/supabase/mcp/issues/277"
GOAL_STRING = None   # e.g. "Expose foreign keys as a traversable edge list ..."
# -----------------------------------------------------------------------------


def main():
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        if not os.environ.get(k):
            raise SystemExit(f"missing {k}")

    goal = resolve_intent(issue_url=ISSUE_URL, goal=GOAL_STRING)
    print("INTENT:\n", goal[:300], "\n")

    node = Node(id=TARGET, label="generate_schema_docs tool")

    agent = FixWithTestAgent(CLONE, model="gpt-4o", focus_modules=[TARGET])
    # build only the dependency the tests need; skip the server pkg's tsc gate
    profile = RepoProfile.pnpm_vitest("supabase-mcp",
        filter="@supabase/mcp-server-supabase", project="unit", build=False)
    profile.build_cmd = ["pnpm", "--filter", "@supabase/mcp-utils", "build"]
    verifier = TransitionVerifier(CLONE, profile, timeout=2400)
    reviewer = PeerReviewer(anthropic_review_fn(model="claude-haiku-4-5-20251001"),
                            reviewer_name="claude-reviewer")
    
    whiteboard = HtmlWhiteboardAdapter(path=f"./board-{int(time.time())}.html")


    app = build_loop(
        agent=agent,
        verifier=verifier,
        reviewer=reviewer,
        whiteboard=whiteboard,
        personas=[("backend", "correctness and precision of behavior")],
        budget=2,
        gate_on_high_severity_conflict=True,
    )
    board = initial_board(goal=goal, nodes=[node], budget=2)
    print("Running (first real run does several pnpm installs — minutes)…\n")
    final = app.invoke(board, config={"configurable": {"thread_id": "fk-run"}})

    print("status     :", final["status"])
    print("needs_human:", final["needs_human"])
    print("committed  :", [p.id for p in final["committed"]])
    for s in final["snapshots"]:
        print(f"\n-- iteration {s.iteration}: {s.summary}")
        for p in s.accepted:
            print("   ACCEPTED:", p.id, "—", p.text)
        for r in s.rejected:
            print("   REJECTED:", r.proposal.id, "—", r.reason)
        for c in s.conflicts:
            print("   CONFLICT:", c.note)
    print("\nboard:", final["board_location"])


if __name__ == "__main__":
    main()