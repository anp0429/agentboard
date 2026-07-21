"""Iteration one: a backend persona and an SRE persona review a real repo.

The verifier runs the repo's actual pytest suite on every proposed change. The
outcomes below are not staged — they were measured against the real suite:

  * backend tightens MATCH_TOL 60 -> 15  -> the suite goes RED -> REJECTED,
    with the real failing test as the reason. The world said wrong.
  * SRE appends a planned scale-relative constant -> GREEN -> committed.
  * backend and SRE both retune ATTACH_TOL, to 50 vs 12 -> both GREEN -> the
    tests cannot adjudicate a precision-vs-robustness call -> CONFLICT, the
    human gate decides.

Usage (point REPO at a clone of the repo under review):

    python examples/run_repo_review.py /path/to/svg-graph-parser
"""
import sys

from agentboard import build_loop, initial_board
from agentboard.experimental.ingestion.repo_adapter import RepoIngestionAdapter
from agentboard.experimental.verifiers.pytest_verifier import PytestVerifier
from agentboard.experimental.whiteboards.flow_adapter import FlowWhiteboardAdapter
from agentboard.experimental.state import CodeChange, Proposal

CONST = "svg_graph_parser/world1/constants.py"

PERSONAS = [
    ("backend", "Ships features; wants tighter, more precise behavior."),
    ("sre", "Guards reliability; wary of changes that reduce robustness."),
]


class RepoStubAgent:
    """Deterministic agent carrying the validated edits. Implements ``Agent``.
    Swap for an LLM agent by implementing the same propose() method."""

    def propose(self, persona, goal, nodes, prior_committed, iteration):
        if iteration != 1 or CONST not in {n.id for n in nodes}:
            return []  # nothing new on later passes -> the loop converges

        if persona == "backend":
            return [
                Proposal("be-match", "backend", "issue", CONST, severity="medium",
                         text="Endpoint match tolerance is loose (60px); risks wrong attachments."),
                Proposal("be-match-fix", "backend", "fix", CONST, targets="be-match",
                         text="Tighten MATCH_TOL from 60 to 15 for precision.",
                         change=CodeChange(CONST, find="MATCH_TOL = 60.0", replace="MATCH_TOL = 15.0")),
                Proposal("attach", "backend", "issue", CONST, severity="medium",
                         text="Arrowhead attach tolerance (30px) needs tuning."),
                Proposal("be-attach-fix", "backend", "fix", CONST, targets="attach",
                         text="Loosen ATTACH_TOL to 50 — catch more arrowheads (recall).",
                         change=CodeChange(CONST, find="ATTACH_TOL = 30.0", replace="ATTACH_TOL = 50.0")),
            ]
        if persona == "sre":
            return [
                Proposal("sre-scale", "sre", "issue", CONST, severity="low",
                         text="Tolerances are absolute pixels; won't transfer across scales."),
                Proposal("sre-scale-fix", "sre", "fix", CONST, targets="sre-scale",
                         text="Add a planned scale-relative fallback constant + TODO.",
                         change=CodeChange(CONST, append=(
                             "# Scale-relative migration (planned, raised by SRE)\n"
                             "CHAR_LEN_FALLBACK = 1.0  # TODO: derive tolerances from this"))),
                Proposal("sre-attach-fix", "sre", "fix", CONST, targets="attach",
                         text="Tighten ATTACH_TOL to 12 — avoid false attaches (precision).",
                         change=CodeChange(CONST, find="ATTACH_TOL = 30.0", replace="ATTACH_TOL = 12.0")),
            ]
        return []


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else "svggp_clone"
    nodes = RepoIngestionAdapter(repo, "svg_graph_parser").ingest()
    print(f"ingested {len(nodes)} modules from {repo}")

    app = build_loop(
        agent=RepoStubAgent(),
        verifier=PytestVerifier(repo, test_args=["-q"]),
        whiteboard=FlowWhiteboardAdapter(path="agentboard_repo.html"),
        personas=PERSONAS,
        budget=3,
    )
    final = app.invoke(initial_board("Backend + SRE review the geometry tolerances.", nodes, budget=3),
                       {"configurable": {"thread_id": "repo-1"}})

    print(f"status     : {final['status']}")
    for snap in final["snapshots"]:
        print(f"  iteration {snap.iteration}: {snap.summary}")
        for r in snap.rejected:
            print(f"    REJECTED {r.proposal.persona}: {r.reason}")
        for c in snap.conflicts:
            who = " vs ".join(p.persona for p in c.proposals)
            print(f"    CONFLICT on {c.node_ref}: {who}")
    print(f"board      : {final['board_location']}")


if __name__ == "__main__":
    main()
