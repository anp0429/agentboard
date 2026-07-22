"""agentboard — a code review gate that verifies by executing tests.

A model proposes edge-case tests from your intent and your change; a
deterministic harness runs each one against the real code in a clean
checkout; a behavior is a gap only if its test runs and fails. No model is in
the pass/fail decision. The entry point is the `agentboard` CLI
(`agentboard.cli:main`).

The legacy whiteboard/loop API (built on LangGraph) now lives under
`agentboard.experimental`; its top-level aliases below are still importable
from this package, but only when the optional `whiteboard` extra is installed
(`pip install "agentboard[whiteboard]"`). It is not needed for the review
gate, so a lean install does not pull LangGraph, and the imports below degrade
to absent rather than crashing `import agentboard`.
"""

# Single source of truth is pyproject.toml; read it from installed metadata
# so this can never drift again (it sat at 0.1.0 through four releases).
try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("reviewgate")
except Exception:  # not installed (e.g. running from a bare checkout)
    __version__ = "0.0.0.dev0"

# Legacy whiteboard exports — available only with the [whiteboard] extra.
# Wrapped so a lean install (review gate only) imports cleanly without
# LangGraph. If you need these symbols, install the extra.
try:  # pragma: no cover - exercised by the [whiteboard] install path
    from .experimental.loop import build_loop, initial_board
    from .experimental.personas import DEFAULT_PERSONAS, StubAgent
    from .experimental.state import (
        Board,
        CodeChange,
        Conflict,
        Node,
        Proposal,
        Rejection,
        Snapshot,
    )
    from .experimental.verifiers.schema_verifier import SchemaVerifier
    from .experimental.whiteboards.html_adapter import HtmlWhiteboardAdapter
    from .experimental.whiteboards.flow_adapter import FlowWhiteboardAdapter
    from .experimental.ingestion.text_adapter import TextIngestionAdapter
    from .experimental.ingestion.repo_adapter import RepoIngestionAdapter
    from .experimental.verifiers.pytest_verifier import PytestVerifier
    from .experimental.agents.openai_agent import OpenAIAgent

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
except ImportError:  # langgraph (the [whiteboard] extra) not installed
    __all__ = []
