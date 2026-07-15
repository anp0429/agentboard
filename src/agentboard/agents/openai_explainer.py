"""Optional interpretation layer: turn a deterministic verdict into a narrative.

This is the part that answers "what was it trying to do, why did it break, what's
the tradeoff" in plain language. It is LLM output — interpretation, not fact — so
the board labels it as "why:" and it never overrides the deterministic verdict.

Pass an instance as ``build_loop(explainer=...)``. Without one, the board still
shows the facts (intent, diff, which behavior broke); it just won't narrate.
"""
from __future__ import annotations

import os

from ..state import Conflict, Proposal


def _diff(p: Proposal) -> str:
    c = p.change
    if c is None:
        return "(no code change)"
    if c.append is not None:
        return f"append: {c.append.strip()[:120]}"
    return f"{(c.find or '').strip()} -> {(c.replace or '').strip()}"


class OpenAIExplainer:
    """Implements explain_rejection / explain_conflict via the OpenAI API."""

    def __init__(self, repo_root: str, model: str = "gpt-4o-mini",
                 max_source_chars: int = 4000, client=None, temperature: float = 0.2):
        self.repo_root = repo_root
        self.model = model
        self.max_source_chars = max_source_chars
        self._client = client
        self.temperature = temperature

    def _client_lazy(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI()
        return self._client

    def _source(self, node_ref: str) -> str:
        try:
            with open(os.path.join(self.repo_root, node_ref), encoding="utf-8") as f:
                return f.read()[: self.max_source_chars]
        except OSError:
            return ""

    def _ask(self, system: str, user: str) -> str:
        try:
            r = self._client_lazy().chat.completions.create(
                model=self.model, temperature=self.temperature,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
            )
            return (r.choices[0].message.content or "").strip()[:240]
        except Exception:
            return ""  # narrative is optional; never break the run

    def explain_rejection(self, proposal: Proposal, reason: str) -> str:
        system = ("You explain a failed code change to an engineer in 1-2 plain "
                  "sentences: what the change intended, and why it broke that test. "
                  "Be concrete. No preamble.")
        user = (f"File:\n```\n{self._source(proposal.node_ref)}\n```\n"
                f"Change: {_diff(proposal)}\nIntent: {proposal.text}\n"
                f"Verifier result: {reason}\nExplain what happened.")
        return self._ask(system, user)

    def explain_conflict(self, conflict: Conflict) -> str:
        system = ("Two changes both pass the tests but disagree. In 1-2 plain "
                  "sentences explain the tradeoff and what each side optimizes, so a "
                  "human can choose. No preamble.")
        edits = "\n".join(f"- {p.persona}: {_diff(p)} ({p.text})" for p in conflict.proposals)
        user = (f"File:\n```\n{self._source(conflict.node_ref)}\n```\n"
                f"Competing changes:\n{edits}\nExplain the tradeoff.")
        return self._ask(system, user)
