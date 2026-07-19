#!/usr/bin/env python3
"""Regenerate the benchmark table from the run artifacts.

Reads a directory of schema-v1 --json-out files (bench/results/*.json) and
emits a markdown summary: one row per run with verdict counts and the
fingerprint. This is the mechanical half of BENCHMARK.md; the strict/broad/
false-positive adjudication is a human reading source and stays prose, on
purpose, because judging whether an executed failure reflects a real defect
is exactly the call the tool refuses to automate.

Usage:
    python3 bench/report.py bench/results > bench/results/table.md
"""

import json
import os
import sys


def _counts(doc: dict) -> dict:
    c = {}
    for f in doc.get("findings", []):
        c[f.get("status", "?")] = c.get(f.get("status", "?"), 0) + 1
    return c


def _fingerprint(doc: dict) -> str:
    summ = doc.get("summary", "")
    return summ.split("fingerprint:")[-1].strip() if "fingerprint:" in summ else "-"


def main(argv: list[str]) -> int:
    d = argv[1] if len(argv) > 1 else "bench/results"
    files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    if not files:
        print(f"no .json artifacts in {d}", file=sys.stderr)
        return 1

    ORDER = ["confirmed_gap", "handled", "skipped_covered", "broken_test", "timed_out"]
    lines = [
        "# Benchmark runs (mechanical summary)",
        "",
        "Generated from the run artifacts. Verdict counts and fingerprints are",
        "mechanical; strict/broad/false-positive scoring is adjudicated by hand",
        "in BENCHMARK.md (whether an executed failure is a real defect is not a",
        "call this tool makes for itself).",
        "",
        "| run | gaps | handled | covered | broken | timed | env fail | fingerprint |",
        "|-----|------|---------|---------|--------|-------|----------|-------------|",
    ]
    tot = {k: 0 for k in ORDER}
    for fn in files:
        try:
            doc = json.load(open(os.path.join(d, fn), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        c = _counts(doc)
        for k in ORDER:
            tot[k] += c.get(k, 0)
        env = "yes" if doc.get("env_error") else ""
        name = fn[:-5]
        lines.append(
            f"| {name} | {c.get('confirmed_gap', 0)} | {c.get('handled', 0)} | "
            f"{c.get('skipped_covered', 0)} | {c.get('broken_test', 0)} | "
            f"{c.get('timed_out', 0)} | {env} | `{_fingerprint(doc)}` |"
        )
    lines += [
        "",
        f"Totals: {tot['confirmed_gap']} confirmed gaps (pre-adjudication), "
        f"{tot['handled']} handled, {tot['skipped_covered']} covered, "
        f"{tot['broken_test']} broken, {tot['timed_out']} timed out, "
        f"across {len(files)} runs.",
        "",
        "Confirmed-gap counts here are RAW. Real vs false-positive is decided",
        "in BENCHMARK.md by reading source, because the auditor demonstrably",
        "mislabels (see the auditor-inversion note there).",
    ]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
