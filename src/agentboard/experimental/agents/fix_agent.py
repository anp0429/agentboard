"""FixAgent — proposes a code fix for a confirmed_gap. It NEVER judges.

The judge is TransitionVerifier.verify_transition: the finding's own red test must
go green under the fix, with zero regressions. This agent only turns
(behavior, observed failure, source) into a candidate CodeChange.

Deterministic pre-checks before anything expensive runs:
  - `find` must be non-empty and occur EXACTLY ONCE in the target file
    (ambiguous or missing anchors are rejected here, not by a test run).
  - `replace` must differ from `find`.
These reject malformed proposals for free; they do not judge correctness.
"""
from __future__ import annotations

import json
import os

from ..state import CodeChange

_SYSTEM = """You are a senior engineer proposing a MINIMAL fix for a verified bug.

You are given:
- the intended behavior that a failing test proved is violated,
- the observed assertion failure,
- the failing test's code,
- the full content of one source file, with its repo-relative path.

Propose ONE minimal edit to THAT file that makes the intended behavior hold.

Rules:
- Smallest change that fixes the cause. No refactors, no style changes, no
  drive-by improvements.
- `find` must be an EXACT, UNIQUE substring of the file as given (copy it
  verbatim, whitespace included). `replace` is its replacement.
- Do not touch the test.

Respond ONLY with JSON:
{"find": "<exact snippet>", "replace": "<replacement>", "note": "<one line: what/why>"}
If you cannot propose a defensible fix in this file, respond {"find": null}."""


def check_change_applies(source: str, find: str | None, replace: str | None):
    """Deterministic applicability check. Returns (ok, reason)."""
    if not find:
        return False, "agent proposed no fix"
    if source.count(find) == 0:
        return False, "proposed `find` snippet not present in file"
    if source.count(find) > 1:
        return False, f"proposed `find` snippet ambiguous ({source.count(find)} occurrences)"
    if replace is None or replace == find:
        return False, "replace missing or identical to find"
    return True, ""


class FixAgent:
    def __init__(self, repo_root: str, model: str = "gpt-5.5"):
        self.repo_root = repo_root
        self.model = model
        self._client = None

    def _client_lazy(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI()
        return self._client

    def propose(self, behavior: str, observed: str, test_code: str,
                target_path: str) -> tuple[CodeChange | None, str]:
        """Returns (CodeChange, note) or (None, reason)."""
        src_file = os.path.join(self.repo_root, target_path)
        try:
            source = open(src_file, encoding="utf-8").read()
        except OSError as e:
            return None, f"cannot read target file: {e}"

        user = (
            f"INTENDED BEHAVIOR (a test proved this is violated):\n{behavior}\n\n"
            f"OBSERVED FAILURE:\n{observed}\n\n"
            f"FAILING TEST:\n{test_code}\n\n"
            f"FILE: {target_path}\n----- FILE CONTENT -----\n{source}"
        )
        resp = self._client_lazy().chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
        )
        try:
            data = json.loads(resp.choices[0].message.content or "{}")
        except (json.JSONDecodeError, IndexError):
            return None, "agent returned unparseable output"

        find, replace = data.get("find"), data.get("replace")
        ok, why = check_change_applies(source, find, replace)
        if not ok:
            return None, why
        note = (data.get("note") or "").strip()[:200]
        return CodeChange(path=target_path, find=find, replace=replace), note
