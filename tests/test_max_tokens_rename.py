"""OpenAI renamed max_tokens to max_completion_tokens; compatible servers
did not. providers.chat_completion absorbs the rename with a single retry
on the one specific 400, instead of keeping a vendor list.

Found live: the first review run against real api.openai.com (gpt-5.5)
failed with this 400 and proposed 0 behaviors; every prior run had gone
through OpenAI-compatible endpoints (Ollama, OpenRouter) that still speak
max_tokens."""

import pytest

from agentboard.providers import chat_completion


class _Rejecting:
    """Fake client whose create() rejects max_tokens the way OpenAI does."""

    def __init__(self):
        self.calls = []

        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(dict(kwargs))
                if "max_tokens" in kwargs:
                    raise RuntimeError(
                        "Error code: 400 - Unsupported parameter: 'max_tokens' "
                        "is not supported with this model. "
                        "Use 'max_completion_tokens' instead."
                    )
                return {"ok": True, "kwargs": kwargs}

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


class _Accepting(_Rejecting):
    """Fake compatible server: max_tokens works first try."""

    def __init__(self):
        super().__init__()

        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(dict(kwargs))
                return {"ok": True, "kwargs": kwargs}

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def test_rename_rejection_is_retried_with_new_name():
    client = _Rejecting()
    resp = chat_completion(client, model="gpt-x", max_tokens=6000, messages=[])
    assert resp["ok"]
    assert len(client.calls) == 2
    assert "max_tokens" in client.calls[0]
    assert "max_tokens" not in client.calls[1]
    # not the starved original budget: the retry carries reasoning headroom
    assert client.calls[1]["max_completion_tokens"] == 24000


def test_compatible_server_gets_one_call_with_max_tokens():
    client = _Accepting()
    resp = chat_completion(client, model="qwen3", max_tokens=6000, messages=[])
    assert resp["ok"]
    assert len(client.calls) == 1
    assert client.calls[0]["max_tokens"] == 6000


def test_unrelated_errors_propagate_without_retry():
    class _Broken(_Rejecting):
        def __init__(self):
            super().__init__()
            outer = self

            class _Completions:
                def create(self, **kwargs):
                    outer.calls.append(dict(kwargs))
                    raise RuntimeError("Error code: 401 - invalid api key")

            class _Chat:
                completions = _Completions()

            self.chat = _Chat()

    client = _Broken()
    with pytest.raises(RuntimeError, match="401"):
        chat_completion(client, model="gpt-x", max_tokens=6000, messages=[])
    assert len(client.calls) == 1


def test_rename_retry_raises_reasoning_headroom():
    # The rename rejection only fires for real OpenAI reasoning models,
    # whose budget covers hidden reasoning tokens before any output. The
    # retry must carry real headroom, not re-send the starved 6000.
    client = _Rejecting()
    chat_completion(client, model="gpt-x", max_tokens=6000, messages=[])
    assert client.calls[1]["max_completion_tokens"] == 24000


def test_rename_retry_scales_with_a_larger_configured_budget():
    client = _Rejecting()
    chat_completion(client, model="gpt-x", max_tokens=10000, messages=[])
    assert client.calls[1]["max_completion_tokens"] == 40000
