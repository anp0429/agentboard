"""MCP adapter plumbing — no models, no sandbox, no protocol traffic.

The server's one job is to be indistinguishable from the CLI: same args
namespace, same review() path, same artifact. These tests pin the three
adapter-specific behaviors that COULD diverge: stdout capture (protocol
safety), artifact passthrough, and error shaping. The review pipeline
itself is tested elsewhere; here cli.review is monkeypatched, because the
adapter must not care what it does — only how it is called and what comes
back."""

import argparse
import json
import sys

import pytest

pytest.importorskip("mcp", reason="MCP extra not installed")

from agentboard import mcp_server  # noqa: E402


def _fake_review(doc=None, rc=0, chatty=True):
    """A cli.review stand-in that behaves like the real one from the
    adapter's point of view: prints to stdout, honors ns.json_out."""
    def fake(ns):
        if chatty:
            print("proposing for a.ts ...")
        if doc is not None:
            with open(ns.json_out, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
        return rc
    return fake


def test_artifact_passthrough_with_log(monkeypatch):
    doc = {"schema_version": 1, "confirmed_gaps": 2, "findings": []}
    monkeypatch.setattr("agentboard.cli.review", _fake_review(doc))
    out = mcp_server._run_review(mcp_server._review_args(repo="/r", target="a.ts"))
    assert out["schema_version"] == 1
    assert out["confirmed_gaps"] == 2
    assert "proposing" in out["log"]


def test_stdout_stays_clean(monkeypatch, capsys):
    """A stray print on stdio IS protocol corruption; narration must be
    captured into the artifact, and real stdout must see nothing."""
    doc = {"schema_version": 1, "findings": []}
    monkeypatch.setattr("agentboard.cli.review", _fake_review(doc))
    mcp_server._run_review(mcp_server._review_args(repo="/r", target="a.ts"))
    assert capsys.readouterr().out == ""


def test_nonzero_exit_is_error_with_log(monkeypatch):
    monkeypatch.setattr("agentboard.cli.review", _fake_review(rc=1))
    out = mcp_server._run_review(mcp_server._review_args(repo="/r", target="a.ts"))
    assert "error" in out and "schema_version" not in out
    assert "proposing" in out["log"]  # the WHY rides along for the caller


def test_crash_is_error_not_server_death(monkeypatch):
    def boom(ns):
        raise RuntimeError("sandbox exploded")
    monkeypatch.setattr("agentboard.cli.review", boom)
    out = mcp_server._run_review(mcp_server._review_args(repo="/r", target="a.ts"))
    assert "sandbox exploded" in out["error"]


def test_namespace_covers_every_cli_review_flag(monkeypatch):
    """If a new flag lands in the CLI parser, the adapter must learn it the
    same day — this is the tripwire. Parse a minimal review command through
    the real parser and require attribute parity with _review_args()."""
    import agentboard.cli as cli

    captured = {}
    monkeypatch.setattr(cli, "review", lambda ns: captured.setdefault("ns", ns) and 0 or 0)
    cli.main(["review", "--target", "a.ts"])
    cli_flags = set(vars(captured["ns"])) - {"command"}
    adapter_flags = set(vars(mcp_server._review_args()))
    missing = cli_flags - adapter_flags
    assert not missing, f"MCP adapter namespace missing CLI flags: {missing}"


def test_worktree_is_the_default():
    """The agent-session mode is the default mode; refs mode is the opt-out."""
    assert mcp_server._review_args().worktree is True
