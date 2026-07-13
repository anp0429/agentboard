"""GapAuditor — the precision layer. ADVISORY, never a verdict.

Tonight's proven problem: every `confirmed_gap` the pipeline produced was a FALSE
POSITIVE — the agent asserted its own ASSUMPTION, and the tool's real contract
(often in code the PR didn't touch) disagreed. Only a human reading source caught
them.

This does that source-reading step and hands the human the evidence:
for each confirmed_gap, a DIFFERENT model reads the source file + the agent's test
+ the observed assertion failure, and judges: does the code's ACTUAL behavior match
what the test asserted?

CRITICAL THESIS GUARD:
  - This NEVER changes the gate's verdict. The gate already ran the code; that
    result stands. The auditor only ANNOTATES: likely_real / likely_false_positive
    / uncertain, with a one-line reason and the source lines it relied on.
  - It is triage, not truth. It tells the human WHICH gaps to check first and WHERE
    to look. The human still decides. An LLM does not get to rule a real failing
    test "not a bug" and suppress it.
  - Because it's a judgment (not a deterministic check), it must show its evidence
    so the human can overrule it in seconds.
"""
from __future__ import annotations

import json
import os

from ..review import ReviewFinding

Assessment = str  # "likely_real" | "likely_false_positive" | "uncertain"

_SYSTEM = """You are auditing a code review finding for FALSE POSITIVES. Another agent claimed a tool has a bug because a test it wrote failed. Your job is NOT to decide if the tool is good — it is to decide whether the failing test actually reflects a real defect, or whether the agent simply ASSERTED something the code was never contracted to do.

You are given: the source file, the test the agent wrote, and the assertion that failed.

Reason strictly from the SOURCE:
- Find the code that produces the value under test. Quote the specific lines.
- Ask: is the agent's assertion the code's ACTUAL contract, or the agent's assumption about how it *should* behave?
- A test can fail for three reasons: (1) the tool has a real defect, (2) the agent asserted a behavior the tool never promised (design disagreement / wrong assumption), or (3) the agent's test setup didn't create what it assumed (so the tool correctly returned empty/less).
- Cases (2) and (3) are FALSE POSITIVES. Only (1) is a real gap.

Be skeptical of the agent. If the source shows the tool behaving consistently by its own clear rule, and the agent simply expected a different rule, that is likely_false_positive — even though the test really failed.

COMMIT to a call. "uncertain" is ONLY for when you genuinely cannot find the relevant code or the source is truly ambiguous — it is NOT a safe default. If you can trace the code path that produces the value under test, you MUST decide likely_real or likely_false_positive:
- If the code plainly produces a WRONG result for a valid input the intent covers (loses, duplicates, or fabricates information), say likely_real.
- If the code behaves consistently by its own clear rule and the agent merely assumed a different rule, or the test's setup did not create what it assumed, say likely_false_positive.
Do not hide correct analysis behind "uncertain." If your reasoning has identified the mechanism, state the verdict that reasoning implies.

You MUST always fill in "reason" and "evidence" with specifics from the source (name the behavior and the lines/logic). An empty reason is not acceptable.

Output ONLY JSON:
{"assessment": "likely_real" | "likely_false_positive" | "uncertain",
 "reason": "<one sentence, plain — required, never empty>",
 "evidence": "<the specific source behavior/lines you relied on — required, never empty>"}"""


def _loads_one(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                pass
    return {}


class GapAuditor:
    def __init__(self, model: str = "gpt-5.5", client=None, max_source_chars: int = 16000):
        self.model = model
        self._client = client
        self.max_source_chars = max_source_chars
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

    def _ask(self, source: str, finding: ReviewFinding) -> dict:
        user = (
            f"SOURCE FILE:\n```\n{source[:self.max_source_chars]}\n```\n\n"
            f"THE AGENT'S CLAIMED GAP: {finding.behavior}\n\n"
            f"THE TEST IT WROTE:\n```\n{finding.test_code or '(none)'}\n```\n\n"
            f"THE ASSERTION THAT FAILED:\n{finding.observed or '(none)'}\n\n"
            f"Audit this for a false positive. Respond ONLY with the JSON."
        )
        client = self._client_lazy()
        if self._is_openai:
            resp = client.chat.completions.create(
                model=self.model,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": _SYSTEM},
                          {"role": "user", "content": user}],
            )
            return _loads_one(resp.choices[0].message.content or "{}")
        resp = client.messages.create(
            model=self.model, max_tokens=2500,
            system=_SYSTEM + "\n\nRespond with ONLY the JSON object, nothing before it.",
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return _loads_one(text)

    def audit(self, source: str, finding: ReviewFinding) -> ReviewFinding:
        """Annotate a confirmed_gap with an advisory assessment. Verdict UNCHANGED."""
        if finding.status != "confirmed_gap":
            return finding
        try:
            data = self._ask(source, finding)
        except KeyError as e:  # missing API key — make it LOUD, not a silent skip
            finding.audit = "not_audited"
            finding.audit_reason = f"auditor could not run: missing {e} — gap is UNVERIFIED"
            print(f"  [AUDITOR DID NOT RUN] missing {e}; gaps are unverified")
            return finding
        except Exception as e:
            finding.audit = "not_audited"
            finding.audit_reason = f"auditor error: {e} — gap is UNVERIFIED"
            print(f"  [warn] auditor: {e}")
            return finding
        assessment = data.get("assessment", "uncertain")
        if assessment not in ("likely_real", "likely_false_positive", "uncertain"):
            assessment = "uncertain"
        finding.audit = assessment
        finding.audit_reason = str(data.get("reason", "")).strip()[:200]
        finding.audit_evidence = str(data.get("evidence", "")).strip()[:300]
        return finding

    def audit_all(self, source: str, findings: list[ReviewFinding]) -> list[ReviewFinding]:
        for f in findings:
            self.audit(source, f)
        return findings