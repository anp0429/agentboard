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

from .demo import TARGET_DIR
from .fingerprint import verdict_summary
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentboard",
        description="LLM proposes tests. A deterministic gate decides.",
    )
    sub = parser.add_subparsers(dest="command")
    d = sub.add_parser("demo", help="zero-key demo: gate a bundled buggy target")
    d.add_argument("--fixed", action="store_true",
                   help="run against the fixed target (red -> green)")
    args = parser.parse_args(argv)
    if args.command == "demo":
        return demo(fixed=args.fixed)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
