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

from ..providers import chat_completion

import json

from ..review import ReviewFinding

Assessment = str  # "likely_real" | "likely_false_positive" | "uncertain"

_SYSTEM = """You are auditing a code review finding for FALSE POSITIVES. Another agent claimed a tool has a bug because a test it wrote failed. Your job is NOT to decide if the tool is good — it is to decide whether the failing test actually reflects a real defect, or whether the agent simply ASSERTED something the code was never contracted to do.

You are given: the source file, the test the agent wrote, and the assertion that failed.

Reason strictly from the SOURCE:
- Find the code that produces the value under test. Quote the specific lines.
- Ask: is the agent's assertion the code's ACTUAL contract, or the agent's assumption about how it *should* behave?
- A test can fail for three reasons: (1) the tool has a real defect, (2) the agent asserted a behavior the tool never promised (design disagreement / wrong assumption), or (3) the agent's test setup didn't create what it assumed (so the tool correctly returned empty/less).
- Cases (2) and (3) are FALSE POSITIVES. Only (1) is a real gap.

Be skeptical of the agent, but be MORE skeptical of dismissing a real bug. Calling a genuine defect a false positive is the worst error you can make here: the failing test already EXECUTED against real code, so the burden of proof is on the dismissal, not on the bug. A test that ran and failed is presumed to reflect a real defect UNTIL you can quote the exact source line or rule that proves the agent asserted something the code never promised.

HARD BAR FOR likely_false_positive: you may only answer likely_false_positive if you can QUOTE the specific line(s) of source that establish the code's actual contract and show the agent's assertion contradicts that contract (a design disagreement) OR show the test's setup did not create what it assumed. Put that quoted line in "evidence". If you cannot quote such a line, you are NOT permitted to say likely_false_positive — answer likely_real or, only if the code path is genuinely untraceable, uncertain.

WATCH FOR REAL BUGS THAT LOOK BENIGN: if the observed failure shows actual state being lost, mutated, or fabricated (e.g. a prototype changed, a value dropped, a count off by one, "expected true to be false" on a pollution check), lean likely_real — those are the exact signatures of the defects this tool exists to catch, and they are easy to wave away as "the agent's assumption."

COMMIT to a call. "uncertain" is ONLY for when you genuinely cannot find the relevant code or the source is truly ambiguous — it is NOT a safe default. If you can trace the code path that produces the value under test, you MUST decide likely_real or likely_false_positive:
- If the code plainly produces a WRONG result for a valid input the intent covers (loses, duplicates, or fabricates information), say likely_real.
- If the code behaves consistently by its own clear rule AND you can quote that rule, and the agent merely assumed a different rule, say likely_false_positive.
Do not hide correct analysis behind "uncertain." If your reasoning has identified the mechanism, state the verdict that reasoning implies.

You MUST always fill in "reason" and "evidence" with specifics from the source (name the behavior and the lines/logic). An empty reason is not acceptable; a likely_false_positive with no quoted source line is not acceptable.

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
    def __init__(self, model: str = "gpt-5.5", client=None, max_source_chars: int = 16000,
                 log=print):
        self.model = model
        # optional provider pin from repo config; ambient env still wins
        # upstream (api.py resolves precedence before passing it here).
        self.base_url = ""
        self._client = client
        self.max_source_chars = max_source_chars
        # print-shaped narration sink; the caller picks where lines go (the
        # CLI passes print, the MCP server a per-call buffer). See api.py.
        self.log = log
        from ..providers import uses_anthropic
        self._is_openai = not uses_anthropic(model)

    def _client_lazy(self):
        if self._client is None:
            from ..providers import client_for
            self._client = client_for(self.model, self.base_url)
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
            resp = chat_completion(
                    client,
                model=self.model,
                response_format={"type": "json_object"},
                    # a JSON plan never needs the model's full output ceiling;
                    # an uncapped request lets a chatty provider run for
                    # minutes and makes metered routers reserve the whole
                    # ceiling against the account balance.
                    max_tokens=2500,
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
        """Annotate a confirmed_gap with an advisory assessment. Verdict UNCHANGED.

        Two guards against the auditor's own failure modes, both learned from a
        benchmark run:
          * empty reason -> retry once. Sonnet hedged 'uncertain' with a blank
            reason on ~6 gaps (concentrated on prototype-semantics questions); a
            single retry recovers most into a committed, reasoned call.
          * likely_false_positive with no quoted source line -> DOWNGRADE to
            uncertain. Dismissing a real, executed failure is the costliest
            error here (it once flagged a genuine strict catch as FP), so a
            dismissal that cannot cite the contract it relies on is not trusted.
        """
        if finding.status != "confirmed_gap":
            return finding
        try:
            data = self._ask(source, finding)
            reason = str(data.get("reason", "")).strip()
            if not reason:  # empty-reason hedge -> one retry
                data = self._ask(source, finding)
        except KeyError as e:  # missing API key — make it LOUD, not a silent skip
            finding.audit = "not_audited"
            finding.audit_reason = f"auditor could not run: missing {e} — gap is UNVERIFIED"
            self.log(f"  [AUDITOR DID NOT RUN] missing {e}; gaps are unverified")
            return finding
        except Exception as e:
            finding.audit = "not_audited"
            finding.audit_reason = f"auditor error: {e} — gap is UNVERIFIED"
            self.log(f"  [warn] auditor: {e}")
            return finding
        assessment = data.get("assessment", "uncertain")
        if assessment not in ("likely_real", "likely_false_positive", "uncertain"):
            assessment = "uncertain"
        reason = str(data.get("reason", "")).strip()[:200]
        evidence = str(data.get("evidence", "")).strip()[:300]
        # A dismissal must cite the contract it relies on. No evidence quote ->
        # the auditor has not earned the FP call; downgrade so a real bug is
        # never buried under an unsupported "false positive".
        if assessment == "likely_false_positive" and not evidence:
            assessment = "uncertain"
            reason = ("(downgraded from false-positive: no source line cited) "
                      + reason)[:200]
        finding.audit = assessment
        finding.audit_reason = reason
        finding.audit_evidence = evidence
        return finding

    def audit_all(self, source: str, findings: list[ReviewFinding]) -> list[ReviewFinding]:
        for f in findings:
            self.audit(source, f)
        return findings