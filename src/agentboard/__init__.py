"""agentboard — project any multi-agent run as an iteration-tagged whiteboard.

Generic in mechanism, narrow in purpose: a bounded, self-verifying agent loop
built on LangGraph that documents every iteration onto a whiteboard you can open.
"""
from .loop import build_loop, initial_board
from .personas import DEFAULT_PERSONAS, StubAgent
from .state import Board, CodeChange, Conflict, Node, Proposal, Rejection, Snapshot
from .verifiers.schema_verifier import SchemaVerifier
from .whiteboards.html_adapter import HtmlWhiteboardAdapter
from .whiteboards.flow_adapter import FlowWhiteboardAdapter
from .ingestion.text_adapter import TextIngestionAdapter
from .ingestion.repo_adapter import RepoIngestionAdapter
from .verifiers.pytest_verifier import PytestVerifier
from .agents.openai_agent import OpenAIAgent

__version__ = "0.1.0"

__all__ = [
    "build_loop",
    "initial_board",
    "DEFAULT_PERSONAS",
    "StubAgent",
    "SchemaVerifier",
    "HtmlWhiteboardAdapter",
    "FlowWhiteboardAdapter",
    "TextIngestionAdapter",
    "RepoIngestionAdapter",
    "PytestVerifier",
    "OpenAIAgent",
    "Board",
    "Node",
    "Proposal",
    "CodeChange",
    "Rejection",
    "Conflict",
    "Snapshot",
]
