"""Run the loop end to end with zero setup and no API key.

    python examples/run_text_review.py

Ingests a tiny text "architecture", runs the persona panel through the
verify-before-commit loop until it converges, and writes an iteration-tagged
whiteboard you can open in a browser.
"""
from agentboard import TextIngestionAdapter, build_loop, initial_board
from agentboard.experimental.whiteboards.flow_adapter import FlowWhiteboardAdapter

SOURCE = """
- Order service
- Payment service
- Outbound webhook call
"""

GOAL = "Review this service design and surface problems and fixes."


def main() -> None:
    nodes = TextIngestionAdapter().ingest(SOURCE)
    board = FlowWhiteboardAdapter(path="agentboard_demo.html")
    app = build_loop(whiteboard=board, budget=4)

    config = {"configurable": {"thread_id": "demo-1"}}
    final = app.invoke(initial_board(GOAL, nodes, budget=4), config)

    print(f"status        : {final['status']}")
    print(f"iterations    : {len(final['snapshots'])}")
    print(f"committed     : {len(final['committed'])} proposals")
    for snap in final["snapshots"]:
        print(f"  iteration {snap.iteration}: {snap.summary}")
    print(f"board written : {final['board_location']}")


if __name__ == "__main__":
    main()
