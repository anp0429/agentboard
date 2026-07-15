"""ReviewerAgent — a generic staff-engineer reviewer.

Design constraints, each earned the hard way this session:
  * GENERIC. The intent is INJECTED as data ({intent}), never baked into the
    persona. Same agent reviews any change against any intent.
  * NO SHAPE HINTS. The prompt never names composite keys, cross-schema refs,
    etc. If it listed them, a "finding" would just be the prompt read back. The
    edge-case judgment must come from the model, or we learn it can't.
  * NO FAILURE BIAS. It does NOT try to make tests fail. It asserts what the
    intent IMPLIES and lets reality answer. Aiming at red manufactures red.
  * COVERAGE-AWARE. It reads the existing tests and SKIPS what's already covered.
    Untested-but-intended behavior is where real defects hide (that is exactly
    where the composite-FK bug lived: the only test had no foreign keys).
  * DUAL-AXIS. Correctness (does output match the intent?) AND consistency (does
    the same input produce the same output across two runs?).

The agent only PROPOSES (behaviors + tests). The FindingVerifier runs them and
decides. No model certifies a gap.
"""

from __future__ import annotations

import json
import os

from ..review import ReviewFinding

_SYSTEM = """You are a staff engineer reviewing a code change against the intent it is meant to satisfy. You are not improving the code and not inventing features. You are checking whether it does what the intent says.

INTENT:
{intent}

You are given the source file under review and the existing tests for it.

Do this:
1. First, identify WHAT KIND OF SYSTEM this change operates on, from the intent and the code. For example: does it work with a database? an authentication or authorization layer? a serialization/parsing format? a network protocol? Name the domain(s) it touches.

2. Then review it SYSTEMATICALLY against that domain's standard concerns — go through the domain's full space of structures and rules exhaustively, not just the cases the intent happens to mention. An experienced engineer does not free-associate a few cases; they enumerate the domain thoroughly. Depending on the domain, be systematic about things like:
   - if it works with structured data: every kind of element and every kind of constraint or relationship that data model supports — including compound/multi-part ones and ones that cross boundaries, not only the simple single-part case.
   - if it touches security or access: every way access is granted, denied, escalated, or leaked.
   - for any domain: the boundary cases, the empty/absent case, and the compound case (two or more of something interacting).
   Enumerate the domain's structure space and, for EACH kind, ask: does this code handle THAT kind correctly? You must reason from these categories to concrete cases yourself. (Do not expect anyone to name the specific failing case for you — that is your job.)

3. Also reason along these review dimensions across the cases you enumerate:
   - correctness: does the output match what the intent implies for realistic inputs and covers all edge cases for given intent ?
   - data integrity: does the code preserve and represent the underlying structures faithfully — without dropping, duplicating, or inventing information? Check both completeness (nothing missing) and correctness (nothing fabricated).
   - consistency: does the same input produce the same output if run twice?
   - failure modes: are malformed, empty, or missing inputs handled the way the intent implies?

4. For each behavior, check the EXISTING TESTS. If a behavior is already genuinely tested, mark it covered and move on. Pay attention to what the existing tests do NOT exercise — untested-but-implied behavior is where problems hide.

5. For each behavior that is NOT already covered, write ONE test that asserts the correct behavior the intent implies, using the SAME test style/harness as the existing tests. Do not try to make it fail; assert what SHOULD happen and let it run.

Rules:
- Pick realistic inputs that are allowed by the intent. Do not invent behavior the intent does not ask for.
- Reuse the existing test harness exactly: same imports, same setup helpers, same style. Study how the existing tests prepare their environment and reproduce that setup faithfully before exercising the code under review. The test must compile and run.
{harness_notes}
- Assert the CORRECT expected result, not a failure.
- EXACTNESS FOR PRODUCED COLLECTIONS (mandatory). Whenever the tool returns a collection (an array or set of items) and the intent implies that collection is the complete, authoritative answer, assert it EXACTLY: assert the count AND deep-equal the full expected set — nothing missing and nothing extra. A consumer relies on this output as truth; an item that should not be there is false information the consumer will act on, a correctness failure, not a cosmetic one. Presence-only matchers (arrayContaining, toContain, toContainEqual, objectContaining, stringContaining) are FORBIDDEN when the collection is meant to be complete, because they pass even when the tool returns extra or duplicated items. Assert length (e.g. toHaveLength(N)) plus a full deep-equal; if output order is not guaranteed, sort both sides by a stable key first, but still assert the exact length — the length check is what catches over-production.

Output ONLY JSON:
{{"behaviors": [
  {{"behavior": "<one sentence>",
    "axis": "correctness" | "consistency",
    "covered_by_existing": true | false,
    "coverage_note": "<which existing test covers it, or 'none found'>",
    "test_path": "<repo-root-relative path to the test file, or null if covered>",
    "test_code": "<a complete test in the existing harness style, or null if covered>"
  }}
]}}"""


def _loads_lenient(text: str) -> dict:
    """Parse model JSON; if truncated, salvage the complete objects seen so far.

    Anthropic's messages API has no guaranteed-JSON mode, so a long response can
    be cut off mid-string. Rather than lose the whole review, recover every fully
    formed object we can find.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # salvage: try to parse the object closed at every '}', capturing nested ones
    objs, stack, instr, esc = [], [], False, False
    for i, ch in enumerate(text):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            try:
                o = json.loads(text[start : i + 1])
                if isinstance(o, dict) and "behavior" in o:
                    objs.append(o)
            except json.JSONDecodeError:
                pass
    return {"behaviors": objs}


def parse_review_plan(data: dict) -> list[ReviewFinding]:
    """Pure: model JSON -> ReviewFinding list. Defensive; drops malformed items."""
    findings: list[ReviewFinding] = []
    for b in data.get("behaviors", []) or []:
        behavior = str(b.get("behavior", "")).strip()
        if not behavior:
            continue
        axis = b.get("axis", "correctness")
        if axis not in ("correctness", "consistency"):
            axis = "correctness"
        covered = bool(b.get("covered_by_existing", False))
        tp, tc = b.get("test_path"), b.get("test_code")
        findings.append(
            ReviewFinding(
                behavior=behavior,
                axis=axis,
                covered_by_existing=covered,
                coverage_note=str(b.get("coverage_note", "")).strip()[:200],
                test_path=tp if (isinstance(tp, str) and tp and not covered) else None,
                test_code=tc
                if (isinstance(tc, str) and tc.strip() and not covered)
                else None,
            )
        )
    return findings


class ReviewerAgent:
    def __init__(
        self,
        repo_root: str,
        target_path: str,
        existing_tests_path: str,
        model: str = "gpt-5.5",
        client=None,
        max_chars: int = 12000,
        harness_notes: str = "",
    ):
        self.repo_root = repo_root
        self.target_path = target_path
        self.existing_tests_path = existing_tests_path
        self.model = model
        self._client = client
        self.max_chars = max_chars
        # Repo-specific test-writing rules (e.g. RepoProfile.harness_notes).
        # Injected into the prompt as data; the prompt stays repo-agnostic.
        self.harness_notes = harness_notes.strip()
        self._is_openai = model.startswith("gpt") or model.startswith("o")

    def _read(self, rel: str) -> str:
        try:
            with open(os.path.join(self.repo_root, rel), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def _client_lazy(self):
        if self._client is None:
            if self._is_openai:
                from openai import OpenAI

                self._client = OpenAI()
            else:
                from anthropic import Anthropic

                self._client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        return self._client

    def review(self, intent: str, change: str = "") -> list[ReviewFinding]:
        source = self._read(self.target_path)[: self.max_chars]
        tests = self._read(self.existing_tests_path)[: self.max_chars]
        change_block = (
            f"WHAT THIS PR CHANGED (review THIS against the intent, not the whole file):\n"
            f"```\n{change}\n```\n\n"
            if change.strip()
            else ""
        )
        user = (
            f"{change_block}"
            f"SOURCE FILE ({self.target_path}):\n```\n{source}\n```\n\n"
            f"EXISTING TESTS ({self.existing_tests_path}):\n```\n{tests}\n```"
        )
        notes = (
            "- " + self.harness_notes if self.harness_notes else
            "(no repo-specific harness notes provided)"
        )
        try:
            client = self._client_lazy()
            if self._is_openai:
                resp = client.chat.completions.create(
                    model=self.model,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": _SYSTEM.format(intent=intent, harness_notes=notes)},
                        {"role": "user", "content": user},
                    ],
                )
                data = json.loads(resp.choices[0].message.content or "{}")
            else:
                resp = client.messages.create(
                    model=self.model,
                    max_tokens=8000,
                    system=_SYSTEM.format(intent=intent, harness_notes=notes)
                    + "\n\nRespond with ONLY the JSON object, no prose before or after.",
                    messages=[{"role": "user", "content": user}],
                )
                text = "".join(
                    b.text for b in resp.content if getattr(b, "type", None) == "text"
                )
                data = _loads_lenient(text)
        except Exception as e:  # never crash the loop
            print(f"  [warn] reviewer: {e}")
            return []
        return parse_review_plan(data)