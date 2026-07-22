"""The collaboration-loop architecture that predates the review gate.

This subpackage holds the earlier blackboard/loop stack: fix agents that
propose patches the gate verifies red to green, and multi-model argument
(personas, peer review, whiteboard adapters) with executed tests as the
referee. It seeds the roadmap but is not imported by the review path
(`agentboard review` / `agentboard.api`), which lives in the parent
package. The loop itself needs the `[whiteboard]` extra (LangGraph);
everything here is importable without it except `experimental.loop`.
"""
