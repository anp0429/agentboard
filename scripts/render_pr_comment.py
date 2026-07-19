#!/usr/bin/env python3
"""Render a PR comment from an agentboard --json-out artifact (schema v1).

Usage: python3 scripts/render_pr_comment.py run.json > comment.md

Design position (deliberate, do not "improve" away):
- The evidence is the product. Confirmed gaps show the failing test source
  and its observed output so a maintainer can judge in seconds.
- Everything that is not a confirmed gap is collapsed. broken_test,
  skipped_covered, and handled details are agentboard's bookkeeping, not
  the maintainer's problem.
- Advisory only. This renderer never suggests blocking anything.
- test_code and observed are nullable (skipped_covered findings never
  generated a test); render must not assume strings.
"""

import json
import sys

MARKER = "<!-- agentboard-review -->"


def _code(text: str, lang: str = "") -> str:
    return f"```{lang}\n{(text or '').rstrip()}\n```"


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as fh:
        doc = json.load(fh)

    findings = doc.get("findings", [])
    gaps = [f for f in findings if f.get("status") == "confirmed_gap"]
    rest = [f for f in findings if f.get("status") != "confirmed_gap"]

    lines: list[str] = [MARKER, "### agentboard review", ""]

    if doc.get("env_error"):
        lines += [
            "**Environment failure: nothing was executed.** "
            "Verdicts below this line are not real results.",
            "",
            _code(doc["env_error"]),
            "",
        ]

    lines += [f"`{doc.get('summary', '')}`", ""]

    if gaps:
        lines.append(
            f"**{len(gaps)} confirmed gap(s).** Each one is a test that "
            "compiled, ran, and failed against this change. Run it yourself "
            "to reproduce."
        )
        lines.append("")
        for i, f in enumerate(gaps, 1):
            lines.append(f"**{i}. {f.get('behavior', '(no description)')}**")
            if f.get("observed"):
                lines += ["", _code(f["observed"])]
            if f.get("test_code"):
                lines += [
                    "",
                    "<details><summary>the test that failed</summary>",
                    "",
                    _code(f["test_code"], "ts"),
                    "",
                    "</details>",
                ]
            lines.append("")
    else:
        lines += [
            "No confirmed gaps. Every proposed behavior either passed "
            "execution against this change or was already covered by the "
            "existing suite.",
            "",
        ]

    if rest:
        lines += [
            "<details><summary>"
            f"{len(rest)} other proposed behavior(s) (passed, covered, or "
            "not executable)</summary>",
            "",
        ]
        for f in rest:
            status = f.get("status", "?")
            lines.append(f"- `{status}` {f.get('behavior', '')}")
        lines += ["", "</details>", ""]

    lines += [
        "---",
        "*Advisory only; a human decides. Verdicts come from executed "
        "tests, never from model judgment. "
        "[agentboard](https://github.com/anp0429/agentboard)*",
    ]

    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
