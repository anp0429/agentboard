"""The library boundary for a review run — one callable, two adapters.

cli.review() used to be the only entry point, so the MCP server had to fake
an argparse Namespace and redirect global stdout to reuse it. That redirect
was process-wide state: two concurrent MCP calls would cross-contaminate
each other's captured narration. This module is the fix at the root:

* ReviewRequest is the real API surface — a dataclass whose fields mirror
  the CLI parser's review namespace one-for-one. The parity test in
  tests/test_mcp_server.py trips the day a CLI flag lands without a field
  here, exactly as it used to trip on the faked namespace.
* run_review(request, log=...) is the whole pipeline. Every human-facing
  line goes through the injected `log` callable (print-shaped), so an
  adapter chooses its own sink: the CLI passes print, the MCP server passes
  a per-call buffer. No global stdout is ever redirected.

The verdict logic is unchanged: this file orchestrates; the gate decides.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field, fields

from .agents.critic_agent import CriticAgent
from .agents.gap_auditor import GapAuditor
from .agents.reviewer_agent import ReviewerAgent
from .config import (
    ConfigError,
    build_profile,
    current_branch,
    detect_project_dir,
    fork_point,
    intent_from_commits,
    load_config,
    preflight,
)
from .fingerprint import verdict_summary
from .proposal_cache import propose_or_cached
from .review import ReviewRun, render_review_html
from .verifiers.finding_verifier import FindingVerifier
from .verifiers.harness import harness_for_profile, harness_for_target


@dataclass
class ReviewRequest:
    """Everything a review run needs, named. Field-for-field mirror of the
    CLI's `review` namespace (tests assert the parity), with the parser's
    own defaults, so a request built with no arguments means exactly what
    `agentboard review` with no flags means."""

    repo: str = "."
    config: str = ""
    target: str = ""
    tests: str = ""
    head: str = ""
    base: str = ""
    intent: str = ""
    issue: str = ""
    no_critic: bool = False
    audit_model: str = ""
    no_audit: bool = False
    fresh: bool = False
    timeout: int = 1800
    also: list[str] = field(default_factory=list)
    scope: str = ""
    depth: int = 2
    max_files: int = 20
    yes: bool = False
    axis: str = "default"
    worktree: bool = False
    board: str = ""
    json_out: str = ""
    dataset: str | None = None

    @classmethod
    def from_namespace(cls, ns) -> "ReviewRequest":
        """Build from a parsed argparse namespace. Attribute access is
        deliberate: a ReviewRequest field with no matching CLI flag fails
        loudly here instead of silently defaulting."""
        return cls(**{f.name: getattr(ns, f.name) for f in fields(cls)})


@dataclass
class ReviewResult:
    """What a run produced. exit_code keeps the CLI's advisory contract:
    0 = the review ran (gaps live in the run, never in the code), 1 = it
    could not run. run is None exactly when exit_code is 1."""

    exit_code: int
    run: ReviewRun | None = None
    board_path: str = ""
    json_path: str = ""


def run_review(request: ReviewRequest, log=print) -> ReviewResult:
    """The full review pipeline: resolve config/refs/tests, propose, gate,
    audit, render the board, write the JSON artifact, append the dataset.
    All narration goes through `log` (print-shaped)."""
    req = request
    repo = os.path.abspath(os.path.expanduser(req.repo))
    try:
        cfg = load_config(repo, req.config)
    except ConfigError as e:
        # the same friendly one-liner the old SystemExit carried, but as an
        # ordinary could-not-run exit: no adapter's process dies over a typo.
        log(str(e))
        return ReviewResult(exit_code=1)

    head = req.head or current_branch(repo)
    base = req.base or cfg.base or (fork_point(repo, head) or "main")
    target = req.target
    tests = req.tests or _default_tests_for(repo, target)

    # friendly, specific guidance for the most common first-run stumble:
    # we couldn't find the tests file and the user didn't say where it is.
    if not req.tests and not os.path.isfile(os.path.join(repo, tests)):
        diff_tests = _tests_from_diff(repo, base, head)
        if len(diff_tests) == 1:
            tests = diff_tests[0]
            log(f"tests: {tests} (the change's own diff names it)")
        else:
            log("agentboard review — couldn't find the tests file.")
            log(f"  Looked for one matching {target!r} but found nothing "
                f"unambiguous.")
            if diff_tests:
                log("  The change itself touches these test files; pass one:")
                for t in diff_tests:
                    log(f"    --tests {t}")
            else:
                log("  Point me at it directly:  --tests path/to/your.test.ts")
            return ReviewResult(exit_code=1)

    need_critic = cfg.run_critic and not req.no_critic
    worktree = bool(req.worktree)
    problems = preflight(
        repo_root=repo, head=head, base=base, target=target, tests=tests,
        reviewer_model=cfg.reviewer_model, need_critic=need_critic,
        critic_model=cfg.critic_model, worktree=worktree,
    )
    if problems:
        log("agentboard review — cannot start:")
        for p in problems:
            log(f"  - {p}")
        return ReviewResult(exit_code=1)

    # intent: --intent > --issue > commit messages on the branch
    from .ingestion.intent import resolve_intent
    if req.intent:
        intent = req.intent
    elif req.issue:
        intent = resolve_intent(issue_url=req.issue)
    elif worktree:
        # Uncommitted work has no commit message; deriving intent from the
        # branch's history would describe the PREVIOUS change, not this one.
        log("agentboard review — cannot start:")
        log("  - --worktree needs --intent (or --issue): uncommitted edits "
            "have no commit message to derive intent from.")
        return ReviewResult(exit_code=1)
    else:
        intent = intent_from_commits(repo, base, head)
        if not intent:
            log("agentboard review — cannot start:")
            log("  - no --intent, no --issue, and no commit messages to derive "
                "intent from. Say what the change is meant to do.")
            return ReviewResult(exit_code=1)
        log(f"intent: derived from commit message(s) on {head}")

    from .ingestion.pr_diff import diff_blob, load_pr_diff, load_worktree_diff
    change = ""
    try:
        if worktree:
            # Agent-session mode: the diff and the sandbox both describe the
            # on-disk working tree, so they cannot disagree. Diff against the
            # given base, or HEAD when none was given (just the uncommitted
            # edits — the usual "gate what I changed this session" question).
            wt_base = req.base or "HEAD"
            change = diff_blob(load_worktree_diff(repo, base=wt_base))
            log(f"change: {len(change)} chars (working tree vs {wt_base})")
        else:
            change = diff_blob(load_pr_diff(repo, head=head, base=base))
            log(f"change: {len(change)} chars ({head} vs {base})")
    except Exception as e:  # noqa: BLE001
        log(f"  - could not load the diff ({e}); aborting rather than "
            "silently reviewing the whole file")
        return ReviewResult(exit_code=1)
    if worktree and not change.strip():
        log("agentboard review — cannot start:")
        log(f"  - --worktree: no changes between the working tree and "
            f"{req.base or 'HEAD'}. Nothing to review.")
        return ReviewResult(exit_code=1)

    project_dir = detect_project_dir(repo, target)
    if project_dir != ".":
        log(f"project root: {project_dir} (nearest lockfile/package.json "
            "above the target)")
    profile = build_profile(repo, cfg, tests, project_dir=project_dir)
    critic = CriticAgent(model=cfg.critic_model, log=log) if need_critic else None

    # the set of (target, tests) pairs to review. Default: the one --target.
    # Multi-target (blast radius) appends more pairs; each is reviewed against
    # the same diff/intent and merged into ONE run so the board and fingerprint
    # cover the whole change.
    pairs = _resolve_targets(repo, target, tests, req, log=log)
    if req.scope:
        extra = _blast_pairs(repo, base, head, req, have={t for t, _ in pairs},
                             log=log)
        if extra is None:
            return ReviewResult(exit_code=1)
        pairs.extend(extra)

    # The advisory precision layer: a SECOND model reads the source and each
    # confirmed gap's failing test, and flags likely false positives (wrong
    # assertions). It annotates only — the gate's verdict never changes.
    # Best with a model different from the reviewer's, so their blind spots
    # don't correlate: --audit-model. Runs only on confirmed gaps, so it
    # costs nothing on a clean run.
    auditor = None
    if not req.no_audit:
        audit_model = req.audit_model or cfg.critic_model or cfg.reviewer_model
        auditor = GapAuditor(model=audit_model, log=log)

    run = ReviewRun(intent=intent, target=target)
    for tgt, tst_path in pairs:
        if len(pairs) > 1:
            log(f"--- reviewing {tgt} ---")
        src = open(os.path.join(repo, tgt), encoding="utf-8").read()
        tst = open(os.path.join(repo, tst_path), encoding="utf-8").read()
        reviewer = ReviewerAgent(repo, tgt, tst_path, model=cfg.reviewer_model,
                                 harness_notes=profile.harness_notes,
                                 axis=req.axis, log=log)
        if req.axis and req.axis != "default":
            log(f"axis: {req.axis} (proposals biased toward "
                "adversarial/untrusted input; the gate is unchanged)")
        log(f"proposing for {tgt} (reviewer {cfg.reviewer_model}"
            + (f" + critic {cfg.critic_model}" if need_critic else "") + ")…")
        findings = propose_or_cached(
            reviewer, critic, intent=intent, change=change, source=src,
            tests=tst, fresh=req.fresh, log=log,
        )
        for f in findings:
            f.source_file = tgt
        log(f"  {len(findings)} behavior(s) to gate")
        if not findings:
            # A reviewer that proposed nothing has not reviewed anything.
            # Reporting "no gaps" here would be a clean bill of health from
            # an exam that never happened, so this is a hard stop, not a
            # warning that scrolls past. The usual cause is a missing or
            # unreachable model client (watch for [warn] lines above).
            log("agentboard review — cannot continue:")
            log(f"  - the reviewer proposed 0 behaviors for {tgt}. Nothing "
                "was reviewed.")
            log("  - check any [warn] lines above: a missing model client "
                "or API key is the usual cause.")
            return ReviewResult(exit_code=1)
        sub = ReviewRun(intent=intent, target=tgt, findings=findings)
        FindingVerifier(repo, profile, tests_file=tst_path,
                        timeout=req.timeout,
                        project_dir=project_dir, log=log,
                        harness=harness_for_profile(profile)).run(sub)
        if auditor is not None:
            gap_count = sum(1 for f in sub.findings if f.status == "confirmed_gap")
            if gap_count:
                log(f"  auditing {gap_count} confirmed gap(s) with "
                    f"{auditor.model} (advisory — verdicts unchanged)…")
                auditor.audit_all(src, sub.findings)
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
        log("\nENVIRONMENT FAILURE: the test environment could not be "
            "prepared, so nothing was executed.")
        log("Verdicts below are not real results. Fix this first:")
        for line in run.env_error.strip().splitlines():
            log(f"  {line}")
        log()

    for f in run.findings:
        tag = f"{f.source_file}: " if len(pairs) > 1 and f.source_file else ""
        log(f"  [{f.status:14}] {tag}{f.behavior[:60]}")
        if f.observed and f.status not in ("handled", "skipped_covered"):
            log(f"       -> {f.observed[:120]}")
        if f.audit:
            log(f"       [auditor] {f.audit}"
                + (f" — {f.audit_reason[:100]}" if f.audit_reason else ""))
    # The board must NOT default into the reviewed repo. Writing
    # ./review_board.html there pollutes the working tree, and a `git add -A`
    # can commit it — which then feeds back as a bogus diff on the next review
    # (a real dogfooding bug: a 270-line board became a 19k-char "change" and
    # starved the reviewer to zero behaviors). Default to the system temp dir;
    # an explicit --board still wins for anyone who wants it in-tree.
    board_path = req.board or os.path.join(
        tempfile.gettempdir(), "agentboard_review_board.html"
    )
    board = render_review_html(run, board_path)
    log(f"\n{verdict_summary(run)}")
    log(f"{len(run.gaps)} confirmed gap(s) across {len(pairs)} file(s). "
        f"Board: {board}")

    if req.json_out:
        _write_json_out(req.json_out, run, repo=repo, base=base, head=head,
                        pairs=pairs, board=board)
        log(f"json: {req.json_out}")

    # Dataset collection: every finding is a labeled training row (proposal +
    # executed verdict). Opt-in, append-only, changes no verdict. See dataset.py.
    if req.dataset:
        from .dataset import append_run
        append_run(
            run, path=req.dataset, repo=repo, base=base, head=head,
            pairs=pairs, intent=intent, axis=req.axis,
            reviewer_model=cfg.reviewer_model, critic_model=cfg.critic_model,
            log=log,
        )
    return ReviewResult(exit_code=0, run=run, board_path=board,
                        json_path=req.json_out or "")


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


def _default_tests_for(repo: str, target: str, dir_fallback: bool = True) -> str:
    """Find the tests file for a target, using the naming conventions of
    whichever framework claims the target's extension (the harness owns
    them: foo.test.ts for vitest, test_foo.py for pytest). Returns "" if no
    framework claims it — the caller then asks for --tests."""
    h = harness_for_target(target)
    return h.default_tests_for(repo, target, dir_fallback) if h else ""


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
                # advisory auditor annotations — null unless a confirmed_gap
                # was audited. Additive and nullable, so schema_version stays 1.
                "audit": f.audit or None,
                "audit_reason": f.audit_reason or None,
                "audit_evidence": f.audit_evidence or None,
            }
            for f in run.findings
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def _resolve_targets(repo, target, tests, args, log=print):
    """The (target, tests) pairs to review. Default is the single --target;
    --also file.ts[:tests.ts] adds more (the foundation blast-radius scoping
    will populate automatically). Each added file's tests are autodetected
    unless given as file:tests."""
    pairs = [(target, tests)]
    for spec in (getattr(args, "also", None) or []):
        if ":" in spec:
            tgt, tst = spec.split(":", 1)
        else:
            tgt, tst = spec, _default_tests_for(repo, spec, dir_fallback=False)
        if not tst or not os.path.isfile(os.path.join(repo, tst)):
            log(f"  (skipping {tgt}: no tests file found — pass file:tests)")
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


def _blast_pairs(repo, base, head, args, have, log=print):
    """Blast-radius scoping (--scope/--depth). Returns extra (target, tests)
    pairs; [] when the graph is unavailable (review degrades to the explicit
    targets); None when the selection exceeds --max-files without --yes
    (abort so no tokens are spent by surprise). The graph only answers
    "which files" — the gate still decides everything."""
    try:
        from pathlib import Path

        from .graph import blast_radius, depth_costs, ensure_graph, graph_available
    except Exception:
        log("  (blast radius unavailable: graph wrapper missing; "
            "reviewing explicit targets only)")
        return []
    if not graph_available():
        log("  (blast radius unavailable: pip install code-review-graph; "
            "reviewing explicit targets only)")
        return []

    log(f"building/updating code graph (incremental, base {base})…")
    if not ensure_graph(Path(repo), base=base):
        log("  (graph build failed; reviewing explicit targets only)")
        return []
    changed = _changed_files(repo, base, head)

    _memo: dict[str, bool] = {}

    def has_tests(f: str) -> bool:
        rf = _rel(repo, f)
        if rf not in _memo:
            tst = _default_tests_for(repo, rf, dir_fallback=False)
            _memo[rf] = bool(tst) and os.path.isfile(os.path.join(repo, tst))
        return _memo[rf]

    rows = depth_costs(Path(repo), changed, base=base,
                       max_depth=max(args.depth, 3), gap_probe=has_tests)
    if not rows:
        log("  (graph returned no impact data; reviewing explicit targets only)")
        return []
    log("blast radius cost curve (graph queries only, no tokens spent yet):")
    for row in rows:
        marker = "   <- --depth" if row["depth"] == args.depth else ""
        trunc = " (truncated)" if row.get("truncated") else ""
        log(f"  depth {row['depth']}: {row['files']} impacted file(s), "
            f"{row.get('test_gaps', 0)} without findable tests{trunc}{marker}")

    result = blast_radius(Path(repo), changed, base=base, depth=args.depth)
    if result is None:
        log("  (impact query failed; reviewing explicit targets only)")
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

    log(f"scope '{args.scope}' at depth {args.depth}: "
        f"{len(selected)} additional file(s) selected for the gate")
    if len(selected) > args.max_files and not args.yes:
        log(f"  exceeds --max-files {args.max_files}. Re-run with --yes to "
            "confirm the spend, or lower --depth / tighten --scope.")
        return None

    extra: list[tuple[str, str]] = []
    for f in selected:
        tst = _default_tests_for(repo, f, dir_fallback=False) if has_tests(f) else ""
        if not tst:
            tst = _host_tests_for(repo, f)
            if tst:
                log(f"  (gap file {f}: hosting proposals in {tst})")
        if not tst or not os.path.isfile(os.path.join(repo, tst)):
            log(f"  (skipping {f}: no tests file found — pass {f}:tests "
                "via --also)")
            continue
        extra.append((f, tst))
    return extra
