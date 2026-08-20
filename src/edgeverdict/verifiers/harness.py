# EDGEVERDICT_TESTFILE_RESOLVER_V2
# EDGEVERDICT_FREE_IMPORTS_V1
"""The framework seam of the gate: everything a test framework decides.
FindingVerifier owns the *gate semantics* — warm sandbox, batch-then-serial
fallback, the four-verdict contract, and the conservative "no minted gaps"
rule. What it must NOT own is any opinion about vitest, because the next
repo is a pytest repo and the verdict logic is framework-independent by
design. This module names the boundary:

    RepoProfile  = repo facts       (install/build/test commands, env, smoke)
    Harness      = framework facts  (how to inject a test, how to name it,
                                     how to filter a run to it, how to read
                                     the runner's output, what a genuine
                                     assertion failure looks like)

A Harness answers exactly the questions the verifier asks per finding:

    inject          proposal code into the pristine tests file (PURE)
    strip_imports   the injection-legality rule for proposal imports
    test_title      the name the runner will know the proposal by
    mark_title      stamp a gate-owned mark into that name (batch attribution)
    serial_command  run ONLY one titled test, results to a file
    batch_command   run ONLY mark-stamped tests, results to a file
    read_verdict    one serial run's output -> (status, observed)
    read_batch      one batched run's output -> neutral per-test records
    classify_failure one failure message -> assertion | timeout | load_error
    default_tests_for the framework's test-file naming conventions

The classification hook is the load-bearing one: a confirmed_gap may only
ever come from a named assertion failure, and each framework spells its
assertion layer differently. The vitest spelling lives here now, moved
byte-for-byte from finding_verifier so the e2e fingerprint tests stay the
oracle that nothing observable changed.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..review import Status


@dataclass
class BatchResult:
    """One executed test from a batched run, framework-neutral.

    The verifier's attribution logic (mark matching, verdict priority) works
    on these records only, so it cannot grow a framework dependency back.
    `status` values other than passed/failed mean "did not properly run" —
    the verifier leaves those findings for the serial path to decide."""

    title: str    # the name the runner reported (carries the gate's mark)
    status: str   # "passed" | "failed" | anything else = did not run
    failure: str  # first failure message, "" when none


class Harness(ABC):
    """Framework facts, named instead of assumed. See the module docstring
    for the contract each method fulfills."""

    name: str = ""
    # source-file extensions this framework's repos use; drives dispatch in
    # default-tests discovery (api._default_tests_for).
    src_suffixes: tuple[str, ...] = ()
    # runner artifact filename, written into the sandbox repo root
    result_file: str = ""

    @abstractmethod
    def inject(self, pristine: str, test_code: str,
               host_path: str | None = None,
               target_path: str | None = None) -> tuple[str | None, str]:
        """Proposal code into the pristine tests-file content (PURE).
        Returns (new_content, "") or (None, reason)."""

    @abstractmethod
    def strip_imports(self, test_code: str, pristine: str = "") -> str:
        """Apply the framework's rule for proposal-carried imports before
        injection. `pristine` is the host file, for rules that depend on
        what is already imported there."""

    @abstractmethod
    def test_title(self, test_code: str) -> str | None:
        """The name the runner will report the proposal's test under, or
        None when none can be read (-> broken_test / serial skip)."""

    @abstractmethod
    def mark_title(self, test_code: str, mark: str) -> str | None:
        """Stamp `mark` into the proposal's test name so a batched run can
        be attributed. Returns marked code, or None when no test opener was
        found (the finding then falls back to the serial path)."""

    @abstractmethod
    def serial_command(self, profile, tests_file: str, title: str,
                       out: str, is_parameterized: bool = False) -> list[str]:
        """The runner invocation for ONE titled test, machine-readable
        results written to `out`. is_parameterized lets a pytest-style
        harness switch to -k selection when one def fans into N node ids;
        harnesses without that concern (vitest) ignore it."""

    @abstractmethod
    def batch_command(self, profile, tests_file: str, mark_prefix: str,
                      out: str, scoped: bool = True) -> list[str]:
        """The runner invocation for every mark-stamped test at once,
        machine-readable results written to `out`. scoped=True limits the run
        to tests_file (fast); scoped=False is the whole-suite fallback."""

    @abstractmethod
    def read_verdict(self, out: str) -> tuple[Status, str]:
        """One serial run's output file -> (status, observed). The verdict
        priority (assertion > timeout > load error > never-ran) must match
        read_batch's records as consumed by the verifier: serial and batch
        may never diverge."""

    @abstractmethod
    def read_batch(self, out: str) -> list[BatchResult] | None:
        """One batched run's output file -> per-test records, or None when
        the output is missing/unreadable (-> nothing attributed, everyone
        falls back to serial)."""

    @abstractmethod
    def classify_failure(self, fm: str) -> tuple[str, str]:
        """One failure message -> (kind, first_line) where kind is
        "assertion" | "timeout" | "load_error". The single shared brain for
        serial and batched classification within this framework."""

    @staticmethod
    @abstractmethod
    def default_tests_for(repo: str, target: str,
                          dir_fallback: bool = True) -> str:
        """The framework's test-file naming conventions: find the tests file
        for a target, or return a best-guess path for a clear error."""


# ---------------------------------------------------------------------------
# vitest
# ---------------------------------------------------------------------------


class VitestHarness(Harness):
    # The positional test-file path in batch/serial commands must resolve from
    # the workdir vitest runs in (the package dir, e.g. apps/studio), not the
    # repo root — command path is project-relative (data/x.test.ts) while
    # injection still writes at the repo-relative path.
    project_relative_cmd_paths = True

    """The vitest spelling of the gate. Every rule in here was learned
    against a real repo (zod, jotai, zustand, supabase/mcp) — the comments
    carry the provenance, moved verbatim from finding_verifier."""

    name = "vitest"
    src_suffixes = (".ts", ".tsx", ".js", ".jsx", ".mjs")
    result_file = "edgeverdict-finding-result.json"

    def inject(self, pristine: str, test_code: str,
               host_path: str | None = None,
               target_path: str | None = None) -> tuple[str | None, str]:
        """Inject the agent's test into the pristine tests-file content (PURE).

        Works on the pristine content in memory (not the file on disk) so the warm
        base can inject a DIFFERENT test per finding, each starting from a clean file
        — never stacking one finding's test on top of another's.

        Placement is load-bearing, and it is decided by the LAST top-level opener
        (column-0 describe/test/it), not by whether a describe exists anywhere:

        * Last opener is describe -> the file ENDS in a describe block; insert
          before its final column-0 close so the proposal inherits
          describe-scoped helpers (learned against zod, whose tests file is one
          wrapping describe).
        * Last opener is test/it -> the file ends in top-level tests; the final
          `})` closes the LAST TEST, and inserting there nests the proposal
          inside another test's body, where `-t` skipping means it never
          registers at runtime ("name match failed") while a typecheck project
          still "passes" it statically. Append at end of file instead: module
          scope is always legal and module imports/helpers are in scope.

        The second case includes MIXED files — describes early, top-level its at
        the end (jotai's store.test.tsx, found on benchmark row 6, where the old
        "any describe anywhere" routing nested all 10 proposals inside the final
        it). EOF-append was proven against jotai's real environment before this
        rule was written.
        """
        if not test_code:
            return None, "no test supplied"
        code = self.strip_imports(test_code, pristine).rstrip()
        if not code:
            return None, "test contained only imports"
        if host_path:
            # catch 6b: bindings the proposal imported and the host lacks
            # are merged into the host's import header (verified against
            # the target's real export surface) instead of being lost to
            # strip_imports -> ReferenceError (ufo 9 / ohash 7 class).
            pristine = self._merge_imports(pristine, test_code, host_path)
            # names the proposal uses but never imported: bind them when the
            # repo itself vouches for the source (target exports / sibling
            # test imports). Otherwise leave them and fail honestly.
            pristine = self.resolve_free_imports(
                pristine, test_code, host_path, target_path)
        openers = re.findall(r"^(describe|test|it)\b", pristine, flags=re.MULTILINE)
        if openers and openers[-1] == "describe":
            tail = pristine.rstrip()
            # Column-0 close, with or without a semicolon: prettier semi:false
            # repos (zustand was the one that surfaced this) end the block with
            # `})` not `});`. Indented closes still never match, so the top-level
            # placement guarantee is unchanged.
            idx = tail.rfind("\n});")
            if idx == -1:
                idx = tail.rfind("\n})")
            if idx == -1:
                return None, "could not find describe-block close to inject before"
            return pristine[:idx] + "\n\n" + code + "\n" + pristine[idx:], ""
        return pristine.rstrip() + "\n\n" + code + "\n", ""


    def _import_header_end(self, pristine: str) -> int:
        """Index of the line AFTER the last top-level import in ``pristine``."""
        end, skipping = 0, False
        for i, line in enumerate(pristine.splitlines()):
            s = line.strip()
            if skipping:
                if s.endswith(";") or s.endswith('"') or s.endswith("'"):
                    skipping = False
                    end = i + 1
                continue
            if s.startswith("import ") or s.startswith("import{"):
                if s.endswith(";") or (" from " in s and s[-1] in "\"';"):
                    end = i + 1
                else:
                    skipping = " from " not in s
                    if not skipping:
                        end = i + 1
        return end

    def _merge_imports(self, pristine: str, test_code: str,
                       host_path: str) -> str:
        """Catch 6b. Deterministic, no model: collect the bindings the
        proposal tried to import, keep only names the resolved module's
        real export surface vouches for and the host does not already
        bind, and insert one import line per specifier after the host's
        last import. Relative specifiers resolve from the host file's
        directory and prefer the host's own specifier for the same file;
        bare specifiers merge only when the host already imports that
        module verbatim (its presence is proven, its surface is not
        cheaply checkable). Anything unverifiable is dropped — the old
        ReferenceError is strictly better than a transform error that
        poisons the whole file."""
        from ..ts_surface import (
            _ts_exports,
            _ts_resolve,
            bound_import_names,
            parse_es_imports,
        )
        host_dir = os.path.dirname(host_path)
        host_imps = parse_es_imports(pristine)
        host_names = bound_import_names(pristine)
        host_specs = {i["spec"] for i in host_imps}
        spec_for_file: dict[str, str] = {}
        for i in host_imps:
            if i["spec"].startswith("."):
                r = _ts_resolve(host_dir, i["spec"])
                if r:
                    spec_for_file.setdefault(r, i["spec"])
        lines: list[str] = []
        for imp in parse_es_imports(test_code):
            if imp["is_type"]:
                continue
            spec = imp["spec"]
            if spec.startswith("."):
                resolved = _ts_resolve(host_dir, spec)
                if not resolved:
                    continue
                use_spec = spec_for_file.get(resolved, spec)
                vals, _types = _ts_exports(resolved, set(), 0)
                allowed = set(vals)
                # defect 28: aliased imports merge as `exported as local`,
                # verified against the EXPORTED name (the local alias never
                # appears on the export surface by construction)
                named = []
                for exported, local in imp.get("pairs",
                                               [(n, n) for n in imp["names"]]):
                    if local in host_names or exported not in allowed:
                        continue
                    named.append(local if exported == local
                                 else f"{exported} as {local}")
                    host_names.add(local)
                if imp["default"] and imp["default"] not in host_names                         and "default" in allowed:
                    lines.append(f'import {imp["default"]} from "{use_spec}";')
                    host_names.add(imp["default"])
                if imp["namespace"] and imp["namespace"] not in host_names:
                    lines.append(
                        f'import * as {imp["namespace"]} from "{use_spec}";')
                    host_names.add(imp["namespace"])
            else:
                if spec not in host_specs:
                    continue
                use_spec = spec
                named = [n for n in imp["names"] if n not in host_names]
            if named:
                lines.append(
                    f'import {{ {", ".join(named)} }} from "{use_spec}";')
                host_names.update(n.split(" as ")[-1] for n in named)
        if not lines:
            return pristine
        split = pristine.splitlines()
        at = self._import_header_end(pristine)
        merged = "\n".join(split[:at] + lines + split[at:])
        # defect 29: splitlines/join eats a trailing newline; keep it
        if pristine.endswith("\n") and not merged.endswith("\n"):
            merged += "\n"
        return merged

    def strip_imports(self, test_code: str, pristine: str = "") -> str:
        """Remove module-level import statements from proposed test code.

        Proposals are injected INTO an existing tests file, never run standalone —
        and ES imports are only legal at module top level, so a proposal that
        carries its own imports fails the whole file's transform (three findings
        died this way against zod). The harness rule already tells the proposer
        to reuse the host file's imports; stripping enforces it mechanically.
        Since catch 6b, stripping is half the contract: inject() first
        merges needed-and-verified bindings into the host's import header,
        so a stripped import is only lost when the export surface could
        not vouch for it.
        If a stripped import was genuinely needed, the test fails at runtime with
        a clear ReferenceError — still a correct broken_test, instead of a
        transform failure that poisons the batch.
        """
        out, skipping = [], False
        for line in test_code.splitlines():
            stripped = line.strip()
            if skipping:
                if stripped.endswith(";") or stripped.endswith('"') or stripped.endswith("'"):
                    skipping = False
                continue
            if stripped.startswith("import ") or stripped.startswith("import{"):
                # multi-line import: skip until the closing `from "..."` line
                if not (stripped.endswith(";") or " from " in stripped and
                        (stripped.endswith('";') or stripped.endswith("';")
                         or stripped.endswith('"') or stripped.endswith("'"))):
                    skipping = " from " not in stripped
                continue
            out.append(line)
        return "\n".join(out)


    # ---- free-identifier resolution -------------------------------------
    # An LLM proposal routinely uses identifiers it never imports (`z`,
    # `injectableTool`), because it writes the test as if it were already
    # inside the module. If the chosen host file does not bind those names,
    # the injected test dies with ReferenceError before it can prove
    # anything. That is not a finding, it is a lost verdict.
    #
    # Resolution is deterministic and evidence-based. A name is imported ONLY
    # when the repo itself vouches for where it comes from:
    #   1. the TARGET file exports it  -> import from the target
    #   2. a sibling test in the same package imports it -> reuse that exact
    #      specifier, verbatim
    # Anything else is left unbound and fails honestly, exactly as today.
    _JS_GLOBALS = frozenset(["describe", "test", "it", "expect", "vi", "beforeEach", "afterEach", "beforeAll", "afterAll", "console", "JSON", "Object", "Array", "String", "Number", "Boolean", "Promise", "Math", "Date", "Map", "Set", "WeakMap", "WeakSet", "Symbol", "Error", "TypeError", "RangeError", "RegExp", "globalThis", "process", "Buffer", "URL", "URLSearchParams", "AbortController", "setTimeout", "clearTimeout", "setInterval", "clearInterval", "structuredClone", "require", "module", "exports", "undefined", "null", "true", "false", "NaN", "Infinity", "async", "await", "return", "const", "let", "var", "function", "class", "new", "typeof", "instanceof", "if", "else", "for", "while", "do", "switch", "case", "break", "continue", "throw", "try", "catch", "finally", "of", "in", "this", "void", "delete", "yield", "import", "export", "default", "from", "as"])

    _IDENT_RE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)")
    _DECL_RE = re.compile(
        r"(?:^|[\s;{(,])(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)")

    @staticmethod
    def _strip_js_literals(code: str) -> str:
        """Blank out strings/templates/comments so identifier scanning does not
        trip over words inside them."""
        out = []
        i, n = 0, len(code)
        while i < n:
            c = code[i]
            if c in "\"'`":
                q = c
                i += 1
                while i < n:
                    if code[i] == "\\":
                        i += 2
                        continue
                    if code[i] == q:
                        i += 1
                        break
                    i += 1
                out.append(" ")
                continue
            if c == "/" and i + 1 < n and code[i + 1] == "/":
                while i < n and code[i] != "\n":
                    i += 1
                continue
            if c == "/" and i + 1 < n and code[i + 1] == "*":
                i += 2
                while i + 1 < n and not (code[i] == "*" and code[i + 1] == "/"):
                    i += 1
                i += 2
                continue
            out.append(c)
            i += 1
        return "".join(out)

    @classmethod
    def _free_identifiers(cls, test_code: str, host: str) -> list[str]:
        """Names the proposal uses that the host file does not bind."""
        from ..ts_surface import bound_import_names
        code = cls._strip_js_literals(test_code)
        used = {m.group(1) for m in cls._IDENT_RE.finditer(code)}
        local = {m.group(1) for m in cls._DECL_RE.finditer(code)}
        # destructured locals: const { a, b } = ... / ({ a }) =>
        for m in re.finditer(r"(?:const|let|var)\s*\{([^}]*)\}", code):
            for part in m.group(1).split(","):
                nm = part.split(":")[-1].strip()
                if nm:
                    local.add(nm)
        for m in re.finditer(r"\(\s*\{([^}]*)\}\s*\)\s*=>", code):
            for part in m.group(1).split(","):
                nm = part.split(":")[-1].strip()
                if nm:
                    local.add(nm)
        # simple arrow/function params
        for m in re.finditer(r"\(([^)]*)\)\s*=>", code):
            for part in m.group(1).split(","):
                nm = part.strip().split(":")[0].strip()
                if nm.isidentifier():
                    local.add(nm)
        hostb = bound_import_names(host) | {
            m.group(1) for m in cls._DECL_RE.finditer(cls._strip_js_literals(host))}
        keys = {m.group(1) for m in re.finditer(r"([A-Za-z_$][\w$]*)\s*:", code)}
        free = [u for u in sorted(used)
                if u not in cls._JS_GLOBALS and u not in local
                and u not in hostb and u not in keys]
        return free

    @classmethod
    def resolve_free_imports(cls, merged: str, test_code: str,
                             host_path: str | None,
                             target_path: str | None) -> str:
        """Add imports for identifiers the repo can vouch for. Pure."""
        if not host_path:
            return merged
        free = cls._free_identifiers(test_code, merged)
        if not free:
            return merged
        from ..ts_surface import _ts_exports
        host_dir = os.path.dirname(host_path)
        additions: dict[str, list[str]] = {}

        # 1. the target's own export surface
        tgt_vals: set = set()
        if target_path and os.path.isfile(target_path):
            try:
                tgt_vals, _ = _ts_exports(target_path, set(), 0)
            except Exception:
                tgt_vals = set()
        if tgt_vals and target_path:
            rel = os.path.relpath(target_path, host_dir).replace(os.sep, "/")
            rel = re.sub(r"\.[cm]?tsx?$", ".js", rel)
            if not rel.startswith("."):
                rel = "./" + rel
            for name in list(free):
                if name in tgt_vals:
                    additions.setdefault(rel, []).append(name)
                    free.remove(name)

        # 2. an existing import ELSEWHERE in the repo that binds the name,
        # accepted only if the host's own package.json declares that
        # dependency (so the specifier is guaranteed resolvable from here).
        if free:
            import json as _json

            from ..ts_surface import parse_es_imports as _parse_imports

            # host package dir + its declared deps
            pkg = host_dir
            deps: set = set()
            for _ in range(6):
                pj = os.path.join(pkg, "package.json")
                if os.path.isfile(pj):
                    try:
                        with open(pj, encoding="utf-8") as fh:
                            d = _json.load(fh)
                        for key in ("dependencies", "devDependencies",
                                    "peerDependencies"):
                            deps |= set((d.get(key) or {}).keys())
                    except (OSError, ValueError):
                        pass
                    break
                nxt = os.path.dirname(pkg)
                if nxt == pkg:
                    break
                pkg = nxt
            # search root: nearest ancestor holding a workspace/lock marker
            root = pkg
            for _ in range(6):
                if any(os.path.exists(os.path.join(root, m)) for m in
                       ("pnpm-workspace.yaml", "pnpm-lock.yaml", ".git")):
                    break
                nxt = os.path.dirname(root)
                if nxt == root:
                    break
                root = nxt
            # pruned walk: globbing "**" would descend node_modules and cost
            # minutes on a real repo. Skip vendor/build dirs and cap the scan.
            _SKIP = {"node_modules", ".git", "dist", "build", "coverage",
                     ".next", ".turbo", "out", ".venv", "__pycache__"}
            sibs: list[str] = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if d not in _SKIP and not d.startswith(".")]
                for fn in filenames:
                    if (".test." in fn or ".spec." in fn or ".e2e." in fn) \
                            and fn.rsplit(".", 1)[-1] in ("ts", "tsx", "js",
                                                          "jsx", "mts", "cts"):
                        sibs.append(os.path.join(dirpath, fn))
                if len(sibs) > 400:
                    break
            seen: set = set()
            for s in sorted({s for s in sibs if "node_modules" not in s}):
                if not free:
                    break
                if s in seen:
                    continue
                seen.add(s)
                try:
                    with open(s, encoding="utf-8", errors="replace") as fh:
                        txt = fh.read(65536)
                except OSError:
                    continue
                for imp in _parse_imports(txt):
                    spec = imp.get("spec") or ""
                    if spec.startswith("."):
                        # A relative specifier is NOT re-based. It was, and it
                        # minted false gaps: `setup` in supabase/mcp is a local
                        # helper in server.test.ts AND an exported helper in
                        # test/e2e/utils.ts with a DIFFERENT signature. Binding
                        # by name resolved a proposal's setup({readOnly,
                        # features}) to the e2e one, which ignores both, so the
                        # test ran against a default server and failed on a
                        # count it was never given a chance to satisfy. Three
                        # "confirmed gaps" out of one wrong import.
                        # Verifying that a module EXPORTS the name does not
                        # verify it is the SAME thing; generic names (setup,
                        # render, createClient) collide across suites. An
                        # unbound name fails honestly as broken_test, which is
                        # the correct outcome. Bare specifiers (below) are safe
                        # because the package manifest vouches for them.
                        continue
                    base = spec.split("/")[0]
                    if base.startswith("@"):
                        base = "/".join(spec.split("/")[:2])
                    if deps and base not in deps:
                        continue  # host package cannot resolve it
                    for local_name, _orig in imp.get("pairs") or []:
                        if local_name in free:
                            additions.setdefault(spec, []).append(local_name)
                            free.remove(local_name)
        if not additions:
            return merged

        lines = []
        for spec, names in additions.items():
            uniq = sorted(set(names))
            lines.append("import { " + ", ".join(uniq) + " } from \"" + spec + "\";")
        split = merged.split("\n")
        at = 0
        for idx, ln in enumerate(split):
            if ln.startswith("import ") or ln.startswith("} from"):
                at = idx + 1
        return "\n".join(split[:at] + lines + split[at:])

    # A test declaration is `test(` or `it(` — OR a custom wrapper whose
    # first arg is a string and second is a function, e.g. pg-meta's
    # `withTestDatabase('title', async ({db}) => {...})`. Repos with shared
    # test setup wrap the runner this way, and those are exactly the
    # integration-tested (DB-backed) repos, so the wrapper case is common
    # precisely where it matters. We try the precise test/it form first (so
    # well-formed tests never touch the general path), then fall back to the
    # structural "IDENT('string', <function>)" form. First match wins, and in
    # a real proposal the OUTERMOST wrapper precedes any inner string+callback
    # call (e.g. executeQuery('sql', () => ...)), so the outer title is taken.
    _TITLE_TESTIT = re.compile(
        r"""(?:test|it)\(\s*(['"`])((?:\\.|(?!\1).)*)\1""")
    _TITLE_WRAPPER = re.compile(
        r"""[A-Za-z_$][\w$]*\(\s*(['"`])((?:\\.|(?!\1).)*)\1\s*,\s*"""
        r"""(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"""
        r"""|[A-Za-z_$][\w$]*\(\s*(['"`])((?:\\.|(?!\3).)*)\3\s*,\s*"""
        r"""(?:async\s+)?function\b""")

    def test_title(self, test_code: str) -> str | None:
        # Match the OPENING quote, then read until the SAME unescaped quote —
        # a title opened with ' must not be truncated at an apostrophe it
        # contains (`shouldn't`), which silently cut the title short and made
        # every -t / mark lookup miss ("name match failed", supabase/mcp#316
        # x8). Then unescape JS string escapes so the extracted title equals
        # the title vitest RENDERS and reports (what -t matches against).
        code = test_code or ""
        m = self._TITLE_TESTIT.search(code)
        if m:
            return VitestHarness._unescape_js(m.group(2))
        # custom test wrapper (string-then-function): the title is the first
        # string arg. group(2) is the arrow form, group(4) the function form.
        m = self._TITLE_WRAPPER.search(code)
        if m:
            return VitestHarness._unescape_js(m.group(2) or m.group(4))
        return None

    @staticmethod
    def _unescape_js(s: str) -> str:
        # minimal JS string unescape: the runner reports the rendered title,
        # so \' -> ' , \" -> " , \` -> ` , \\ -> \ . Unknown escapes keep
        # their char (\n stays literal backslash-n only if present; titles
        # rarely carry control escapes and vitest renders them verbatim).
        out = []
        i = 0
        while i < len(s):
            c = s[i]
            if c == "\\" and i + 1 < len(s):
                nxt = s[i + 1]
                if nxt in "\"'`\\":
                    out.append(nxt)
                    i += 2
                    continue
                out.append(c)
                i += 1
                continue
            out.append(c)
            i += 1
        return "".join(out)

    def mark_title(self, test_code: str, mark: str) -> str | None:
        # mark the title inside the test(...) opener itself — a naive
        # replace can hit a lookalike (comment, string) elsewhere and
        # leave the real test unmarked -> unattributed -> serial fallback.
        def _stamp(m: re.Match[str]) -> str:
            return m.group(1) + mark + " "

        code, n = re.subn(
            r"""((?:test|it)\(\s*[`'"])""",
            _stamp,
            test_code,
            count=1,
        )
        return code if n else None

    def serial_command(self, profile, tests_file: str, title: str,
                       out: str, is_parameterized: bool = False) -> list[str]:
        # Pass the test FILE positionally so vitest loads only THIS file, not
        # every test file in the project. Without it, `-t title` makes vitest
        # discover the whole suite (in supabase/studio: ~170 node workers,
        # ~475s) instead of one worker for one file (~1s). -t still narrows to
        # the injected test WITHIN the file, so pre-existing suite failures can
        # never be misattributed to this finding. (sig EDGEVERDICT_SCOPED_COMMAND_V1)
        return profile.test_base + [
            tests_file,
            "-t", title, "--typecheck.enabled=false",
            "--reporter=json", f"--outputFile={out}",
        ]

    def batch_command(self, profile, tests_file: str, mark_prefix: str,
                      out: str, scoped: bool = True) -> list[str]:
        # Pass the test FILE positionally so vitest loads and transforms only
        # THIS file, not every test file in the project (the ~170-worker,
        # ~475s scan). Scoping is what a human runs: `vitest run <file>`. -t
        # still narrows to the gate-marked tests WITHIN the file.
        # scoped=False drops the file: the fallback for repos whose vitest
        # config `include` would filter a positional path to nothing. The
        # verifier retries unscoped when the scoped run attributes nothing.
        # (sig EDGEVERDICT_SCOPED_COMMAND_V1)
        file_arg = [tests_file] if scoped else []
        return profile.test_base + [
            *file_arg,
            "-t", mark_prefix, "--typecheck.enabled=false",
            "--reporter=json", f"--outputFile={out}",
        ]

    _EXC_HEAD = re.compile(
        r"^([A-Za-z_$][\w$.]*(?:Error|Exception))"
        r"(?:\s*\[[^\]]+\])?"
        r"(?::|$)")

    def classify_failure(self, fm: str) -> tuple[str, str]:
        """One failure message -> (kind, first_line). This is the gate's
        shared brain for serial and batched paths — they must never diverge.

        Classification is by the exception actually RAISED, read from its
        raising position: in a JS report the thrown error heads the message
        and stack frames descend, so the first line matching a named error
        (SomeError:, AssertionError [ERR_ASSERTION]:) is the verdict. A
        substring match over the whole report was the pytest classifier's
        false-positive generator, and the same latent twin sat here — a
        crash merely QUOTING "AssertionError" (a codeframe line, a
        .toThrow(AssertionError) expectation) could mint a confirmed gap.
        Ordering, twice proven by the gate on itself: an identified
        AssertionError wins outright, because its human message may
        legitimately mention timeouts; timeout wording is consulted on the
        raising line, or over the report only when NO error was named
        (runner-generated reports have no traceback). vitest's assertion
        layer (@vitest/expect, chai, node:assert) names AssertionError in
        every genuine expect/assert failure."""
        first = fm.strip().splitlines()[0][:200] if fm.strip() else ""
        # vitest 3 serializes its per-test timeout through a stack-donor
        # placeholder: the reported failureMessage is the placeholder's
        # stack, headed "Error: STACK_TRACE_ERROR", and the human timeout
        # message does not survive into the JSON reporter. The placeholder
        # header IS the timeout signal; it must HEAD the message.
        if first == "Error: STACK_TRACE_ERROR":
            return "timeout", "test timed out (vitest 3 placeholder serialization)"
        exc = ""
        exc_line = ""
        for line in fm.splitlines():
            st = line.strip()
            m = self._EXC_HEAD.match(st)
            if m:
                exc, exc_line = m.group(1), st
                break
        if exc.endswith("AssertionError"):
            return "assertion", first
        if exc.endswith("TimeoutError"):
            return "timeout", first
        if exc and "timed out" in exc_line.lower():
            return "timeout", first
        if not exc and "timed out" in fm.lower():
            return "timeout", first
        return "load_error", first

    def read_verdict(self, out: str) -> tuple[Status, str]:
        if not os.path.isfile(out):
            return "broken_test", "test run produced no JSON output"
        try:
            data = json.loads(open(out, encoding="utf-8").read())
        except Exception as e:  # noqa: BLE001
            return "broken_test", f"could not parse results: {e}"
        # find the newly-added test's result; distinguish assertion-fail from load error
        failed_assertion = None
        load_error = None
        timeout_msg = None
        ran = 0
        for suite in data.get("testResults", []):
            msg = suite.get("message") or ""
            if suite.get("status") == "failed" and not suite.get("assertionResults"):
                load_error = msg  # suite failed to collect -> broken test
            for t in suite.get("assertionResults", []):
                if t.get("status") in ("passed", "failed"):
                    ran += 1
                if t.get("status") == "failed":
                    fm = (t.get("failureMessages") or [""])[0]
                    kind, first = self.classify_failure(fm)
                    if kind == "timeout":
                        timeout_msg = first
                    elif kind == "assertion":
                        failed_assertion = first
                    else:
                        load_error = first
        if failed_assertion:
            return "confirmed_gap", failed_assertion
        if timeout_msg:
            return "timed_out", timeout_msg
        if load_error:
            return "broken_test", load_error
        if ran == 0:
            return "broken_test", "injected test did not run (name match failed)"
        return "handled", "test passed — the tool already does this"

    def read_batch(self, out: str) -> list[BatchResult] | None:
        if not os.path.isfile(out):
            return None
        try:
            with open(out, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001
            return None
        results: list[BatchResult] = []
        for suite in data.get("testResults", []):
            for t in suite.get("assertionResults", []):
                results.append(BatchResult(
                    title=t.get("title") or t.get("fullName") or "",
                    status=t.get("status") or "",
                    failure=(t.get("failureMessages") or [""])[0],
                ))
        return results

    @staticmethod
    def default_tests_for(repo: str, target: str,
                          dir_fallback: bool = True) -> str:
        """Find the tests file for a target. Tries, in order: co-located
        (foo.test.ts), the same basename under any tests dir, and a
        singular/plural basename variant (errors.ts <-> error.test.ts, which is
        exactly the shape that tripped up the first real run on zod). Returns ""
        if nothing unambiguous is found — the caller then asks for --tests."""
        import glob as _glob

        for suffix in VitestHarness.src_suffixes:
            if not target.endswith(suffix):
                continue
            stem = target[: -len(suffix)]
            base = os.path.basename(stem)

            # 1. co-located: src/foo.ts -> src/foo.test.ts (or .spec —
            # pathe's whole suite is *.spec.ts; the old YAML picker knew
            # the suffix and the library port forgot it, gauntlet catch 3)
            for kind in (".test", ".spec"):
                cand = f"{stem}{kind}{suffix}"
                if os.path.isfile(os.path.join(repo, cand)):
                    return cand
            colocated = f"{stem}.test{suffix}"  # the best-GUESS for errors

            # 2 & 3. search test dirs for <base>.test.<ext>, then singular/plural
            variants = [base]
            if base.endswith("s"):
                variants.append(base[:-1])       # errors -> error
            else:
                variants.append(base + "s")      # error  -> errors
            target_dir = os.path.dirname(os.path.join(repo, target))
            for name in variants:
                hits: list[str] = []
                pats = []
                for kind in (".test", ".spec"):
                    pats += [
                        f"**/tests/**/{name}{kind}{suffix}",
                        f"**/__tests__/**/{name}{kind}{suffix}",
                        f"**/test/**/{name}{kind}{suffix}",
                        f"**/{name}{kind}{suffix}",
                    ]
                for pat in pats:
                    hits += _glob.glob(os.path.join(repo, pat), recursive=True)
                hits = sorted({h for h in hits if "node_modules" not in h})
                if len(hits) == 1:
                    return os.path.relpath(hits[0], repo)
                if len(hits) > 1:
                    # ambiguous — pick the file sharing the longest directory
                    # prefix with the target (closest in the monorepo tree)
                    def _shared(h: str) -> int:
                        return len(os.path.commonpath([target_dir, os.path.dirname(h)]))
                    best = max(hits, key=_shared)
                    # but a basename match in a FOREIGN directory injects the
                    # target's ./ imports against the wrong file (#316:
                    # transports/util.test.ts for tools/util.ts). If the pick
                    # is not in the target's own dir yet that dir holds exactly
                    # one test, the same-dir suite is the safer host.
                    if os.path.dirname(best) != target_dir:
                        _sib = []
                        for _sfx in VitestHarness.src_suffixes:
                            for _k in (".test", ".spec"):
                                _sib += _glob.glob(
                                    os.path.join(target_dir, f"*{_k}{_sfx}"))
                        _sib = sorted({s for s in _sib
                                       if "node_modules" not in s})
                        if len(_sib) == 1:
                            return os.path.relpath(_sib[0], repo)
                    # only accept if it's meaningfully close (shares more than repo root)
                    if _shared(best) > len(repo):
                        return os.path.relpath(best, repo)

            # 3.5 IMPORT MATCHING (the gauntlet's first stranger-repo lesson,
            # unjs/ufo run 1): basename conventions cannot survive human
            # naming — src/utils.ts is tested by test/utilities.test.ts.
            # The pytest lane already learned this and matches by import;
            # this is the TS spelling. PATH match first (a test importing
            # ".../<base>" names its target outright, unjs/defu style);
            # then NAME match (a test importing the target's exported
            # identifiers through a barrel, unjs/ufo style). Deterministic
            # regexes over real files — no model anywhere near resolution.
            candidates: list[str] = []
            cand_pats = []
            for kind in (".test", ".spec"):
                cand_pats += [f"**/tests/**/*{kind}{suffix}",
                              f"**/test/**/*{kind}{suffix}",
                              f"**/__tests__/**/*{kind}{suffix}",
                              f"**/*{kind}{suffix}"]
            for pat in cand_pats:
                candidates += _glob.glob(os.path.join(repo, pat),
                                         recursive=True)
            candidates = sorted({c for c in candidates
                                 if "node_modules" not in c})[:400]

            def _read(p: str) -> str:
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        return fh.read(65536)
                except OSError:
                    return ""

            # A path-import hit must RESOLVE to the target, not merely end in
            # the target's basename: transports/util.test.ts imports its own
            # ./util (a different file with the same basename) — counting that
            # sent every tools/util.ts run to the wrong host (#316). Extract
            # each import specifier ending in <base> and resolve it from the
            # candidate's own directory; keep the candidate only if it lands
            # on the target file.
            from ..ts_surface import _ts_resolve as _resolve_spec
            target_abs = os.path.normpath(os.path.join(repo, target))
            spec_re = re.compile(
                r"""(?:from\s+|require\(\s*)['"]([^'"]*/"""
                + re.escape(base)
                + r"""(?:\.[cm]?[jt]sx?)?)['"]""")
            path_hits = []
            for c in candidates:
                for spec in spec_re.findall(_read(c)):
                    if not spec.startswith("."):
                        continue
                    r_abs = _resolve_spec(os.path.dirname(c), spec)
                    if r_abs and os.path.normpath(r_abs) == target_abs:
                        path_hits.append(c)
                        break
            if len(path_hits) == 1:
                return os.path.relpath(path_hits[0], repo)
            if len(path_hits) > 1:
                def _shared_p(h: str) -> int:
                    return len(os.path.commonpath(
                        [target_dir, os.path.dirname(h)]))
                best = max(path_hits, key=_shared_p)
                if _shared_p(best) > len(repo):
                    return os.path.relpath(best, repo)

            exported = set(re.findall(
                r"^export\s+(?:async\s+)?(?:function|const|let|var|class|"
                r"enum)\s+([A-Za-z_$][\w$]*)",
                _read(os.path.join(repo, target)), re.MULTILINE))
            for grp in re.findall(r"^export\s*\{([^}]*)\}",
                                  _read(os.path.join(repo, target)), re.MULTILINE):
                for nm in grp.split(","):
                    nm = nm.strip().split(" as ")[-1].strip()
                    if nm:
                        exported.add(nm)
            if exported:
                scored: list[tuple[int, str]] = []
                for c in candidates:
                    imported: set[str] = set()
                    for grp in re.findall(r"import\s*\{([^}]*)\}",
                                          _read(c)):
                        for nm in grp.split(","):
                            nm = nm.strip().split(" as ")[0].strip()
                            if nm:
                                imported.add(nm)
                    overlap = len(exported & imported)
                    if overlap:
                        scored.append((overlap, c))
                if scored:
                    scored.sort(key=lambda t: (-t[0], t[1]))
                    top, runner_up = scored[0], (scored[1]
                                                 if len(scored) > 1 else None)
                    # accept only a CLEAR winner: at least two of the
                    # target's names, and strictly more than second place —
                    # a tie is ambiguity, and ambiguity means ask, not guess
                    if top[0] >= 2 and (runner_up is None
                                        or top[0] > runner_up[0]):
                        return os.path.relpath(top[1], repo)

            # 4. sole test file in the target's own directory. Covers the common
            # one-suite-per-module-directory layout that neither co-location nor
            # basename matching reaches (edgeverdict's own demo fixture: a
            # directory holding order_tool.js and demo.test.js). "Exactly one"
            # is the guard: with two or more there is nothing to infer, so we
            # fall through to asking for --tests rather than guessing.
            #
            # Only for an explicitly named --target (dir_fallback=True). Files
            # added automatically (--also, blast-radius scoping) must find a
            # real match or be skipped: inferring at scale is how twenty
            # unrelated files end up gated against one suite.
            siblings: list[str] = []
            if not dir_fallback:
                return colocated
            for sfx in VitestHarness.src_suffixes:
                for pat in (f"*.test{sfx}", f"*.spec{sfx}"):
                    siblings += _glob.glob(os.path.join(target_dir, pat))
            siblings = sorted({s for s in siblings if "node_modules" not in s})
            if len(siblings) == 1:
                return os.path.relpath(siblings[0], repo)

            return colocated  # fall back to the co-located name for a clear error
        return ""


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


def harness_for_profile(profile) -> Harness:
    """The harness a profile's framework calls for. RepoProfile.kind is
    "pytest" for python repos (set by config.build_profile) and "vitest"
    otherwise; the default keeps every existing profile — including ones
    built by hand in tests — on the vitest path unchanged."""
    if getattr(profile, "kind", "") == "pytest":
        from .pytest_harness import PytestHarness
        return PytestHarness()
    return VitestHarness()


def harness_for_target(target: str) -> Harness | None:
    """The harness whose source-file conventions cover `target`, or None
    when no framework claims the extension (the caller then asks for
    --tests explicitly, exactly as before). Imports are local so the
    harness registry cannot create an import cycle."""
    from .pytest_harness import PytestHarness
    for cls in (VitestHarness, PytestHarness):
        if target.endswith(cls.src_suffixes):
            return cls()
    return None
