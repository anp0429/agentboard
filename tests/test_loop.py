"""Tests. The point: the verifier and the loop are deterministic and checkable
WITHOUT an LLM. That testability is the whole reason verification lives outside
the generative path.
"""
import pytest

# The whiteboard/loop path is the legacy API and needs the [whiteboard] extra
# (LangGraph). The core review gate does not, so a lean install skips this
# file rather than failing to import it.
pytest.importorskip("langgraph")

from agentboard import (
    Node,
    Proposal,
    SchemaVerifier,
    TextIngestionAdapter,
    build_loop,
    initial_board,
)


def test_verifier_rejects_unknown_node():
    nodes = [Node(id="a", label="A")]
    good = Proposal(id="p1", persona="x", kind="issue", node_ref="a", text="ok")
    ghost = Proposal(id="p2", persona="x", kind="issue", node_ref="ghost", text="bad")
    accepted, rejected = SchemaVerifier().verify([good, ghost], nodes, [])
    assert [p.id for p in accepted] == ["p1"]
    assert len(rejected) == 1 and "unknown node" in rejected[0].reason


def test_verifier_rejects_fix_with_unknown_target():
    nodes = [Node(id="a", label="A")]
    fix = Proposal(id="f", persona="x", kind="fix", node_ref="a", text="fix", targets="nope")
    accepted, rejected = SchemaVerifier().verify([fix], nodes, [])
    assert not accepted and "unknown issue" in rejected[0].reason


def test_loop_converges_and_logs_snapshots():
    nodes = TextIngestionAdapter().ingest("- Order service\n- Payment service")
    app = build_loop(budget=6)
    final = app.invoke(initial_board("g", nodes, budget=6), {"configurable": {"thread_id": "t"}})
    assert final["status"] == "converged"
    # iteration 1 issues + iteration 2 fixes, then it stops adding -> >= 2 snapshots
    assert len(final["snapshots"]) >= 2
    # the ghost-node fix from the stub must have been rejected, never committed
    assert all(p.node_ref != "ghost-node" for p in final["committed"])
    # at least one rejection was recorded somewhere in the log
    assert any(snap.rejected for snap in final["snapshots"])


if __name__ == "__main__":
    test_verifier_rejects_unknown_node()
    test_verifier_rejects_fix_with_unknown_target()
    test_loop_converges_and_logs_snapshots()
    print("all tests passed")
