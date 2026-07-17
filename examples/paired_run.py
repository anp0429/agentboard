"""Paired run: ONE cached proposal suite, gated against BOTH refs.

    python paired_run.py <cache-key-prefix> <ref-a> <ref-b>
    e.g.  python paired_run.py 786c8934f7f7 main pr6181

Cross-ref claims are only valid when both sides answered the same
questionnaire. The normal pipeline resamples when the source changes (the
cache key includes source content — correctly, for cache freshness), so two
boards from two refs are DIFFERENT suites. This runner pins the suite: load
one cached proposal set, gate it against each ref, print the side-by-side
with transition markers, and render one board per ref.
"""

import copy
import glob
import os
import subprocess
import sys

from agentboard.fingerprint import verdict_summary
from agentboard.proposal_cache import load
from agentboard.review import ReviewRun, render_review_html
from agentboard.verifiers.finding_verifier import FindingVerifier
from agentboard.verifiers.vitest_verifier import RepoProfile

ZOD = "/Users/ankita/Documents/zod"
TESTS = "packages/zod/src/v4/classic/tests/error.test.ts"


def profile() -> RepoProfile:
    return RepoProfile(
        name="zod",
        install_cmd=["pnpm", "install", "--frozen-lockfile"],
        test_base=["npx", "vitest", "run", "--project", "zod", TESTS],
        build_cmd=None,
        env={"CI": "true"},
        smoke_cmd=None,
    )


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    prefix, ref_a, ref_b = sys.argv[1], sys.argv[2], sys.argv[3]

    cache = os.path.expanduser(
        os.environ.get("AGENTBOARD_CACHE_DIR", "~/.agentboard/proposal_cache")
    )
    matches = glob.glob(f"{cache}/{prefix}*.json")
    if len(matches) != 1:
        raise SystemExit(f"{len(matches)} cache entries match {prefix!r}; need exactly 1")
    key = os.path.basename(matches[0])[:-5]
    findings = load(key)
    print(f"suite: {len(findings)} cached proposal(s) from {key[:12]}\n")

    runs = {}
    for ref in (ref_a, ref_b):
        subprocess.run(["git", "-C", ZOD, "checkout", "-q", ref], check=True)
        run = ReviewRun(
            intent=f"paired run vs {ref}", target=TESTS,
            findings=copy.deepcopy(findings),
        )
        print(f"=== {ref} ===")
        FindingVerifier(ZOD, profile(), tests_file=TESTS, timeout=900).run(run)
        for f in run.findings:
            print(f"[{f.status:14}] {f.behavior[:66]}")
            if f.observed and f.status not in ("handled", "skipped_covered"):
                print(f"    -> {f.observed[:130]}")
        print(verdict_summary(run))
        board = f"./paired_board_{ref.replace('/', '_')}.html"
        render_review_html(run, board)
        print(f"board: {board}\n")
        runs[ref] = run

    print("=== side by side (same suite, both refs) ===")
    fixed = broke = 0
    for a, b in zip(runs[ref_a].findings, runs[ref_b].findings):
        marker = ""
        if a.status != b.status:
            if b.status == "handled":
                marker, fixed = "  <-- FIXED by " + ref_b, fixed + 1
            elif a.status == "handled":
                marker, broke = "  <-- REGRESSED in " + ref_b, broke + 1
            else:
                marker = f"  <-- {a.status} -> {b.status}"
        print(f"{ref_a}:{a.status:14} {ref_b}:{b.status:14} {a.behavior[:52]}{marker}")
    print(f"\n{fixed} behavior(s) fixed by {ref_b}, {broke} regressed.")


if __name__ == "__main__":
    main()
