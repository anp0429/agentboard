"""MCP adapter — agentboard as a tool a coding agent can call.

The engine's machine boundary is the schema_version-1 JSON artifact
(cli._write_json_out). This server is a THIN adapter over that boundary,
exactly like the GitHub Action: it builds the same argparse namespace the
CLI would, runs the same review() path, and returns the parsed artifact.
No review logic lives here; if the CLI and the MCP server can disagree,
one of them is wrong.

Design positions, same as everywhere else in this codebase:

* Advisory, never blocking. The tool RETURNS verdicts; it does not raise on
  confirmed gaps. The calling agent (and ultimately a human) decides what a
  gap means. Errors are for "the review could not run", never "the code is
  bad".
* stdout belongs to the MCP protocol. The CLI narrates to stdout; every
  byte of that narration is captured and returned in the artifact's `log`
  field instead, because a stray print() on stdio IS protocol corruption.
* Worktree by default. An agent calling this mid-session has dirty,
  uncommitted edits — that is the thing it wants gated. `worktree=False`
  restores the CLI's refs mode (head vs base).
* Intent is required. The gate needs to know what the change is MEANT to do,
  and the calling agent knows its own intent better than any commit-message
  heuristic. No silent derivation here.

Run: `agentboard-mcp` (stdio). Requires the `mcp` extra:
`pip install "reviewgate[mcp]"`.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as _e:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "The MCP server needs the 'mcp' package. Install the extra: "
        'pip install "reviewgate[mcp]"'
    ) from _e

INSTRUCTIONS = (
    "agentboard is a review gate that verifies code changes by EXECUTING "
    "tests, not by judging diffs. A reviewer model proposes edge-case tests "
    "for the change; a deterministic harness runs each one against the real "
    "code in a sandbox. A behavior is a confirmed gap only if its test "
    "compiles, runs, and fails its assertion — a crashing test is the test's "
    "problem, never the code's. Verdicts come from execution; treat "
    "confirmed_gap findings as reproducible evidence (each carries its test "
    "source and observed output), and audit annotations as advisory opinion."
)

mcp = FastMCP("agentboard", instructions=INSTRUCTIONS)


def _review_args(**overrides) -> argparse.Namespace:
    """The full namespace cli.review() reads, with CLI-parser defaults.
    Kept in one place so a new CLI flag fails loudly here (AttributeError in
    tests) instead of silently diverging between the two adapters."""
    ns = argparse.Namespace(
        repo=".", config="", target="", tests="", head="", base="", intent="",
        issue="", no_critic=False, audit_model="", no_audit=False, fresh=False,
        timeout=1800, also=[], scope="", depth=2, max_files=20, yes=False,
        axis="default", worktree=True, board="", json_out="", dataset=None,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _run_review(ns: argparse.Namespace) -> dict:
    """Run cli.review() with stdout captured, return the parsed artifact.

    The artifact (schema_version 1) is the single source of truth; the
    captured narration rides along as `log` so a calling agent can show a
    human WHY a run failed without the server ever printing to stdout."""
    from .cli import review as cli_review

    tmpdir = tempfile.mkdtemp(prefix="agentboard-mcp-")
    ns.json_out = os.path.join(tmpdir, "run.json")
    if not ns.board:
        ns.board = os.path.join(tmpdir, "review_board.html")

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cli_review(ns)
    except Exception as e:  # noqa: BLE001 - adapter boundary: report, don't crash the server
        return {"error": f"review crashed: {e}", "log": buf.getvalue()}

    log = buf.getvalue()
    if rc != 0 or not os.path.isfile(ns.json_out):
        return {"error": "review could not run (see log)", "log": log}
    with open(ns.json_out, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["log"] = log
    return doc


@mcp.tool()
def review(
    repo: str,
    target: str,
    intent: str,
    tests: str = "",
    base: str = "",
    head: str = "",
    worktree: bool = True,
    axis: str = "default",
    no_audit: bool = False,
    fresh: bool = False,
    timeout: int = 1800,
) -> dict:
    """Gate a code change by proposing and EXECUTING edge-case tests.

    Reviews `target` (path relative to `repo`) against `intent` — a plain
    statement of what the change is meant to do. By default this reviews the
    WORKING TREE (your uncommitted edits, diffed against `base` or HEAD),
    which is the mode to use mid-session before committing. Pass
    worktree=False to review committed refs (head vs base) instead.

    Returns the schema_version-1 run artifact. Read it like this:
    `confirmed_gaps` is the count of behaviors where a proposed test ran and
    failed its assertion against your code — each such finding carries the
    test source (`test_code`) and the assertion output (`observed`), so the
    evidence is reproducible. `broken_test` findings are the proposer's
    failures, not yours. A non-empty `env_error` means nothing was executed
    and no verdict below it is real. `audit` annotations are advisory
    second-model opinion; they never change a verdict.

    Args:
        repo: absolute path to the git repo.
        target: file the change touches, relative to repo.
        intent: what the change is meant to do (one or two sentences).
        tests: tests file relative to repo (default: autodetected).
        base: ref to diff against (worktree mode default: HEAD; refs mode
            default: the branch fork point).
        head: refs mode only — the ref to review (default: current branch).
        worktree: review on-disk working tree (default) vs committed refs.
        axis: "default" or "security" (biases proposals toward adversarial
            input; the gate is unchanged).
        no_audit: skip the advisory false-positive auditor.
        fresh: resample proposals instead of using the cache.
        timeout: per-gate timeout in seconds.
    """
    ns = _review_args(
        repo=repo, target=target, intent=intent, tests=tests, base=base,
        head=head, worktree=worktree, axis=axis, no_audit=no_audit,
        fresh=fresh, timeout=timeout,
    )
    return _run_review(ns)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
