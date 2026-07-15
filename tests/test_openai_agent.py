"""Offline tests for the OpenAI agent parser. No network, no openai package
needed — we test the fragile part (model JSON -> valid Proposals) directly, and
a fake client to exercise the full propose() path.
"""
from agentboard.agents.openai_agent import OpenAIAgent, parse_response

SOURCE = "MATCH_TOL = 60.0  # tolerance\nATTACH_TOL = 30.0\n"


def test_parse_builds_issue_and_fix_with_change():
    data = {
        "issues": [{"id": "i1", "severity": "high", "text": "Tolerance too loose."}],
        "fixes": [{"id": "f1", "targets": "i1", "text": "Tighten it.",
                   "find": "MATCH_TOL = 60.0", "replace": "MATCH_TOL = 20.0"}],
    }
    props = parse_response("backend", "pkg/mod.py", SOURCE, data)
    kinds = {p.kind for p in props}
    assert kinds == {"issue", "fix"}
    fix = next(p for p in props if p.kind == "fix")
    assert fix.change is not None and fix.change.find == "MATCH_TOL = 60.0"
    # targets were remapped to the globally-unique issue id
    issue = next(p for p in props if p.kind == "issue")
    assert fix.targets == issue.id


def test_parse_tolerates_garbage():
    assert parse_response("sre", "m.py", SOURCE, {}) == []
    assert parse_response("sre", "m.py", SOURCE, {"issues": [{}], "fixes": [{}]}) == []
    # a fix missing find/replace becomes a text-only proposal (no change to run)
    props = parse_response("sre", "m.py", SOURCE, {"fixes": [{"id": "f1", "text": "do it"}]})
    assert len(props) == 1 and props[0].change is None


class _FakeChoice:
    def __init__(self, content): self.message = type("M", (), {"content": content})


class _FakeClient:
    """Stands in for the OpenAI client; returns canned JSON."""
    def __init__(self, content): self._c = content
    @property
    def chat(self): return self
    @property
    def completions(self): return self
    def create(self, **kw):
        return type("R", (), {"choices": [_FakeChoice(self._c)]})


def test_propose_full_path_with_fake_client(tmp_path):
    (tmp_path / "pkg").mkdir()
    f = tmp_path / "pkg" / "mod.py"
    f.write_text(SOURCE)
    content = '{"issues":[{"id":"i1","severity":"low","text":"x"}],"fixes":[]}'
    agent = OpenAIAgent(repo_root=str(tmp_path), client=_FakeClient(content),
                        focus_modules=["pkg/mod.py"])
    from agentboard.state import Node
    props = agent.propose("backend", "g", [Node("pkg/mod.py", "mod.py")], [], 1)
    assert len(props) == 1 and props[0].kind == "issue"
    # converges: nothing on iteration 2
    assert agent.propose("backend", "g", [Node("pkg/mod.py", "mod.py")], [], 2) == []


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
