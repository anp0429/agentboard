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

from ..providers import chat_completion

import ast
import json
import os

from ..review import ReviewFinding

# Review axes bias WHICH cases the reviewer enumerates, injected as data so the
# base prompt stays generic (same no-shape-hints, no-failure-bias rules apply).
# An axis never names a specific payload — only categories of input — so a
# finding is still the model's judgment, not the prompt read back. "default"
# is the empty string: byte-identical to pre-axis behavior, so old caches and
# fingerprints are unchanged.
AXES: dict[str, str] = {
    "default": "",
    "security": (
        "REVIEW EMPHASIS FOR THIS RUN: adversarial and untrusted input. When you "
        "enumerate the domain (step 2), weight it toward inputs an attacker or a "
        "hostile environment could supply: values that collide with reserved or "
        "inherited names in the target language, inputs that try to cross a "
        "boundary they should not (traversal, injection into a nested "
        "structure), malformed or truncated data, oversized or deeply nested "
        "input, and mixed or unexpected encodings. Still assert only what the "
        "intent IMPLIES the correct handling is; do not assert a vulnerability "
        "or aim at a failure. Reality answers."
    ),
}


def resolve_axis(name: str) -> str:
    """Axis directive text for a name; '' for default/unknown (never raises)."""
    return AXES.get((name or "default").strip().lower(), "")

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
- GROUND TRUTH FOR SETUP AND RESULT SHAPE: the existing tests file — and especially any test the reviewed diff itself adds or modifies — is authoritative, because it demonstrates the CURRENT working harness by construction. Copy its setup sequence, its data seeding, and EXACTLY how it unwraps the result it asserts on (which properties it reads off the response, at what nesting). Do not invent a response shape from the source code alone; read it off an existing test's assertions. Any repo notes below are background knowledge — when they conflict with what the existing tests or the diff's own tests actually do, THE TESTS WIN.
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


def import_surface(repo_root: str, target_rel: str) -> str:
    """Deterministic prompt DATA for Python targets: the module path a test
    must import and the public names that actually exist. Exists because
    the reviewer, given only source text, invented `_targets_from_diff`
    in agentboard.cli when the real names were public in agentboard.config
    — 33 proposals died of hallucinated imports in one self-review run.
    Facts from the ast, not judgment; the prompt stays repo-agnostic."""
    if not target_rel.endswith(".py"):
        return ""
    try:
        with open(os.path.join(repo_root, target_rel),
                  encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        return ""
    # Name collection: every public top-level BINDING is importable, and
    # the gate proved the narrow first version wrong five ways in one run
    # (fingerprint e4a011add5ff924c): annotated constants, compound and
    # tuple assignments, lowercase publics like `router`, __init__
    # re-exports, namespace-package paths.
    is_init = os.path.basename(target_rel) == "__init__.py"
    names: list[str] = []

    def _bind(name: str) -> None:
        if name and not name.startswith("_") and name not in names:
            names.append(name)

    def _target_names(t) -> list[str]:
        if isinstance(t, ast.Name):
            return [t.id]
        if isinstance(t, (ast.Tuple, ast.List)):
            out: list[str] = []
            for e in t.elts:
                out.extend(_target_names(e))
            return out
        if isinstance(t, ast.Starred):
            return _target_names(t.value)
        return []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            _bind(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for nm in _target_names(t):
                    _bind(nm)
        elif isinstance(node, ast.AnnAssign):
            # ENABLED: bool = True binds; a bare `x: int` annotation
            # does not exist at runtime and is excluded
            if node.value is not None and isinstance(node.target, ast.Name):
                _bind(node.target.id)
        elif is_init and isinstance(node, (ast.Import, ast.ImportFrom)):
            # a package __init__'s re-exports ARE its public surface;
            # regular modules' imports are dependencies, not API, and
            # stay out
            for a in node.names:
                if a.name == "*":
                    continue
                _bind(a.asname or a.name.split(".")[0])

    # Module path, best effort for real layouts: walk up through
    # identifier-named directories (namespace packages have no
    # __init__.py, PEP 420), then drop a topmost src/lib segment, which
    # is a source ROOT, not a package. Requiring __init__.py at every
    # level truncated `company.product.feature` to `product.feature`.
    stem = os.path.basename(target_rel)[: -len(".py")]
    parts = [] if stem == "__init__" else [stem]
    d = os.path.dirname(target_rel)
    while d:
        seg = os.path.basename(d)
        if not seg.isidentifier():
            break
        parts.append(seg)
        d = os.path.dirname(d)
    while parts and parts[-1] in ("src", "lib"):
        parts.pop()
    if not parts:
        return ""
    module = ".".join(reversed(parts))
    listed = ", ".join(names) if names else "(no public top-level names)"
    return (
        f"IMPORT SURFACE ({target_rel}) — importable as `{module}`. "
        f"Public top-level names: {listed}. In every proposed test, import "
        f"the target ONLY via this module path and ONLY these names; never "
        f"invent private helpers, other module paths, or fixtures that are "
        f"not defined inside your own test."
    )


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
        axis: str = "default",
        log=print,
    ):
        self.repo_root = repo_root
        self.target_path = target_path
        self.existing_tests_path = existing_tests_path
        self.model = model
        # optional provider pin from repo config; ambient env still wins
        # upstream (api.py resolves precedence before passing it here).
        self.base_url = ""
        self._client = client
        self.max_chars = max_chars
        # print-shaped narration sink; the caller picks where lines go (the
        # CLI passes print, the MCP server a per-call buffer). See api.py.
        self.log = log
        # Repo-specific test-writing rules (e.g. RepoProfile.harness_notes).
        # Injected into the prompt as data; the prompt stays repo-agnostic.
        self.harness_notes = harness_notes.strip()
        self.axis = (axis or "default").strip().lower()
        self._axis_directive = resolve_axis(self.axis)
        from ..providers import uses_anthropic
        self._is_openai = not uses_anthropic(model)

    def _read(self, rel: str) -> str:
        try:
            with open(os.path.join(self.repo_root, rel), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def _client_lazy(self):
        if self._client is None:
            from ..providers import client_for

            self._client = client_for(self.model, self.base_url)
        return self._client

    def review(self, intent: str, change: str = "") -> list[ReviewFinding]:
        source = self._read(self.target_path)[: self.max_chars]
        tests = self._read(self.existing_tests_path)[: self.max_chars]
        surface = import_surface(self.repo_root, self.target_path)
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
            + (f"\n\n{surface}" if surface else "")
            + (f"\n\n{self._axis_directive}" if self._axis_directive else "")
        )
        notes = (
            "- " + self.harness_notes if self.harness_notes else
            "(no repo-specific harness notes provided)"
        )
        try:
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
                    max_tokens=6000,
                    messages=[
                        {"role": "system", "content": _SYSTEM.format(intent=intent, harness_notes=notes)},
                        {"role": "user", "content": user},
                    ],
                )
                content = resp.choices[0].message.content or ""
                if not content.strip():
                    # An empty completion is a failure wearing success's
                    # clothes: a reasoning model can spend the whole token
                    # budget thinking and return nothing, which downstream
                    # reads as "0 behaviors" with no hint. Say it loudly.
                    reason = getattr(resp.choices[0], "finish_reason", "?")
                    self.log(f"  [warn] reviewer: empty completion "
                             f"(finish_reason={reason}) — the model likely "
                             f"spent its whole token budget reasoning")
                # lenient, not strict: local models behind the same client
                # sometimes fence their JSON despite response_format.
                data = _loads_lenient(content or "{}")
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
            self.log(f"  [warn] reviewer: {e}")
            return []
        return parse_review_plan(data)