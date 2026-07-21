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


def openai_client(base_url: str = ""):
    """OpenAI-compatible client. Precedence: explicit base_url argument
    (a repo's config pinning its provider), then OPENAI_BASE_URL, then
    api.openai.com. A local endpoint that ignores auth still gets a
    placeholder key because the SDK requires one to construct at all."""
    from openai import OpenAI

    base = (base_url or os.environ.get("OPENAI_BASE_URL", "")).strip() or None
    api_key = os.environ.get("OPENAI_API_KEY") or ("local" if base else None)
    return OpenAI(base_url=base, api_key=api_key)


def client_for(model: str, base_url: str = ""):
    return anthropic_client() if uses_anthropic(model) else openai_client(base_url)


def chat_completion(client, **kwargs):
    """client.chat.completions.create with the max-tokens rename absorbed.

    OpenAI renamed max_tokens to max_completion_tokens for its current
    models and rejects the old name with a 400. OpenAI-COMPATIBLE servers
    (Ollama, OpenRouter, vLLM) still speak max_tokens and may not know the
    new name. Same lesson as uses_anthropic: encode the rule, not a vendor
    list. Send the widely-understood name; on the one specific rejection,
    retry once with the new name. Found live: the first-ever review run
    against real api.openai.com (gpt-5.5) failed with exactly this 400
    while every prior run had gone through compatible endpoints.
    """
    try:
        return client.chat.completions.create(**kwargs)
    except Exception as e:
        message = str(e)
        if ("max_tokens" in kwargs
                and "max_tokens" in message
                and "max_completion_tokens" in message):
            kwargs = dict(kwargs)
            budget = kwargs.pop("max_tokens")
            # The rename rejection only ever comes from real OpenAI
            # reasoning models, and for those the budget covers hidden
            # reasoning tokens BEFORE any output: 6000 can be consumed
            # entirely by thinking, returning an empty completion that
            # reads as "0 behaviors" with no error at all. Give the retry
            # real headroom. Compatible endpoints never reach this branch,
            # so their tight cap (metered routers reserve the ceiling
            # against the account balance) is untouched.
            kwargs["max_completion_tokens"] = max(4 * budget, 24000)
            return client.chat.completions.create(**kwargs)
        raise


def endpoint_label(model: str, base_url: str = "") -> str:
    """Human-readable name of the endpoint a model will actually talk to.

    Printed at review start so a stray OPENAI_BASE_URL (a .zshrc that
    points at a local Ollama, an OpenRouter export left over from another
    session) is visible in the run log instead of silently redefining what
    a model name means. Precedence mirrors openai_client: explicit
    base_url argument, then the environment, then api.openai.com.
    """
    if uses_anthropic(model):
        return "anthropic"
    base = (base_url or os.environ.get("OPENAI_BASE_URL", "")).strip()
    if not base:
        return "api.openai.com"
    from urllib.parse import urlparse

    return urlparse(base).netloc or base
