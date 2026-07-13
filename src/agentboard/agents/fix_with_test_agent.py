"""FixWithTestAgent — proposes a change AND the test that proves it.

The plain OpenAIAgent emits a fix (find/replace) but no test, so it can't feed
the TransitionVerifier, which needs a NEW test to check red-on-baseline /
green-after. This agent asks the model for both, in one shot, and packs them
into a single Proposal (``change`` + ``test_change``).

Same discipline as openai_agent: the network call and the parsing are split, so
``parse_fix_with_test`` is a pure, offline-testable function.

It does NOT try to guarantee the test is good — that's not its job and it
couldn't if it tried. The TransitionVerifier is the judge: if the model's test
doesn't actually fail-before-and-pass-after, the proposal is rejected. The agent
proposes; the world disposes.
"""
from __future__ import annotations

import json
import os
import re

from ..state import CodeChange, Node, Proposal

_SYSTEM = (
    "You are a senior engineer. You are given one source file and a goal. Propose "
    "ONE small, surgical change that advances the goal, AND a NEW test that proves "
    "it.\n\n"
    "CRITICAL constraint on the test — it will be checked mechanically:\n"
    "  * The test MUST FAIL on the current (unchanged) code.\n"
    "  * The test MUST PASS once your change is applied.\n"
    "A test that passes on the current code will be rejected as worthless.\n\n"
    "Output ONLY JSON with this shape:\n"
    '{{"issue": {{"text": "<one sentence>", "severity": "low|medium|high"}},\n'
    '  "fix": {{"text": "<one sentence>", "find": "<exact substring from the file, or null>", '
    '"replace": "<replacement, or null>", "append": "<code to append, or null>"}},\n'
    '  "test": {{"path": "<repo-root-relative path to a test file>", '
    '"append": "<a complete vitest test, including any imports it needs>"}}}}\n'
    "Rules: use EITHER find+replace (find must be copied verbatim and occur once) "
    "OR append, not both. The test `path` may be a new file. Keep the change minimal."
)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:24]


def parse_fix_with_test(persona: str, node_id: str, data: dict) -> list[Proposal]:
    """Turn the model's JSON into one Proposal carrying change + test_change.
    Pure and defensive: anything malformed yields no proposal rather than a crash."""
    mod = _slug(node_id.split("/")[-1])
    issue = data.get("issue") or {}
    fix = data.get("fix") or {}
    test = data.get("test") or {}

    text = str(fix.get("text") or issue.get("text") or "").strip()
    if not text:
        return []
    sev = issue.get("severity", "medium")
    if sev not in ("low", "medium", "high"):
        sev = "medium"

    # implementation change: find/replace OR append
    find, replace, append = fix.get("find"), fix.get("replace"), fix.get("append")
    change: CodeChange | None = None
    if isinstance(find, str) and find and isinstance(replace, str):
        change = CodeChange(path=node_id, find=find, replace=replace)
    elif isinstance(append, str) and append.strip():
        change = CodeChange(path=node_id, append=append)
    if change is None:
        return []

    # the test that proves it
    tpath, tappend = test.get("path"), test.get("append")
    test_change: CodeChange | None = None
    if isinstance(tpath, str) and tpath and isinstance(tappend, str) and tappend.strip():
        test_change = CodeChange(path=tpath, append=tappend)
    if test_change is None:
        return []  # no test -> nothing for the TransitionVerifier to check; skip

    gid = f"{persona}:{mod}:fix"
    return [Proposal(gid, persona, "fix", node_id, text, severity=sev,
                     change=change, test_change=test_change)]


class FixWithTestAgent:
    """Implements the ``Agent`` protocol; proposes change + proving test."""

    def __init__(self, repo_root: str, model: str = "gpt-4o",
                 focus_modules: list[str] | None = None, max_source_chars: int = 9000,
                 client=None, temperature: float = 0.2):
        self.repo_root = repo_root
        self.model = model
        self.focus_modules = focus_modules
        self.max_source_chars = max_source_chars
        self._client = client
        self.temperature = temperature

    def _client_lazy(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI()
        return self._client

    def _ask(self, persona: str, goal: str, node_id: str, source: str) -> list[Proposal]:
        user = f"Goal: {goal}\nFile: {node_id}\n\n```\n{source}\n```"
        try:
            resp = self._client_lazy().chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": _SYSTEM},
                          {"role": "user", "content": user}],
            )
            data = json.loads(resp.choices[0].message.content or "{}")
        except Exception as e:  # never crash the loop on a bad call/parse
            print(f"  [warn] {persona} on {node_id}: {e}")
            return []
        return parse_fix_with_test(persona, node_id, data)

    def propose(self, persona, goal, nodes, prior_committed, iteration):
        if iteration != 1:
            return []
        ids = [n.id for n in nodes]
        targets = self.focus_modules or ids
        proposals: list[Proposal] = []
        for node_id in targets:
            if node_id not in ids:
                continue
            try:
                with open(os.path.join(self.repo_root, node_id), encoding="utf-8") as f:
                    source = f.read()[: self.max_source_chars]
            except OSError:
                continue
            proposals += self._ask(persona, goal, node_id, source)
        return proposals
