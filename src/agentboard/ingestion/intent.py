"""Resolve the loop's GOAL from one of two sources the user chooses:

    1. an issue URL   -> fetch the issue title+body via the GitHub REST API
    2. a goal string  -> use it verbatim

An issue IS the intent, straight from the source — that is how a real PR starts.
A hand-written task string is the escape hatch when there's no issue.

Deliberately tiny and dependency-free (urllib, not a GitHub SDK). Public repos
need no token; a GITHUB_TOKEN env var is used if present (higher rate limit).
"""
from __future__ import annotations

import json
import os
import re
import urllib.request


def from_issue_url(url: str, max_chars: int = 4000) -> str:
    """Fetch '<owner>/<repo> issue #<n>' and return 'TITLE\n\nBODY' as the goal."""
    m = re.search(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)", url)
    if not m:
        raise ValueError(f"not a GitHub issue URL: {url}")
    owner, repo, num = m.groups()
    api = f"https://api.github.com/repos/{owner}/{repo}/issues/{num}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "agentboard"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    req = urllib.request.Request(api, headers=headers)
    data = json.load(urllib.request.urlopen(req, timeout=30))
    title = (data.get("title") or "").strip()
    body = (data.get("body") or "").strip()
    goal = f"{title}\n\n{body}".strip()
    if not goal:
        raise ValueError("issue had no title or body")
    return goal[:max_chars]


def resolve_intent(*, issue_url: str | None = None, goal: str | None = None) -> str:
    """Pick the intent source. Exactly one of issue_url / goal should be set."""
    if issue_url and goal:
        raise ValueError("pass either issue_url OR goal, not both")
    if issue_url:
        return from_issue_url(issue_url)
    if goal:
        return goal.strip()
    raise ValueError("provide issue_url or goal")
