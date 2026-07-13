"""CriticAgent — the second seat, a DIFFERENT model (Claude by default).

The first agent (ReviewerAgent, GPT) derives the behaviors the intent promises
and tests them — it produces a competent *feature checklist*. Empirically it
does NOT reach for adversarial schema shapes on its own.

This agent's job is different, and it MUST be a different model family, because a
same-model second seat is a clone with the same blind spots (proven). It receives
the first agent's coverage as a finished artifact and asks only: what does this
MISS? What inputs would break these features? It proposes tests for the gaps.

Why this is collaboration and not a vote:
  - The critic does not judge whether the first agent's tests are "correct" — the
    gate already runs those.
  - It ADDS candidate gap-tests the first agent didn't think of.
  - Every candidate it proposes still goes through the deterministic FindingVerifier.
    Two models agreeing "composite FKs break this" proves nothing until the test
    fails on the branch. Collaboration raises candidate quality; the gate decides
    truth.

Structure mirrors reviewer_agent: model call split from pure parsing.
"""
from __future__ import annotations

import json
import os

from ..review import ReviewFinding

_SYSTEM = """You are a staff engineer doing a second-pass review. Another engineer already reviewed a code change against this intent and wrote tests for the behaviors it promises:

INTENT:
{intent}

Their tests (the coverage so far) are given to you. Your job is NOT to re-check what they covered. Your job is to find what their coverage MISSES — the realistic inputs and data shapes that would break this tool but that their tests never exercise.

Think like someone trying to find the bug they didn't think of. Consider unusual but legitimate shapes of the data this tool processes. For each gap you find, write ONE test that asserts the CORRECT behavior for that shape.

Constraints:
- Only propose gaps that are within the intent's scope — real inputs the tool is supposed to handle, not invented features.
- Do NOT try to force a failure. Assert what SHOULD happen for that input and let it run.
- MANDATORY SETUP: reproduce the exact project setup the existing tests use (create org, create project, set active status, load schema via the project's db exec) before calling the tool. Never call the tool with a project_id you did not create this way. Copy the setup verbatim from the existing tests; change only the schema you load and the assertions.
- Reuse the existing test harness exactly (same imports, helpers, style). The test must compile.
- EXACTNESS FOR PRODUCED COLLECTIONS (mandatory). Whenever the tool returns a collection and the intent implies it is the complete, authoritative answer, assert it EXACTLY: assert the count AND deep-equal the full expected set — nothing missing and nothing extra. A consumer relies on this as truth; an item that should not be there is false information it will act on, a correctness failure. Presence-only matchers (arrayContaining, toContain, toContainEqual, objectContaining, stringContaining) are FORBIDDEN when the collection is meant to be complete — they pass even when the tool over-produces. Assert length (toHaveLength(N)) plus a full deep-equal; sort both sides by a stable key first if output order is not guaranteed.

Output ONLY JSON:
{{"gaps": [
  {{"behavior": "<the uncovered case, one sentence>",
    "axis": "correctness" | "consistency",
    "test_path": "<repo-root-relative path to the test file>",
    "test_code": "<a complete test in the existing harness style>"
  }}
]}}"""


def _loads_gaps_lenient(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    objs, stack, instr, esc = [], [], False, False
    for i, ch in enumerate(text):
        if instr:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == '"': instr = False
            continue
        if ch == '"': instr = True
        elif ch == "{": stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            try:
                o = json.loads(text[start:i + 1])
                if isinstance(o, dict) and "behavior" in o and "test_code" in o:
                    objs.append(o)
            except json.JSONDecodeError:
                pass
    return {"gaps": objs}


def parse_gaps(data: dict) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for g in data.get("gaps", []) or []:
        behavior = str(g.get("behavior", "")).strip()
        tc = g.get("test_code")
        if not behavior or not (isinstance(tc, str) and tc.strip()):
            continue
        axis = g.get("axis", "correctness")
        if axis not in ("correctness", "consistency"):
            axis = "correctness"
        findings.append(ReviewFinding(
            behavior=behavior,
            axis=axis,
            covered_by_existing=False,
            coverage_note="proposed by critic (2nd-pass, different model)",
            test_path=g.get("test_path"),
            test_code=tc,
        ))
    return findings


def _coverage_digest(prior: list[ReviewFinding]) -> str:
    lines = []
    for f in prior:
        lines.append(f"- ({f.axis}) {f.behavior}")
    return "\n".join(lines) if lines else "(no prior coverage)"


class CriticAgent:
    def __init__(self, model: str = "claude-opus-4-8", client=None, max_tokens: int = 3000):
        self.model = model
        self._client = client
        self.max_tokens = max_tokens
        self._is_openai = model.startswith("gpt") or model.startswith("o")

    def _client_lazy(self):
        if self._client is None:
            if self._is_openai:
                from openai import OpenAI
                self._client = OpenAI()
            else:
                from anthropic import Anthropic
                self._client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        return self._client

    def critique(self, intent: str, source: str, existing_tests: str,
                 prior: list[ReviewFinding]) -> list[ReviewFinding]:
        user = (
            f"SOURCE FILE:\n```\n{source[:9000]}\n```\n\n"
            f"EXISTING TESTS (harness to reuse):\n```\n{existing_tests[:9000]}\n```\n\n"
            f"COVERAGE SO FAR (the first reviewer's behaviors — find what these MISS):\n"
            f"{_coverage_digest(prior)}\n\nRespond ONLY with the JSON specified."
        )
        try:
            client = self._client_lazy()
            if self._is_openai:
                resp = client.chat.completions.create(
                    model=self.model,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": _SYSTEM.format(intent=intent)},
                        {"role": "user", "content": user},
                    ],
                )
                data = json.loads(resp.choices[0].message.content or "{}")
            else:
                resp = client.messages.create(
                    model=self.model,
                    max_tokens=8000,
                    system=_SYSTEM.format(intent=intent) + "\n\nRespond with ONLY the JSON object, no prose before or after.",
                    messages=[{"role": "user", "content": user}],
                )
                text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
                data = _loads_gaps_lenient(text)
        except Exception as ex:  # never crash the loop
            print(f"  [warn] critic: {ex}")
            return []
        return parse_gaps(data)