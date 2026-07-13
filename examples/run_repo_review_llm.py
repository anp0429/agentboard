"""Iteration one with a REAL brain: an OpenAI-backed backend + SRE panel review a
real repo, and the verifier runs the real test suite on whatever they propose.

Setup:
    pip install openai
    export OPENAI_API_KEY=sk-...        # you have this
    python examples/run_repo_review_llm.py ../svggp_clone

The agent reads the focus modules, proposes its own issues and diffs, and the
PytestVerifier accepts or rejects each by running your tests. Unlike the stub,
the findings are now generated from your actual code — and anything the model
gets wrong (a bad patch, a change that breaks a test) is caught by the verifier
and shown on the board with the real reason.
"""
import sys

from agentboard import build_loop, initial_board
from agentboard.agents.openai_agent import OpenAIAgent
from agentboard.agents.openai_explainer import OpenAIExplainer
from agentboard.ingestion.repo_adapter import RepoIngestionAdapter
from agentboard.verifiers.pytest_verifier import PytestVerifier
from agentboard.whiteboards.flow_adapter import FlowWhiteboardAdapter

PERSONAS = [
    ("backend", "Ships features; wants precise behavior."),
    ("sre", "Guards reliability; wary of reduced safety margins."),
]

# keep it cheap + focused: review a couple of core modules, not all 21
FOCUS = [
    "svg_graph_parser/world1/constants.py",
    "svg_graph_parser/world1/connect.py",
]


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else "svggp_clone"
    model = sys.argv[2] if len(sys.argv) > 2 else "gpt-4o-mini"
    nodes = RepoIngestionAdapter(repo, "svg_graph_parser").ingest()
    print(f"ingested {len(nodes)} modules; reviewing {len(FOCUS)} with model={model}")

    agent = OpenAIAgent(repo_root=repo, model=model, focus_modules=FOCUS)
    explainer = OpenAIExplainer(repo_root=repo, model=model)
    app = build_loop(
        agent=agent,
        verifier=PytestVerifier(repo, test_args=["-q", "--tb=line", "-rf"]),
        whiteboard=FlowWhiteboardAdapter(path="agentboard_repo_llm.html"),
        personas=PERSONAS,
        budget=2,
        explainer=explainer,
    )
    final = app.invoke(
        initial_board("Backend + SRE review these modules for problems and fixes.", nodes, budget=2),
        {"configurable": {"thread_id": "repo-llm-1"}},
    )

    print(f"status     : {final['status']}")
    for snap in final["snapshots"]:
        print(f"  iteration {snap.iteration}: {snap.summary}")
        for p in snap.accepted:
            print(f"    OK  {p.persona} [{p.kind}] {p.text}")
        for r in snap.rejected:
            print(f"    NO  {r.proposal.persona}: {r.reason}")
        for c in snap.conflicts:
            print(f"    CONFLICT on {c.node_ref}: {' vs '.join(p.persona for p in c.proposals)}")
    print(f"board      : {final['board_location']}")


if __name__ == "__main__":
    main()
