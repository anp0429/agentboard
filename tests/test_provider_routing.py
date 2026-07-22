"""Provider routing: one rule, four consumers, zero drift.

The old rule (`startswith("gpt") or startswith("o")`) lived in four files
and sent every non-OpenAI, non-Claude model name to the Anthropic client,
where it could only crash. The new rule lives in providers.py: claude* is
Anthropic, everything else is OpenAI-compatible, and OPENAI_BASE_URL points
that client at a local server (Ollama, LM Studio, vLLM). These tests pin
the rule, each consumer's use of it, the preflight key logic, and the
openai-path leniency that local models need."""

import os
import types

import pytest

from agentboard.agents.critic_agent import CriticAgent
from agentboard.agents.gap_auditor import GapAuditor
from agentboard.agents.reviewer_agent import ReviewerAgent, _loads_lenient
from agentboard.providers import openai_client, uses_anthropic


# -- the rule itself ---------------------------------------------------------

@pytest.mark.parametrize("model,anthropic", [
    ("claude-opus-4-8", True),
    ("Claude-sonnet", True),          # case-insensitive
    ("gpt-5.5", False),
    ("o3-mini", False),
    ("qwen3.6:27b", False),           # local names are OpenAI-compatible...
    ("devstral-small-2", False),      # ...never Anthropic (the old bug)
    ("llama4:70b", False),
])
def test_routing_rule(model, anthropic):
    assert uses_anthropic(model) is anthropic


def test_every_agent_shares_the_rule():
    """A local model name must select the openai branch in all three agents.
    Under the old per-file rule, 'qwen*' selected the Anthropic branch."""
    r = ReviewerAgent("/r", "a.ts", "a.test.ts", model="qwen3.6:27b")
    c = CriticAgent(model="qwen3.6:27b")
    g = GapAuditor(model="qwen3.6:27b")
    assert r._is_openai and c._is_openai and g._is_openai
    r2 = ReviewerAgent("/r", "a.ts", "a.test.ts", model="claude-opus-4-8")
    assert not r2._is_openai


# -- the client construction -------------------------------------------------

def test_base_url_gets_placeholder_key(monkeypatch):
    """A local endpoint ignores auth, but the SDK refuses to construct
    without a key; the placeholder bridges that without touching real env."""
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = openai_client()
    assert client.api_key == "local"
    assert "localhost:11434" in str(client.base_url)


def test_real_key_wins_over_placeholder(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    assert openai_client().api_key == "sk-real"


# -- preflight key logic -----------------------------------------------------

def _preflight_keys(monkeypatch, model, base_url=None, tmp_path=None):
    import subprocess
    from agentboard.config import preflight
    r = str(tmp_path)
    subprocess.run(["git", "-C", r, "init", "-q"], check=True)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if base_url:
        monkeypatch.setenv("OPENAI_BASE_URL", base_url)
    else:
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    return [p for p in preflight(
        repo_root=r, head="HEAD", base="HEAD", target="", tests="",
        reviewer_model=model, need_critic=False, critic_model="",
    ) if "missing" in p and "KEY" in p]


def test_preflight_local_endpoint_needs_no_key(monkeypatch, tmp_path):
    assert _preflight_keys(
        monkeypatch, "qwen3.6:27b",
        base_url="http://localhost:11434/v1", tmp_path=tmp_path) == []


def test_preflight_cloud_still_demands_key(monkeypatch, tmp_path):
    probs = _preflight_keys(monkeypatch, "gpt-5.5", tmp_path=tmp_path)
    assert any("OPENAI_API_KEY" in p for p in probs)
    # and the message teaches the local escape hatch
    assert any("OPENAI_BASE_URL" in p for p in probs)


def test_preflight_claude_unaffected_by_base_url(monkeypatch, tmp_path):
    """OPENAI_BASE_URL must not waive the ANTHROPIC key for claude models."""
    probs = _preflight_keys(
        monkeypatch, "claude-opus-4-8",
        base_url="http://localhost:11434/v1", tmp_path=tmp_path)
    assert any("ANTHROPIC_API_KEY" in p for p in probs)


# -- the openai path end to end with an injected local-style client ----------

class _FakeCompletions:
    def __init__(self, content):
        self._content = content
    def create(self, **kw):
        msg = types.SimpleNamespace(content=self._content)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=msg)])


def _fake_client(content):
    return types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=_FakeCompletions(content)))


def test_local_model_fenced_json_still_parses():
    """Local models sometimes fence JSON despite response_format; the openai
    path must salvage it, same as the anthropic path always has."""
    fenced = ('```json\n{"behaviors": [{"behavior": "clamps at max", '
              '"test_code": "test(...)"}]}\n```')
    agent = ReviewerAgent("/r", "a.ts", "a.test.ts",
                          model="qwen3.6:27b", client=_fake_client(fenced))
    found = agent.review(intent="clamp page size")
    assert [f.behavior for f in found] == ["clamps at max"]


def test_fenced_salvage_helper_direct():
    out = _loads_lenient('```json\n{"behaviors": [{"behavior": "x"}]}\n```')
    assert out["behaviors"] and out["behaviors"][0]["behavior"] == "x"


# ---------------------------------------------------------------------------
# endpoint visibility and key-shape truth (the key-mess cleanup)
# ---------------------------------------------------------------------------

def test_endpoint_label_default_is_openai(monkeypatch):
    from agentboard.providers import endpoint_label
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert endpoint_label("gpt-5.5") == "api.openai.com"


def test_endpoint_label_shows_env_redirect(monkeypatch):
    from agentboard.providers import endpoint_label
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
    assert endpoint_label("gpt-5.5") == "localhost:11434"


def test_endpoint_label_explicit_pin_beats_env(monkeypatch):
    from agentboard.providers import endpoint_label
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
    assert endpoint_label("qwen3", "https://openrouter.ai/api/v1") == "openrouter.ai"


def test_endpoint_label_claude_is_anthropic(monkeypatch):
    from agentboard.providers import endpoint_label
    assert endpoint_label("claude-opus-4-8") == "anthropic"


def test_config_base_url_key_is_loaded(tmp_path):
    from agentboard.config import load_config
    p = tmp_path / "cfg.toml"
    p.write_text('base_url = "http://localhost:11434/v1"\n')
    cfg = load_config(str(tmp_path), str(p))
    assert cfg.base_url == "http://localhost:11434/v1"


def test_openrouter_key_without_base_url_fails_preflight(tmp_path, monkeypatch):
    import subprocess

    from agentboard.config import preflight

    repo = str(tmp_path / "r")
    os.makedirs(repo)
    subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-or-v1-abc123")
    problems = preflight(
        repo_root=repo, head="HEAD", base="HEAD", target="", tests="",
        reviewer_model="gpt-5.5", need_critic=False, critic_model="gpt-5.5",
        worktree=True,
    )
    assert any("OpenRouter key" in p for p in problems)


def test_openrouter_key_with_base_url_is_fine(tmp_path, monkeypatch):
    import subprocess

    from agentboard.config import preflight

    repo = str(tmp_path / "r")
    os.makedirs(repo)
    subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-or-v1-abc123")
    problems = preflight(
        repo_root=repo, head="HEAD", base="HEAD", target="", tests="",
        reviewer_model="gpt-5.5", need_critic=False, critic_model="gpt-5.5",
        worktree=True,
    )
    assert not any("OpenRouter key" in p for p in problems)
