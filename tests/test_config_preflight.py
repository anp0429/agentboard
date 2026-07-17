"""Config, autodetect, and pre-flight tests — all offline, no API keys.

The usability layer's whole job is to fail fast and clearly BEFORE spending
tokens, and to infer what it can. These tests pin both: bad configs and bad
refs produce specific problems; lockfiles pick profiles; commit messages
become intent.
"""

from __future__ import annotations

import os
import subprocess

from agentboard.config import (
    detect_profile_kind,
    intent_from_commits,
    load_config,
    preflight,
)


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True,
                   capture_output=True, text=True)


def _init_repo(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    return repo


def test_load_config_absent_returns_defaults(tmp_path):
    cfg = load_config(str(tmp_path))
    assert cfg.profile_kind == "" and cfg.run_critic is True


def test_load_config_reads_toml(tmp_path):
    (tmp_path / ".agentboard.toml").write_text(
        'profile = "npm-vitest"\n'
        'project = "unit"\n'
        'base = "develop"\n'
        'critic = false\n'
        'harness_notes = "reuse helpers"\n'
    )
    cfg = load_config(str(tmp_path))
    assert cfg.profile_kind == "npm-vitest"
    assert cfg.project == "unit"
    assert cfg.base == "develop"
    assert cfg.run_critic is False
    assert cfg.harness_notes == "reuse helpers"


def test_detect_profile_from_lockfile(tmp_path):
    d = str(tmp_path)
    assert detect_profile_kind(d) == ""
    (tmp_path / "package-lock.json").write_text("{}")
    assert detect_profile_kind(d) == "npm-vitest"
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert detect_profile_kind(d) == "pnpm-vitest"  # pnpm wins when both exist


def test_preflight_flags_everything_wrong_at_once(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    problems = preflight(
        repo_root=str(tmp_path / "nope"),
        head="x", base="y", target="a.ts", tests="a.test.ts",
        reviewer_model="gpt-5.5", need_critic=False, critic_model="gpt-5.5",
    )
    assert any("not a git repo" in p for p in problems)


def test_preflight_passes_on_a_clean_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    repo = _init_repo(tmp_path)
    with open(os.path.join(repo, "a.ts"), "w") as fh:
        fh.write("export const a = 1\n")
    with open(os.path.join(repo, "a.test.ts"), "w") as fh:
        fh.write("test('a', () => {})\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "commit", "-qm", "head", "--allow-empty")

    problems = preflight(
        repo_root=repo, head="HEAD", base="HEAD~1",
        target="a.ts", tests="a.test.ts",
        reviewer_model="gpt-5.5", need_critic=False, critic_model="gpt-5.5",
    )
    assert problems == [], problems


def test_preflight_names_the_missing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    repo = _init_repo(tmp_path)
    open(os.path.join(repo, "a.ts"), "w").close()
    open(os.path.join(repo, "a.test.ts"), "w").close()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c")
    problems = preflight(
        repo_root=repo, head="HEAD", base="HEAD",
        target="a.ts", tests="a.test.ts",
        reviewer_model="gpt-5.5", need_critic=False, critic_model="gpt-5.5",
    )
    assert any("OPENAI_API_KEY" in p for p in problems)


def test_intent_derived_from_commit_messages(tmp_path):
    repo = _init_repo(tmp_path)
    open(os.path.join(repo, "f.ts"), "w").close()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    with open(os.path.join(repo, "f.ts"), "w") as fh:
        fh.write("changed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fix null handling when input is empty")
    intent = intent_from_commits(repo, "HEAD~1", "HEAD")
    assert "null handling when input is empty" in intent
