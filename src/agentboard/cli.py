"""agentboard CLI.

`agentboard demo` is the zero-key proof: a bundled buggy target, four
pre-proposed tests, and the deterministic gate — no API key, no config, no
repo of yours at risk. The LLM's job (proposing) is pre-done; what you watch
is the part that makes agentboard trustworthy: the gate deciding.

    agentboard demo           # the planted bug is caught: 1 confirmed gap
    agentboard demo --fixed   # same gate, bug fixed: 0 gaps (red -> green)

Requires node + npm on PATH (the demo target is a real vitest project).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from .agents.critic_agent import CriticAgent
from .agents.reviewer_agent import ReviewerAgent
from .config import (
    build_profile,
    current_branch,
    fork_point,
    intent_from_commits,
    load_config,
    preflight,
)
from .demo import TARGET_DIR
from .fingerprint import verdict_summary
from .proposal_cache import propose_or_cached
from .review import ReviewFinding, ReviewRun, render_review_html
from .verifiers.finding_verifier import FindingVerifier
from .verifiers.vitest_verifier import RepoProfile

_BUG = "Math.min(n, hi - 1)"
_FIX = "Math.min(n, hi)"

_BADGE = {
    "handled": "\x1b[32mhandled      \x1b[0m",
    "confirmed_gap": "\x1b[31mCONFIRMED GAP\x1b[0m",
    "broken_test": "\x1b[33mbroken test  \x1b[0m",
    "timed_out": "\x1b[35mtimed out    \x1b[0m",
}


def _findings() -> list[ReviewFinding]:
    """Four pre-proposed tests — one per verdict the gate can issue. In real
    use an LLM proposes these from your issue/PR; the gate works the same."""
    return [
        ReviewFinding(
            behavior="in-range page sizes are honored",
            test_code=(
                "test('honors an in-range page size', () => {\n"
                "  expect(findOrders(ORDERS, 'open', 2).length).toBe(2);\n"
                "});"
            ),
        ),
        ReviewFinding(
            behavior="a request for exactly the maximum page size is honored",
            test_code=(
                "test('clamp keeps the inclusive upper bound', async () => {\n"
                "  const { clampPageSize } = await import('./order_tool.js');\n"
                "  expect(clampPageSize(50, 1, 50)).toBe(50);\n"
                "});"
            ),
        ),
        ReviewFinding(
            behavior="a defective proposal cannot manufacture a gap",
            test_code=(
                "test('references a name that does not exist', () => {\n"
                "  expect(totallyUndefinedHelper()).toBe(true);\n"
                "});"
            ),
        ),
        ReviewFinding(
            behavior="a hang is reported as ambiguity, not as anything else",
            test_code=(
                "test('never resolves', async () => {\n"
                "  await new Promise(() => {});\n"
                "}, 1000);"
            ),
        ),
    ]


def _demo_profile() -> RepoProfile:
    return RepoProfile(
        name="agentboard-demo",
        install_cmd=["npm", "install", "--no-audit", "--no-fund"],
        test_base=["npx", "vitest", "run"],
        build_cmd=None,
        env={"CI": "true"},
        smoke_cmd=["npx", "vitest", "run", "--passWithNoTests",
                   "-t", "___agentboard_env_probe___"],
    )


def demo(fixed: bool = False) -> int:
    if shutil.which("node") is None or shutil.which("npm") is None:
        print("agentboard demo needs node + npm on PATH "
              "(the demo target is a real vitest project).\n"
              "Install node, then re-run: https://nodejs.org")
        return 1

    work = tempfile.mkdtemp(prefix="agentboard_demo_")
    target = os.path.join(work, "target")
    shutil.copytree(TARGET_DIR, target)
    tool = os.path.join(target, "order_tool.js")
    if fixed:
        src = open(tool, encoding="utf-8").read()
        with open(tool, "w", encoding="utf-8") as fh:
            fh.write(src.replace(_BUG, _FIX, 1))

    print("agentboard demo — the deterministic gate, no API key required.")
    print(f"target: a tiny order tool ({'bug FIXED' if fixed else 'one planted bug'})")
    print("four pre-proposed tests -> one gate run -> four honest verdicts\n")

    t0 = time.time()
    print("[1/2] preparing sandbox (npm install, ~10s first run)...")
    run = ReviewRun(intent="demo", target="order_tool.js", findings=_findings())
    verifier = FindingVerifier(
        target, _demo_profile(), tests_file="demo.test.js", timeout=300
    )
    print("[2/2] running the gate (one batched vitest invocation)...\n")
    verifier.run(run)

    for f in run.findings:
        print(f"  {_BADGE.get(f.status, f.status):<14} {f.behavior}")
        if f.status != "handled" and f.observed:
            print(f"                -> {f.observed[:100]}")
    print(f"\n{verdict_summary(run)}")
    print(f"gate time: {time.time() - t0:.1f}s")

    board = os.path.abspath("./agentboard_demo_board.html")
    render_review_html(run, board)
    print(f"board:     {board}")

    if not fixed and run.gaps:
        print(
            "\nThe gap is real: `clampPageSize` treats the upper bound as "
            "exclusive,\nso a request for exactly the maximum comes back one "
            "short — the kind of\nbug an LLM judge reads right past. The gate "
            "ran the test; the test failed.\n"
            "\nNow watch it flip:   agentboard demo --fixed"
        )
    elif fixed and not run.gaps:
        print(
            "\nSame gate, same tests, one-line fix: the gap is gone. "
            "Red -> green,\ndecided by execution — no model in the verdict "
            "path, ever."
        )
    shutil.rmtree(work, ignore_errors=True)
    return 0


def _tests_from_diff(repo: str, base: str, head: str) -> list[str]:
    """Test files the reviewed change itself touches. When basename
    autodetect finds nothing, the change's own diff is the next-best
    deterministic signal: a fix that came with a test names its tests file
    for us, and that file is exactly where proposals belong."""
    r = subprocess.run(
        ["git", "-C", repo, "diff", "--name-only", f"{base}...{head}"],
        capture_output=True, text=True,
    )
    out: list[str] = []
    for line in r.stdout.splitlines():
        f = line.strip()
        if not f:
            continue
        b = os.path.basename(f)
        if (".test." in b or ".spec." in b) and os.path.isfile(os.path.join(repo, f)):
            out.append(f)
    return out


def _default_tests_for(repo: str, target: str) -> str:
    """Find the tests file for a target. Tries, in order: co-located
    (foo.test.ts), the same basename under any tests dir, and a
    singular/plural basename variant (errors.ts <-> error.test.ts, which is
    exactly the shape that tripped up the first real run on zod). Returns ""
    if nothing unambiguous is found — the caller then asks for --tests."""
    import glob as _glob

    for suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
        if not target.endswith(suffix):
            continue
        stem = target[: -len(suffix)]
        base = os.path.basename(stem)

        # 1. co-located: src/foo.ts -> src/foo.test.ts
        colocated = f"{stem}.test{suffix}"
        if os.path.isfile(os.path.join(repo, colocated)):
            return colocated

        # 2 & 3. search test dirs for <base>.test.<ext>, then singular/plural
        variants = [base]
        if base.endswith("s"):
            variants.append(base[:-1])       # errors -> error
        else:
            variants.append(base + "s")      # error  -> errors
        target_dir = os.path.dirname(os.path.join(repo, target))
        for name in variants:
            hits: list[str] = []
            for pat in (
                f"**/tests/**/{name}.test{suffix}",
                f"**/__tests__/**/{name}.test{suffix}",
                f"**/test/**/{name}.test{suffix}",
                f"**/{name}.test{suffix}",
            ):
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
                # only accept if it's meaningfully close (shares more than repo root)
                if _shared(best) > len(repo):
                    return os.path.relpath(best, repo)

        # 4. sole test file in the target's own directory. Covers the common
        # one-suite-per-module-directory layout that neither co-location nor
        # basename matching reaches (agentboard's own demo fixture: a
        # directory holding order_tool.js and demo.test.js). "Exactly one"
        # is the guard: with two or more there is nothing to infer, so we
        # fall through to asking for --tests rather than guessing.
        siblings: list[str] = []
        for sfx in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
            for pat in (f"*.test{sfx}", f"*.spec{sfx}"):
                siblings += _glob.glob(os.path.join(target_dir, pat))
        siblings = sorted({s for s in siblings if "node_modules" not in s})
        if len(siblings) == 1:
            return os.path.relpath(siblings[0], repo)

        return colocated  # fall back to the co-located name for a clear error
    return ""


def init(args) -> int:
    """Write a starter .agentboard.toml, pre-filled with what we can detect."""
    from .config import CONFIG_NAME, detect_profile_kind, detect_vitest_projects

    repo = os.path.abspath(os.path.expanduser(args.repo))
    dest = os.path.join(repo, CONFIG_NAME)
    if os.path.isfile(dest) and not args.force:
        print(CONFIG_NAME + " already exists. Use --force to overwrite.")
        return 1

    kind = detect_profile_kind(repo) or "pnpm-vitest"
    projects = detect_vitest_projects(repo)
    if len(projects) == 1:
        proj_line = 'project = "' + projects[0] + '"'
    elif projects:
        proj_line = '# project = "unit"   # multiple detected: ' + ", ".join(projects)
    else:
        proj_line = '# project = "unit"   # set if your repo uses vitest projects'

    lines = [
        "# agentboard config - committed once, shared by everyone reviewing this repo.",
        'profile = "' + kind + '"',
        proj_line,
        'base = "main"',
        'harness_notes = "Tests already import the framework and helpers - reuse them, do not add import statements."',
        "",
    ]
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("wrote " + CONFIG_NAME)
    print("  profile: " + kind)
    if len(projects) == 1:
        print("  project: " + projects[0] + " (auto-detected)")
    elif projects:
        print("  projects detected: " + ", ".join(projects) + " - pick one in the file")
    print("Review anytime with:  agentboard review --target <file> --intent <what>")
    return 0


def review(args) -> int:
    repo = os.path.abspath(os.path.expanduser(args.repo))
    cfg = load_config(repo)

    head = args.head or current_branch(repo)
    base = args.base or cfg.base or (fork_point(repo, head) or "main")
    target = args.target
    tests = args.tests or _default_tests_for(repo, target)

    # friendly, specific guidance for the most common first-run stumble:
    # we couldn't find the tests file and the user didn't say where it is.
    if not args.tests and not os.path.isfile(os.path.join(repo, tests)):
        diff_tests = _tests_from_diff(repo, base, head)
        if len(diff_tests) == 1:
            tests = diff_tests[0]
            print(f"tests: {tests} (the change's own diff names it)")
        else:
            print("agentboard review — couldn't find the tests file.")
            print(f"  Looked for one matching {target!r} but found nothing "
                  f"unambiguous.")
            if diff_tests:
                print("  The change itself touches these test files; pass one:")
                for t in diff_tests:
                    print(f"    --tests {t}")
            else:
                print("  Point me at it directly:  --tests path/to/your.test.ts")
            return 1

    need_critic = cfg.run_critic and not args.no_critic
    problems = preflight(
        repo_root=repo, head=head, base=base, target=target, tests=tests,
        reviewer_model=cfg.reviewer_model, need_critic=need_critic,
        critic_model=cfg.critic_model,
    )
    if problems:
        print("agentboard review — cannot start:")
        for p in problems:
            print(f"  - {p}")
        return 1

    # intent: --intent > --issue > commit messages on the branch
    from .ingestion.intent import resolve_intent
    if args.intent:
        intent = args.intent
    elif args.issue:
        intent = resolve_intent(issue_url=args.issue)
    else:
        intent = intent_from_commits(repo, base, head)
        if not intent:
            print("agentboard review — cannot start:")
            print("  - no --intent, no --issue, and no commit messages to derive "
                  "intent from. Say what the change is meant to do.")
            return 1
        print(f"intent: derived from commit message(s) on {head}")

    from .ingestion.pr_diff import diff_blob, load_pr_diff
    change = ""
    try:
        change = diff_blob(load_pr_diff(repo, head=head, base=base))
        print(f"change: {len(change)} chars ({head} vs {base})")
    except Exception as e:  # noqa: BLE001
        print(f"  - could not load the diff ({e}); aborting rather than "
              "silently reviewing the whole file")
        return 1

    profile = build_profile(repo, cfg, tests)
    critic = CriticAgent(model=cfg.critic_model) if need_critic else None

    # the set of (target, tests) pairs to review. Default: the one --target.
    # Multi-target (blast radius) appends more pairs; each is reviewed against
    # the same diff/intent and merged into ONE run so the board and fingerprint
    # cover the whole change.
    pairs = _resolve_targets(repo, target, tests, args)
    if getattr(args, "scope", ""):
        extra = _blast_pairs(repo, base, head, args, have={t for t, _ in pairs})
        if extra is None:
            return 1
        pairs.extend(extra)

    run = ReviewRun(intent=intent, target=target)
    for tgt, tst_path in pairs:
        if len(pairs) > 1:
            print(f"--- reviewing {tgt} ---")
        src = open(os.path.join(repo, tgt), encoding="utf-8").read()
        tst = open(os.path.join(repo, tst_path), encoding="utf-8").read()
        reviewer = ReviewerAgent(repo, tgt, tst_path, model=cfg.reviewer_model,
                                 harness_notes=profile.harness_notes)
        print(f"proposing for {tgt} (reviewer {cfg.reviewer_model}"
              + (f" + critic {cfg.critic_model}" if need_critic else "") + ")…")
        findings = propose_or_cached(
            reviewer, critic, intent=intent, change=change, source=src,
            tests=tst, fresh=args.fresh,
        )
        for f in findings:
            f.source_file = tgt
        print(f"  {len(findings)} behavior(s) to gate")
        sub = ReviewRun(intent=intent, target=tgt, findings=findings)
        FindingVerifier(repo, profile, tests_file=tst_path,
                        timeout=args.timeout).run(sub)
        run.findings.extend(sub.findings)
        # env_error lives on the per-file sub-run; merging only findings threw
        # it away, so the banner never rendered anywhere and "see banner"
        # pointed at nothing. Carry it up, tagged per file in multi-target runs.
        if sub.env_error:
            tag = f"{tgt}: " if len(pairs) > 1 else ""
            run.env_error = (
                (run.env_error + "\n" if run.env_error else "") + tag + sub.env_error
            )

    if run.env_error:
        print("\nENVIRONMENT FAILURE: the test environment could not be "
              "prepared, so nothing was executed.")
        print("Verdicts below are not real results. Fix this first:")
        for line in run.env_error.strip().splitlines():
            print(f"  {line}")
        print()

    for f in run.findings:
        tag = f"{f.source_file}: " if len(pairs) > 1 and f.source_file else ""
        print(f"  [{f.status:14}] {tag}{f.behavior[:60]}")
        if f.observed and f.status not in ("handled", "skipped_covered"):
            print(f"       -> {f.observed[:120]}")
    board = render_review_html(run, args.board)
    print(f"\n{verdict_summary(run)}")
    print(f"{len(run.gaps)} confirmed gap(s) across {len(pairs)} file(s). "
          f"Board: {board}")

    if getattr(args, "json_out", ""):
        _write_json_out(args.json_out, run, repo=repo, base=base, head=head,
                        pairs=pairs, board=board)
        print(f"json: {args.json_out}")
    return 0


def _write_json_out(path, run, *, repo, base, head, pairs, board) -> None:
    """Machine-readable run artifact (schema_version 1). This is the adapter
    boundary: CI/MCP surfaces consume this file; the engine knows nothing
    about PR comments. Evidence is the product, so each finding carries its
    test source and observed output, not just a status. Exit codes stay
    advisory (0 = ran, 1 = could not run); gaps live here, never in the
    exit code, per the never-blocking position."""
    counts: dict[str, int] = {}
    for f in run.findings:
        counts[f.status] = counts.get(f.status, 0) + 1
    doc = {
        "schema_version": 1,
        "repo": repo,
        "base": base,
        "head": head,
        "intent": run.intent,
        "targets": [{"target": t, "tests": ts} for t, ts in pairs],
        "env_error": run.env_error,
        "verdict_counts": counts,
        "confirmed_gaps": len(run.gaps),
        "summary": verdict_summary(run),
        "board": board,
        "findings": [
            {
                "behavior": f.behavior,
                "status": f.status,
                "observed": f.observed,
                "source_file": f.source_file,
                "test_code": f.test_code,
            }
            for f in run.findings
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def _resolve_targets(repo, target, tests, args):
    """The (target, tests) pairs to review. Default is the single --target;
    --also file.ts[:tests.ts] adds more (the foundation blast-radius scoping
    will populate automatically). Each added file's tests are autodetected
    unless given as file:tests."""
    pairs = [(target, tests)]
    for spec in (getattr(args, "also", None) or []):
        if ":" in spec:
            tgt, tst = spec.split(":", 1)
        else:
            tgt, tst = spec, _default_tests_for(repo, spec)
        if not tst or not os.path.isfile(os.path.join(repo, tst)):
            print(f"  (skipping {tgt}: no tests file found — pass file:tests)")
            continue
        pairs.append((tgt, tst))
    return pairs


_SRC_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs")


def _changed_files(repo: str, base: str, head: str) -> list[str]:
    """Repo-relative files changed on head since it diverged from base."""
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        cwd=repo, capture_output=True, text=True,
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _rel(repo: str, path: str) -> str:
    """Normalize a graph-reported path to repo-relative."""
    p = str(path)
    ap = p if os.path.isabs(p) else os.path.join(repo, p)
    try:
        return os.path.relpath(ap, repo)
    except ValueError:
        return p


def _is_test_file(path: str) -> bool:
    base = os.path.basename(path)
    parts = path.replace("\\", "/").split("/")[:-1]
    return (".test." in base or ".spec." in base
            or any(p in ("tests", "__tests__", "test") for p in parts))


def _host_tests_for(repo: str, target: str) -> str:
    """For a file with NO dedicated tests (a coverage gap), find the nearest
    existing test file to host proposals in: walk up from the target's
    directory toward the repo root, taking the first *.test.<ext> found at
    each level (including tests/ and __tests__/ subdirs). Proposals gated in
    a non-importing host can skew broken_test until scaffolding exists; the
    gate stays honest either way."""
    import glob as _glob

    suffix = next((s for s in _SRC_SUFFIXES if target.endswith(s)), None)
    if suffix is None:
        return ""
    repo_abs = os.path.abspath(repo)
    d = os.path.dirname(os.path.abspath(os.path.join(repo, target)))
    while d.startswith(repo_abs):
        hits: list[str] = []
        for sub in ("", "tests", "__tests__", "test"):
            hits += _glob.glob(os.path.join(d, sub, f"*.test{suffix}"))
        hits = sorted(h for h in set(hits) if "node_modules" not in h)
        if hits:
            return os.path.relpath(hits[0], repo)
        if d == repo_abs:
            break
        d = os.path.dirname(d)
    return ""


def _blast_pairs(repo, base, head, args, have):
    """Blast-radius scoping (--scope/--depth). Returns extra (target, tests)
    pairs; [] when the graph is unavailable (review degrades to the explicit
    targets); None when the selection exceeds --max-files without --yes
    (abort so no tokens are spent by surprise). The graph only answers
    "which files" — the gate still decides everything."""
    try:
        from pathlib import Path

        from .graph import blast_radius, depth_costs, ensure_graph, graph_available
    except Exception:
        print("  (blast radius unavailable: graph wrapper missing; "
              "reviewing explicit targets only)")
        return []
    if not graph_available():
        print("  (blast radius unavailable: pip install code-review-graph; "
              "reviewing explicit targets only)")
        return []

    print(f"building/updating code graph (incremental, base {base})…")
    if not ensure_graph(Path(repo), base=base):
        print("  (graph build failed; reviewing explicit targets only)")
        return []
    changed = _changed_files(repo, base, head)

    _memo: dict[str, bool] = {}

    def has_tests(f: str) -> bool:
        rf = _rel(repo, f)
        if rf not in _memo:
            tst = _default_tests_for(repo, rf)
            _memo[rf] = bool(tst) and os.path.isfile(os.path.join(repo, tst))
        return _memo[rf]

    rows = depth_costs(Path(repo), changed, base=base,
                       max_depth=max(args.depth, 3), gap_probe=has_tests)
    if not rows:
        print("  (graph returned no impact data; reviewing explicit targets only)")
        return []
    print("blast radius cost curve (graph queries only, no tokens spent yet):")
    for row in rows:
        marker = "   <- --depth" if row["depth"] == args.depth else ""
        trunc = " (truncated)" if row.get("truncated") else ""
        print(f"  depth {row['depth']}: {row['files']} impacted file(s), "
              f"{row.get('test_gaps', 0)} without findable tests{trunc}{marker}")

    result = blast_radius(Path(repo), changed, base=base, depth=args.depth)
    if result is None:
        print("  (impact query failed; reviewing explicit targets only)")
        return []

    candidates: list[str] = []
    for f in result["impacted_files"]:
        rf = _rel(repo, f)
        if rf in have or rf in candidates:
            continue
        if _is_test_file(rf) or not rf.endswith(_SRC_SUFFIXES):
            continue
        if not os.path.isfile(os.path.join(repo, rf)):
            continue
        candidates.append(rf)

    if args.scope == "changed":
        changed_set = set(changed)
        selected = [f for f in candidates if f in changed_set]
    elif args.scope == "test-gaps":
        selected = [f for f in candidates if not has_tests(f)]
    else:
        selected = candidates

    print(f"scope '{args.scope}' at depth {args.depth}: "
          f"{len(selected)} additional file(s) selected for the gate")
    if len(selected) > args.max_files and not args.yes:
        print(f"  exceeds --max-files {args.max_files}. Re-run with --yes to "
              "confirm the spend, or lower --depth / tighten --scope.")
        return None

    extra: list[tuple[str, str]] = []
    for f in selected:
        tst = _default_tests_for(repo, f) if has_tests(f) else ""
        if not tst:
            tst = _host_tests_for(repo, f)
            if tst:
                print(f"  (gap file {f}: hosting proposals in {tst})")
        if not tst or not os.path.isfile(os.path.join(repo, tst)):
            print(f"  (skipping {f}: no tests file found — pass {f}:tests "
                  "via --also)")
            continue
        extra.append((f, tst))
    return extra


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentboard",
        description="LLM proposes tests. A deterministic gate decides.",
    )
    sub = parser.add_subparsers(dest="command")
    d = sub.add_parser("demo", help="zero-key demo: gate a bundled buggy target")
    d.add_argument("--fixed", action="store_true",
                   help="run against the fixed target (red -> green)")

    i = sub.add_parser("init", help="write a starter .agentboard.toml for this repo")
    i.add_argument("--repo", default=".", help="path to the repo (default: cwd)")
    i.add_argument("--force", action="store_true", help="overwrite existing config")

    r = sub.add_parser("review", help="review a change on a repo before you push")
    r.add_argument("--repo", default=".", help="path to the repo (default: cwd)")
    r.add_argument("--target", required=True, help="file the change touches (rel to repo)")
    r.add_argument("--tests", default="", help="tests file (default: <target>.test.<ext>)")
    r.add_argument("--head", default="", help="ref to review (default: current branch)")
    r.add_argument("--base", default="", help="ref to diff against (default: fork point)")
    r.add_argument("--intent", default="", help="what the change is meant to do")
    r.add_argument("--issue", default="", help="issue URL to use as intent instead")
    r.add_argument("--no-critic", action="store_true", help="skip the gap-hunting critic pass")
    r.add_argument("--fresh", action="store_true", help="resample proposals (ignore cache)")
    r.add_argument("--timeout", type=int, default=1800, help="per-gate timeout seconds")
    r.add_argument("--also", action="append", default=[],
                   help="additional file to review (repeatable); file or file:tests")
    r.add_argument("--scope", default="", choices=["changed", "test-gaps", "all"],
                   help="blast-radius scoping via the code graph: changed = "
                        "changed files only, test-gaps = impacted files "
                        "without findable tests, all = every impacted file")
    r.add_argument("--depth", type=int, default=2,
                   help="blast-radius hops (only with --scope; default 2)")
    r.add_argument("--max-files", type=int, default=20,
                   help="require --yes past this many scoped files (default 20)")
    r.add_argument("--yes", action="store_true",
                   help="confirm a scoped selection larger than --max-files")
    r.add_argument("--board", default="./review_board.html", help="output board path")
    r.add_argument("--json-out", default="",
                   help="also write a machine-readable run artifact "
                        "(schema_version 1) to this path")

    args = parser.parse_args(argv)
    if args.command == "demo":
        return demo(fixed=args.fixed)
    if args.command == "init":
        return init(args)
    if args.command == "review":
        return review(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())