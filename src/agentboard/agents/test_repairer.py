"""TestRepairer — one bounded repair round for broken proposals.

A broken_test is the PROPOSER'S failure, never evidence about the code —
but every broken proposal is also a lost attempt at breaking the change,
and self-review runs showed the loss at scale (33 proposals dead of
hallucinated imports in one run before the import surface existed).

This agent gets ONE round: it is shown the proposal, the exact error
that killed it, and the target's real import surface, and may fix ONLY
the test's setup — imports, names, fixtures it defines itself. It is
explicitly forbidden from changing what behavior the test asserts,
because a repair that weakens or retargets the assertion would let the
repairer quietly rewrite the reviewer's intent.

THESIS GUARD: the repairer never assigns a status. A repaired proposal
re-enters the same FindingVerifier as any other candidate and earns
handled / confirmed_gap / broken_test by execution. A proposal that
breaks twice stays broken. One round, hard cap, off-switch
(ReviewRequest.no_repair) — cost stays bounded and the narration says
exactly what happened.
"""
from __future__ import annotations

from ..providers import chat_completion
from ..review import ReviewFinding

MAX_REPAIRS = 6

_SYSTEM = """You repair test code that FAILED TO EXECUTE. Another agent \
proposed a test for a behavior; the test never reached a verdict because \
of a setup problem (bad import, undefined name, missing fixture). You fix \
ONLY the setup so the test can run.

HARD RULES:
- Do NOT change what behavior the test asserts. The assertion's meaning \
is the reviewer's intent and is not yours to edit. You may rename a \
variable or import path; you may not weaken, strengthen, retarget, or \
delete an assertion.
- Import the target ONLY via the module path and public names given in \
the IMPORT SURFACE. Never invent private helpers or other module paths.
- Any fixture or helper the test needs must be defined INSIDE the test \
code you return.
- Return ONLY the corrected test code. No prose, no markdown fences.
If the test cannot be repaired without changing its assertion, return \
exactly: CANNOT_REPAIR"""


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[: -3]
    return t.strip()


class TestRepairer:
    def __init__(self, model: str = "gpt-5.5", client=None, log=print):
        self.model = model
        self.base_url = ""
        self._client = client
        self.log = log
        from ..providers import uses_anthropic
        self._is_openai = not uses_anthropic(model)

    def _client_lazy(self):
        if self._client is None:
            from ..providers import client_for
            self._client = client_for(self.model, self.base_url)
        return self._client

    def repair(self, finding: ReviewFinding, surface: str) -> str | None:
        """Corrected test code for one broken proposal, or None if the
        model declines (CANNOT_REPAIR) or the call fails. Never mutates
        the finding — the caller decides what to do with the code."""
        user = (
            f"THE BEHAVIOR THE TEST IS FOR: {finding.behavior}\n\n"
            f"THE TEST THAT FAILED TO EXECUTE:\n{finding.test_code or ''}\n\n"
            f"THE EXACT ERROR:\n{finding.observed or '(none recorded)'}\n\n"
            + (f"{surface}\n\n" if surface else "")
            + "Return only the corrected test code, or CANNOT_REPAIR."
        )
        try:
            client = self._client_lazy()
            if self._is_openai:
                resp = chat_completion(
                    client,
                    model=self.model,
                    max_tokens=1200,
                    messages=[{"role": "system", "content": _SYSTEM},
                              {"role": "user", "content": user}],
                )
                text = resp.choices[0].message.content or ""
            else:
                resp = client.messages.create(
                    model=self.model,
                    max_tokens=1200,
                    system=_SYSTEM,
                    messages=[{"role": "user", "content": user}],
                )
                text = "".join(getattr(b, "text", "")
                               for b in resp.content)
        except Exception as exc:  # repair is best-effort, never fatal
            self.log(f"  [warn] repair call failed: {exc}")
            return None
        code = _strip_fences(text)
        if not code or code.strip() == "CANNOT_REPAIR":
            return None
        if "def test_" not in code:
            return None  # whatever came back, it is not a test
        return code
