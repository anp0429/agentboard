"""An OpenAI-backed agent. Reads a module, proposes issues and fixes with real
diffs, and hands them to the verifier — which still runs the tests.

Design notes that matter:

* The LLM call and the parsing are split. ``parse_response`` is a pure function
  with no network and no openai dependency, so the fragile part (turning model
  JSON into valid Proposals) is unit-testable offline.
* A fix's ``find`` must be an exact substring of the file or the patch won't
  apply. We do not try to force that here — if the model hallucinates an anchor,
  the PytestVerifier rejects it with "anchor not found", which is the external
  check doing its job. Honest over clever.
* It proposes only on iteration 1, then returns nothing, so the loop converges.
"""
from __future__ import annotations

import json
import os
import re

from ..state import CodeChange, Node, Proposal

_PERSONA_FOCUS = {
    "backend": "correctness and precision of behavior; you ship features.",
    "sre": "reliability and robustness; you distrust changes that reduce safety margins.",
    "security": "trust boundaries, input validation, and unsafe defaults.",
    "simplicity": "removing complexity; you push back on overengineering.",
}

_SYSTEM = (
    "You are a senior {persona} engineer reviewing one source file. Your lens: {focus}\n"
    "Propose concrete, SMALL changes. Output ONLY JSON, no prose, with this shape:\n"
    '{{"issues":[{{"id":"i1","severity":"low|medium|high","text":"<one sentence>"}}],'
    '"fixes":[{{"id":"f1","targets":"i1","text":"<one sentence>",'
    '"find":"<exact substring copied verbatim from the file>",'
    '"replace":"<the replacement text>"}}]}}\n'
    "Rules: `find` MUST be copied character-for-character from the file and appear "
    "exactly once. Keep edits surgical (a line or two). If you only want to flag a "
    "concern with no code change, add an issue and no fix. Propose at most 2 issues "
    "and 2 fixes."
)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:24]


def parse_response(persona: str, node_id: str, source: str, data: dict) -> list[Proposal]:
    """Turn the model's JSON into Proposals. Pure, defensive, offline-testable."""
    out: list[Proposal] = []
    mod = _slug(node_id.split("/")[-1])
    idmap: dict[str, str] = {}

    for raw in (data.get("issues") or [])[:2]:
        local = str(raw.get("id", f"i{len(out)}"))
        gid = f"{persona}:{mod}:{local}"
        idmap[local] = gid
        sev = raw.get("severity", "medium")
        if sev not in ("low", "medium", "high"):
            sev = "medium"
        text = str(raw.get("text", "")).strip()
        if text:
            out.append(Proposal(gid, persona, "issue", node_id, text, severity=sev))

    for raw in (data.get("fixes") or [])[:2]:
        local = str(raw.get("id", f"f{len(out)}"))
        gid = f"{persona}:{mod}:{local}"
        text = str(raw.get("text", "")).strip()
        find = raw.get("find")
        replace = raw.get("replace")
        targets = idmap.get(str(raw.get("targets")))
        change = None
        if isinstance(find, str) and isinstance(replace, str) and find:
            change = CodeChange(path=node_id, find=find, replace=replace)
        if text:
            out.append(Proposal(gid, persona, "fix", node_id, text,
                                targets=targets, change=change))
    return out


class OpenAIAgent:
    """Implements the ``Agent`` protocol via the OpenAI API.

    Reads up to ``max_modules`` source files (or the explicit ``focus_modules``),
    once, on iteration 1. Pass your own ``client`` for testing; otherwise it uses
    ``OpenAI()`` which reads ``OPENAI_API_KEY`` from the environment.
    """

    def __init__(self, repo_root: str, model: str = "gpt-4o-mini",
                 focus_modules: list[str] | None = None, max_modules: int = 3,
                 max_source_chars: int = 6000, client=None, temperature: float = 0.2):
        self.repo_root = repo_root
        self.model = model
        self.focus_modules = focus_modules
        self.max_modules = max_modules
        self.max_source_chars = max_source_chars
        self._client = client
        self.temperature = temperature

    def _client_lazy(self):
        if self._client is None:
            from openai import OpenAI  # imported lazily so the package loads without openai
            self._client = OpenAI()
        return self._client

    def _ask(self, persona: str, goal: str, node_id: str, source: str) -> list[Proposal]:
        focus = _PERSONA_FOCUS.get(persona, "general engineering quality.")
        system = _SYSTEM.format(persona=persona, focus=focus)
        user = f"Goal: {goal}\nFile: {node_id}\n\n```python\n{source}\n```"
        try:
            resp = self._client_lazy().chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
            )
            data = json.loads(resp.choices[0].message.content)
        except Exception as e:  # never crash the loop on a bad call/parse
            print(f"  [warn] {persona} on {node_id}: {e}")
            return []
        return parse_response(persona, node_id, source, data)

    def propose(self, persona, goal, nodes, prior_committed, iteration):
        if iteration != 1:
            return []
        ids = [n.id for n in nodes]
        targets = self.focus_modules or ids[: self.max_modules]
        proposals: list[Proposal] = []
        for node_id in targets:
            if node_id not in ids:
                continue
            path = os.path.join(self.repo_root, node_id)
            try:
                with open(path, encoding="utf-8") as f:
                    source = f.read()[: self.max_source_chars]
            except OSError:
                continue
            proposals += self._ask(persona, goal, node_id, source)
        return proposals
