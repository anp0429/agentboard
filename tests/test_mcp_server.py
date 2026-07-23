"""MCP adapter plumbing — no models, no sandbox, no protocol traffic.

The server's one job is to be indistinguishable from the CLI: same
ReviewRequest, same api.run_review pipeline, same artifact. These tests pin
the three adapter-specific behaviors that COULD diverge: narration capture
(protocol safety), artifact passthrough, and error shaping. The review
pipeline itself is tested elsewhere; here run_review is monkeypatched,
because the adapter must not care what it does — only how it is called and
what comes back."""

import dataclasses
import json

import pytest

pytest.importorskip("mcp", reason="MCP extra not installed")

from agentboard import mcp_server  # noqa: E402
from agentboard.api import ReviewRequest, ReviewResult  # noqa: E402


def _fake_run_review(doc=None, rc=0, chatty=True):
    """An api.run_review stand-in that behaves like the real one from the
    adapter's point of view: narrates through the injected log sink, honors
    request.json_out."""
    def fake(request, log=print):
        if chatty:
            log("proposing for a.ts ...")
        if doc is not None:
            with open(request.json_out, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
        return ReviewResult(exit_code=rc)
    return fake


def test_artifact_passthrough_with_log(monkeypatch):
    doc = {"schema_version": 1, "confirmed_gaps": 2, "findings": []}
    monkeypatch.setattr(mcp_server, "run_review", _fake_run_review(doc))
    out = mcp_server._run_review(mcp_server._review_request(repo="/r", target="a.ts"))
    assert out["schema_version"] == 1
    assert out["confirmed_gaps"] == 2
    assert "proposing" in out["log"]


def test_stdout_stays_clean(monkeypatch, capsys):
    """A stray print on stdio IS protocol corruption; narration must be
    collected into the artifact, and real stdout must see nothing."""
    doc = {"schema_version": 1, "findings": []}
    monkeypatch.setattr(mcp_server, "run_review", _fake_run_review(doc))
    mcp_server._run_review(mcp_server._review_request(repo="/r", target="a.ts"))
    assert capsys.readouterr().out == ""


def test_nonzero_exit_is_error_with_log(monkeypatch):
    monkeypatch.setattr(mcp_server, "run_review", _fake_run_review(rc=1))
    out = mcp_server._run_review(mcp_server._review_request(repo="/r", target="a.ts"))
    assert "error" in out and "schema_version" not in out
    assert "proposing" in out["log"]  # the WHY rides along for the caller


def test_crash_is_error_not_server_death(monkeypatch):
    def boom(request, log=print):
        raise RuntimeError("sandbox exploded")
    monkeypatch.setattr(mcp_server, "run_review", boom)
    out = mcp_server._run_review(mcp_server._review_request(repo="/r", target="a.ts"))
    assert "sandbox exploded" in out["error"]


def test_request_covers_every_cli_review_flag(monkeypatch):
    """If a new flag lands in the CLI parser, ReviewRequest must learn the
    field the same day — this is the tripwire. Parse a minimal review
    command through the real parser and require attribute parity between
    the namespace and ReviewRequest's fields (both directions: a flag with
    no field silently diverges the adapters; a field with no flag is dead
    API surface the CLI can never reach)."""
    import agentboard.cli as cli

    captured = {}
    monkeypatch.setattr(cli, "review", lambda ns: captured.setdefault("ns", ns) and 0 or 0)
    cli.main(["review", "--target", "a.ts"])
    cli_flags = set(vars(captured["ns"])) - {"command"}
    request_fields = {f.name for f in dataclasses.fields(ReviewRequest)}
    missing = cli_flags - request_fields
    assert not missing, f"ReviewRequest missing CLI flags: {missing}"
    extra = request_fields - cli_flags
    assert not extra, f"ReviewRequest fields with no CLI flag: {extra}"


def test_unknown_field_fails_loudly():
    """The loud-failure half of the parity story: an adapter passing a name
    ReviewRequest does not know must TypeError, never silently default."""
    with pytest.raises(TypeError):
        mcp_server._review_request(no_such_flag=True)


def test_worktree_is_the_default():
    """The agent-session mode is the default mode; refs mode is the opt-out."""
    assert mcp_server._review_request().worktree is True


def test_prove_tool_is_registered_beside_review():
    """The author-side verb exists as a first-class MCP tool; review stays.
    Registration only — the body lives in _prove, exercised directly below
    the way review's helpers are, because the decorated object's call
    surface belongs to the framework."""
    import agentboard.mcp_server as srv
    assert getattr(srv, "prove", None) is not None
    assert getattr(srv, "review", None) is not None
    assert callable(srv._prove)


def test_prove_tool_asks_for_intent_on_uncommitted_work(monkeypatch, tmp_path):
    """Worktree mode with no derivable intent returns a crisp error dict,
    never a review run with an empty intent."""
    import subprocess

    import agentboard.mcp_server as srv
    r = str(tmp_path)
    for cmd in (["init", "-q", "-b", "main"],
                ["config", "user.email", "t@t"],
                ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", r, *cmd], check=True,
                       capture_output=True)
    (tmp_path / "a.py").write_text("A = 1\n")
    subprocess.run(["git", "-C", r, "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", r, "commit", "-q", "-m", "init"],
                   check=True, capture_output=True)
    (tmp_path / "a.py").write_text("A = 2\n")  # dirty, no intent anywhere
    out = srv._prove(repo=r)
    assert "error" in out
    assert "intent" in out["error"]
