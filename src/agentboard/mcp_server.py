"""MCP adapter — agentboard as a tool a coding agent can call.

The engine's machine boundary is the schema_version-1 JSON artifact
(api._write_json_out). This server is a THIN adapter over that boundary,
exactly like the GitHub Action: it builds the same ReviewRequest the CLI
builds from its parsed flags, runs the same api.run_review pipeline, and
returns the parsed artifact. No review logic lives here; if the CLI and
the MCP server can disagree, one of them is wrong.

Design positions, same as everywhere else in this codebase:

* Advisory, never blocking. The tool RETURNS verdicts; it does not raise on
  confirmed gaps. The calling agent (and ultimately a human) decides what a
  gap means. Errors are for "the review could not run", never "the code is
  bad".
* stdout belongs to the MCP protocol. The pipeline narrates through an
  injected log sink (api.run_review's `log`); every line is collected into
  a per-call buffer and returned in the artifact's `log` field, because a
  stray print() on stdio IS protocol corruption. No global stdout redirect:
  two concurrent calls cannot cross-contaminate each other's narration.
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

import io
import json
import os
import tempfile

from .api import ReviewRequest, run_review

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


def _review_request(**overrides) -> ReviewRequest:
    """The request api.run_review reads, with the adapter's one deliberate
    default flip: worktree=True (an agent gates its dirty edits). An unknown
    field name fails loudly here (TypeError) instead of silently diverging
    between the two adapters; the parity test in tests/test_mcp_server.py
    keeps ReviewRequest itself in lockstep with the CLI parser."""
    overrides.setdefault("worktree", True)
    return ReviewRequest(**overrides)


def _run_review(request: ReviewRequest) -> dict:
    """Run api.run_review with a per-call log sink, return the parsed artifact.

    The artifact (schema_version 1) is the single source of truth; the
    collected narration rides along as `log` so a calling agent can show a
    human WHY a run failed without the server ever printing to stdout."""
    tmpdir = tempfile.mkdtemp(prefix="agentboard-mcp-")
    request.json_out = os.path.join(tmpdir, "run.json")
    if not request.board:
        request.board = os.path.join(tmpdir, "review_board.html")

    buf = io.StringIO()

    def log(*args, **kwargs):  # print-shaped, but into this call's buffer
        kwargs.setdefault("file", buf)
        print(*args, **kwargs)

    try:
        result = run_review(request, log=log)
    except Exception as e:  # noqa: BLE001 - adapter boundary: report, don't crash the server
        return {"error": f"review crashed: {e}", "log": buf.getvalue()}

    narration = buf.getvalue()
    if result.exit_code != 0 or not os.path.isfile(request.json_out):
        return {"error": "review could not run (see log)", "log": narration}
    with open(request.json_out, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["log"] = narration
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
    req = _review_request(
        repo=repo, target=target, intent=intent, tests=tests, base=base,
        head=head, worktree=worktree, axis=axis, no_audit=no_audit,
        fresh=fresh, timeout=timeout,
    )
    return _run_review(req)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
