"""The snapshot loop, built on LangGraph.

LangGraph is the engine — we do not rebuild orchestration. We add the layer it
does not have: verify-before-commit plus an append-only, projected snapshot log.

One iteration is four nodes:

    agents    each persona proposes blindly (sees only prior committed state)
    verify    the external verifier accepts/rejects; conflicts are detected
    snapshot  the committed delta is recorded and the board is re-projected
    route     converged? budget spent? blocking conflict? -> stop, else loop

Durable execution comes from LangGraph: the loop is compiled with a checkpointer
(swap MemorySaver for PostgresSaver/RedisSaver in production), so every iteration
is a resumable checkpoint. The human gate in v0.1 halts the run with status
``needs_human`` and leaves the conflict on the board for a person to resolve. To
make it a native pause/resume, replace that halt with LangGraph's ``interrupt``:

    from langgraph.types import interrupt, Command   # the v0.2 upgrade
    # in snapshot_node, when needs_human:
    #     decision = interrupt({"conflict": conflicts})
    # then resume with: app.invoke(Command(resume=decision), config)

That resumes from the last checkpoint, not from scratch — the durable-execution
property that makes this worth building on LangGraph rather than rolling your own.
"""
from __future__ import annotations

from typing import Sequence

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .interfaces import Agent, Verifier, WhiteboardAdapter
from .personas import DEFAULT_PERSONAS, StubAgent
from .state import Board, Conflict, Proposal, Rejection, Snapshot
from .verifiers.schema_verifier import SchemaVerifier
from .whiteboards.html_adapter import HtmlWhiteboardAdapter


def _detect_conflicts(accepted: list[Proposal]) -> list[Conflict]:
    """Two committed fixes that touch the *same thing* from different personas
    conflict. For code changes "the same thing" is the exact edit target (so two
    different edits to one line conflict, but unrelated edits to the same file do
    not). We surface these; we never silently pick a winner."""
    groups: dict[str, list[Proposal]] = {}
    for p in accepted:
        if p.kind != "fix":
            continue
        key = p.change.key() if p.change else p.node_ref
        groups.setdefault(key, []).append(p)
    conflicts = []
    for key, props in groups.items():
        personas = {p.persona for p in props}
        replaces = {(p.change.replace if p.change else p.text) for p in props}
        if len(props) > 1 and len(personas) > 1 and len(replaces) > 1:
            conflicts.append(
                Conflict(
                    node_ref=props[0].node_ref,
                    proposals=props,
                    note="personas disagree on this edit — tests pass either way, human decides",
                )
            )
    return conflicts


def build_loop(
    agent: Agent | None = None,
    verifier: Verifier | None = None,
    whiteboard: WhiteboardAdapter | None = None,
    personas: Sequence[tuple[str, str]] | None = None,
    budget: int = 4,
    gate_on_high_severity_conflict: bool = False,
    explainer: object | None = None,
    reviewer: object | None = None,
):
    """Compile and return a runnable LangGraph app.

    All four pieces are swappable; the defaults run with no API key. Set
    ``gate_on_high_severity_conflict=True`` to make a blocking conflict pause the
    run for a human (interrupt). Default False so the demo runs unattended.
    """
    agent = agent or StubAgent()
    verifier = verifier or SchemaVerifier()
    whiteboard = whiteboard or HtmlWhiteboardAdapter()
    personas = list(personas or DEFAULT_PERSONAS)

    def agents_node(state: Board) -> dict:
        proposals: list[Proposal] = []
        for name, _focus in personas:
            proposals += agent.propose(
                persona=name,
                goal=state["goal"],
                nodes=state["nodes"],
                prior_committed=state.get("committed", []),
                iteration=state["iteration"],
            )
        return {"last_proposals": proposals}

    def verify_node(state: Board) -> dict:
        accepted, rejected = verifier.verify(
            state["last_proposals"], state["nodes"], state.get("committed", [])
        )
        conflicts = _detect_conflicts(accepted)
        peer_needs_human = False
        if reviewer is not None:
            # SECOND model: taste only, never the gate. It surfaces disagreement
            # as conflicts and can request a human; it cannot un-accept anything.
            peer_conflicts, peer_needs_human = reviewer.review(
                state["goal"], accepted, state["nodes"]
            )
            conflicts = conflicts + peer_conflicts
        if explainer is not None:
            for r in rejected:
                if hasattr(explainer, "explain_rejection"):
                    r.explanation = explainer.explain_rejection(r.proposal, r.reason)
            for c in conflicts:
                if hasattr(explainer, "explain_conflict"):
                    c.explanation = explainer.explain_conflict(c)
        return {
            "committed": accepted,        # reducer appends to the running list
            "last_rejections": rejected,
            "last_conflicts": conflicts,
            "last_delta": len(accepted),
            "needs_human": peer_needs_human,
        }

    def snapshot_node(state: Board) -> dict:
        it = state["iteration"]
        rejected: list[Rejection] = state.get("last_rejections", [])
        conflicts: list[Conflict] = state.get("last_conflicts", [])
        delta = state.get("last_delta", 0)
        accepted_this_iter = state["committed"][-delta:] if delta else []

        summary = (
            f"{delta} committed · {len(rejected)} rejected · {len(conflicts)} conflict(s)"
        )
        snap = Snapshot(
            iteration=it,
            accepted=accepted_this_iter,
            rejected=rejected,
            conflicts=conflicts,
            summary=summary,
        )

        # decide status BEFORE projecting so the board reflects the final call
        next_status = "running"
        needs_human = state.get("needs_human", False)  # may be set by the peer reviewer
        if delta == 0:
            next_status = "converged"
        elif it >= budget:
            next_status = "budget_exhausted"
        if needs_human or (
            gate_on_high_severity_conflict
            and any(any(p.severity == "high" for p in c.proposals) for c in conflicts)
        ):
            next_status = "needs_human"
            needs_human = True

        # project the cumulative log (this snapshot included) to the board
        all_snaps = state.get("snapshots", []) + [snap]
        location = whiteboard.project(state["goal"], state["nodes"], all_snaps)

        return {
            "snapshots": [snap],          # reducer appends
            "iteration": it + 1,
            "status": next_status,
            "needs_human": needs_human,
            "board_location": location,
        }

    def route(state: Board) -> str:
        return "stop" if state["status"] != "running" else "loop"

    g = StateGraph(Board)
    g.add_node("agents", agents_node)
    g.add_node("verify", verify_node)
    g.add_node("snapshot", snapshot_node)
    g.add_edge(START, "agents")
    g.add_edge("agents", "verify")
    g.add_edge("verify", "snapshot")
    g.add_conditional_edges("snapshot", route, {"loop": "agents", "stop": END})

    return g.compile(checkpointer=MemorySaver())


def initial_board(goal: str, nodes, budget: int = 4) -> Board:
    """Build a fresh blackboard for a run."""
    return Board(
        goal=goal,
        nodes=list(nodes),
        iteration=1,
        budget=budget,
        committed=[],
        snapshots=[],
        last_proposals=[],
        last_rejections=[],
        last_conflicts=[],
        last_delta=0,
        status="running",
        needs_human=False,
        board_location="",
    )