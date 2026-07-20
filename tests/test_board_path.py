"""The review board must never default into the reviewed repo.

Regression for a real dogfooding bug: the board defaulted to
./review_board.html in the reviewed repo's cwd, so a `git add -A` committed
it, and on the next review that 270-line HTML file became a 19k-char "diff"
that starved the reviewer to zero behaviors. The default now lives in the
system temp dir; an explicit --board still wins."""

import os
import tempfile

from agentboard.cli import main


def _parse_review(argv):
    """Parse a review command up to dispatch, capturing the namespace."""
    import agentboard.cli as cli

    captured = {}
    orig = cli.review
    cli.review = lambda ns: captured.setdefault("ns", ns) or 0
    try:
        cli.main(["review", "--target", "a.ts", *argv])
    finally:
        cli.review = orig
    return captured["ns"]


def test_board_default_is_empty_sentinel():
    # The parser no longer bakes ./review_board.html into the repo cwd.
    ns = _parse_review([])
    assert ns.board == ""


def test_explicit_board_is_preserved():
    ns = _parse_review(["--board", "/tmp/mine.html"])
    assert ns.board == "/tmp/mine.html"


def test_resolved_default_is_outside_any_repo():
    # Mirror the resolution logic in review(): empty -> system temp dir.
    board = "" or os.path.join(tempfile.gettempdir(), "agentboard_review_board.html")
    assert board.startswith(tempfile.gettempdir())
    # and specifically not a relative in-repo path
    assert not board.startswith("./")
    assert "review_board.html" in board


def test_demo_board_never_lands_in_cwd():
    # `demo` is everyone's first command, often run inside a repo. Its board
    # follows the same rule as review: system temp dir, never cwd. Guard the
    # source so the literal cwd-relative path cannot come back.
    import inspect

    import agentboard.cli as cli

    src = inspect.getsource(cli.demo) if hasattr(cli, "demo") else inspect.getsource(cli)
    assert './agentboard_demo_board.html"' not in src.replace("'", '"')
