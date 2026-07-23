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
import os
import shutil
import sys
import tempfile
import time

# The review pipeline lives in api.py (the library boundary the MCP server
# shares). The underscored helpers are re-exported because tests and older
# integrations import them from here.
from .api import (  # noqa: F401 - re-exports are part of this module's surface
    ReviewRequest,
    run_review,
    _default_tests_for,
    _resolve_targets,
    _write_json_out,
)
from .demo import TARGET_DIR
from .fingerprint import verdict_summary
from .review import ReviewFinding, ReviewRun, render_review_html
from .verifiers.finding_verifier import FindingVerifier
from .verifiers.vitest_verifier import RepoProfile

_BUG = "Math.min(n, hi - 1)"
_FIX = "Math.min(n, hi)"

# default corpus path for --dataset (bare). Honors AGENTBOARD_DATASET.
_DATASET_DEFAULT = os.environ.get(
    "AGENTBOARD_DATASET",
    os.path.join(os.path.expanduser("~"), ".agentboard", "dataset.jsonl"),
)

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

    # Same rule as review: never write into the user's cwd, it may be a repo.
    board = os.path.join(tempfile.gettempdir(), "agentboard_demo_board.html")
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


def init(args) -> int:
    """Write a starter .agentboard.toml, pre-filled with what we can detect."""
    from .config import (
        CONFIG_NAME,
        detect_profile_kind,
        detect_vitest_projects,
        user_config_path,
    )

    repo = os.path.abspath(os.path.expanduser(args.repo))
    if args.user:
        # Reviewing a repo you don't own: keep its working tree untouched and
        # put the config in the user config dir, keyed by repo name.
        dest = user_config_path(repo)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
    else:
        dest = os.path.join(repo, CONFIG_NAME)
    if os.path.isfile(dest) and not args.force:
        print(dest + " already exists. Use --force to overwrite.")
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
        ("# agentboard config - lives outside the repo (per-repo user config)."
         if args.user else
         "# agentboard config - committed once, shared by everyone reviewing this repo."),
        'profile = "' + kind + '"',
        proj_line,
        'base = "main"',
        'harness_notes = "Tests already import the framework and helpers - reuse them, do not add import statements."',
        "",
    ]
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("wrote " + (dest if args.user else CONFIG_NAME))
    print("  profile: " + kind)
    if len(projects) == 1:
        print("  project: " + projects[0] + " (auto-detected)")
    elif projects:
        print("  projects detected: " + ", ".join(projects) + " - pick one in the file")
    print("Review anytime with:  agentboard review --target <file> --intent <what>")
    return 0


def review(args) -> int:
    """Thin CLI adapter over the library boundary: the parsed namespace
    becomes a ReviewRequest (loudly, field for field — see
    ReviewRequest.from_namespace) and api.run_review does everything else,
    narrating through print, the CLI's sink."""
    return run_review(ReviewRequest.from_namespace(args)).exit_code



def prove(args) -> int:
    """The author-side verb: zero flags, one verdict line first. A thin
    adapter — plan_prove fills in what the user didn't type, run_review
    does everything, prove.verdict_block words the result honestly."""
    from .api import ReviewRequest, run_review
    from .config import ConfigError, load_config
    from .prove import (
        NO_KEY_SCREEN, exit_code_for, gap_details, llm_configured,
        plan_prove, verdict_block,
    )

    import subprocess

    repo = os.path.abspath(os.path.expanduser(args.repo))
    r = subprocess.run(["git", "-C", repo, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("STOPPED: not inside a git repository — prove reviews a "
              "repo's changes; cd into one (or pass --repo).")
        return 1
    repo = r.stdout.strip()

    cfg_base = ""
    try:
        cfg_base = (load_config(repo, "").base_url or "")
    except ConfigError:
        pass  # no config yet is fine for prove; review's defaults apply
    if not llm_configured(config_base_url=cfg_base):
        print(NO_KEY_SCREEN)
        return 1

    plan = plan_prove(repo, args.intent)
    if not plan.targets:
        print(f"prove compared {plan.diffed}: no reviewable source files "
              f"changed (tests and deletions don't count). Nothing to do.")
        return 0
    if not plan.intent:
        print("prove needs one thing it couldn't derive: what is this "
              "change meant to do? Uncommitted work has no commit message.")
        print('  agentboard prove --intent "handle the empty-cart case"')
        return 1

    from .config import untracked_source_files
    ghosts = untracked_source_files(repo)
    if ghosts:
        shown = ", ".join(ghosts[:5]) + (" ..." if len(ghosts) > 5 else "")
        print(f"note: {len(ghosts)} new untracked source file(s) are "
              f"invisible to the diff and NOT reviewed: {shown}")
        print("      include them with:  git add -N " + " ".join(ghosts[:5]))

    print(f"prove: {plan.diffed}")
    print(f"  targets: {', '.join(plan.targets)}"
          + ("" if len(plan.targets) == 1 else "  (first is primary; rest "
             "reviewed via the same run)"))
    print(f"  intent: from {'--intent' if plan.intent_source == 'flag' else 'commit message(s)'}")

    from .prove import prove_board_path
    req = ReviewRequest(
        repo=repo, target=plan.targets[0], also=plan.targets[1:],
        head="" if plan.worktree else plan.head,
        base=plan.base if plan.worktree else plan.base,
        worktree=plan.worktree, intent=plan.intent,
        fresh=args.fresh, timeout=args.timeout,
        board=prove_board_path(),
    )
    result = run_review(req)
    if result.run is None:
        # run_review already printed the specific cause, cause-first.
        print("STOPPED: could not run (see above).")
        return 1
    print()
    print(verdict_block(result.run))
    if result.board_path:
        # the board is the primary human surface: name it immediately,
        # with the command that opens it
        print(f'  board: {result.board_path}')
        print(f'         open "{result.board_path}"')
    for line in gap_details(result.run):
        print(line)
    return exit_code_for(result.run)


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
    i.add_argument("--user", action="store_true",
                   help="write to the user config dir instead of the repo "
                        "(for repos you don't own; leaves their tree untouched)")

    r = sub.add_parser("review", help="review a change on a repo before you push")
    r.add_argument("--repo", default=".", help="path to the repo (default: cwd)")
    r.add_argument("--config", default="",
                   help="config file to use (default: .agentboard.toml in the "
                        "repo, else the per-repo user config)")
    r.add_argument("--target", required=True, help="file the change touches (rel to repo)")
    r.add_argument("--tests", default="", help="tests file (default: <target>.test.<ext>)")
    r.add_argument("--head", default="", help="ref to review (default: current branch)")
    r.add_argument("--base", default="", help="ref to diff against (default: fork point)")
    r.add_argument("--intent", default="", help="what the change is meant to do")
    r.add_argument("--issue", default="", help="issue URL to use as intent instead")
    r.add_argument("--no-critic", action="store_true", help="skip the gap-hunting critic pass")
    r.add_argument("--audit-model", default="",
                   help="model for the advisory gap auditor, which reads the "
                        "source and each confirmed gap's failing test and "
                        "flags likely false positives (wrong assertions). "
                        "Annotates only; verdicts never change. Best set to a "
                        "DIFFERENT model than the reviewer so blind spots "
                        "don't correlate. Default: the critic model.")
    r.add_argument("--no-audit", action="store_true",
                   help="skip the advisory gap auditor")
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
    r.add_argument("--axis", default="default",
                   choices=["default", "security"],
                   help="bias which cases the reviewer proposes; 'security' "
                        "weights toward adversarial/untrusted input. The gate "
                        "and its verdicts are unchanged.")
    r.add_argument("--worktree", action="store_true",
                   help="review the WORKING TREE (uncommitted edits included) "
                        "instead of committed refs. The diff is working tree "
                        "vs --base (default HEAD), and the gate executes the "
                        "same on-disk state it diffed. This is the mode a "
                        "coding agent uses mid-session, before committing.")
    r.add_argument("--board", default="",
                   help="output board path (default: a file in the system temp "
                        "dir, never inside the reviewed repo)")
    r.add_argument("--json-out", default="",
                   help="also write a machine-readable run artifact "
                        "(schema_version 1) to this path")
    r.add_argument("--dataset", nargs="?", const=_DATASET_DEFAULT, default=None,
                   metavar="PATH",
                   help="append one JSONL training row per finding (proposal + "
                        "executed verdict) to a growing corpus. Bare flag uses "
                        f"{_DATASET_DEFAULT}; pass a path to override.")

    p = sub.add_parser("prove", help="try to break your change: BROKEN, "
                       "HELD, or STOPPED, evidence attached")
    p.add_argument("--repo", default=".", help="path inside the repo (default: cwd)")
    p.add_argument("--intent", default="", help="what the change is meant to do "
                   "(default: derived from commit messages; required for "
                   "uncommitted work)")
    p.add_argument("--fresh", action="store_true", help="resample proposals (ignore cache)")
    p.add_argument("--timeout", type=int, default=1800, help="per-gate timeout seconds")

    args = parser.parse_args(argv)
    if args.command == "demo":
        return demo(fixed=args.fixed)
    if args.command == "init":
        return init(args)
    if args.command == "review":
        return review(args)
    if args.command == "prove":
        return prove(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
