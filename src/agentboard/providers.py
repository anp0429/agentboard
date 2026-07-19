"""Model-to-provider routing, in ONE place.

The rule: a model whose name starts with "claude" talks to Anthropic; every
other model talks to an OpenAI-COMPATIBLE endpoint. The second bucket is
deliberately open. gpt-*/o* reach api.openai.com by default, and setting
OPENAI_BASE_URL points the same client at any compatible server (Ollama,
LM Studio, vLLM), which is how local models wire in with zero new config
surface: name the model in .agentboard.toml, export OPENAI_BASE_URL, done.

This used to be `model.startswith("gpt") or model.startswith("o")` pasted
into four files, which routed every non-OpenAI, non-Claude name (qwen,
devstral, ...) to the Anthropic client, where it could only fail. Same
lesson as the failure classifier: shared rules get one brain, or they
drift. Encode the rule, not the vendor list.
"""

from __future__ import annotations

import os


def uses_anthropic(model: str) -> bool:
    return model.lower().startswith("claude")


def anthropic_client():
    from anthropic import Anthropic

    return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def openai_client():
    """OpenAI-compatible client. Honors OPENAI_BASE_URL for local servers;
    a local endpoint that ignores auth still gets a placeholder key because
    the SDK requires one to construct at all."""
    from openai import OpenAI

    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or None
    api_key = os.environ.get("OPENAI_API_KEY") or ("local" if base_url else None)
    return OpenAI(base_url=base_url, api_key=api_key)


def client_for(model: str):
    return anthropic_client() if uses_anthropic(model) else openai_client()
