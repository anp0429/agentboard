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
    # OPENAI_BASE_URL legitimately waives the key (local/compatible endpoint),
    # so a developer shell with it exported would flip this test's outcome.
    # A test's verdict must not depend on the terminal it runs in.
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
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


# --- first-run friction fixes ---

def test_autodetect_handles_zod_style_layout(tmp_path):
    """errors.ts (source) -> error.test.ts in a separate tests dir. The exact
    singular/plural + cross-dir shape that forced a manual --tests on zod."""
    from agentboard.cli import _default_tests_for
    repo = str(tmp_path)
    os.makedirs(f"{repo}/src/core")
    os.makedirs(f"{repo}/src/classic/tests")
    open(f"{repo}/src/core/errors.ts", "w").close()
    open(f"{repo}/src/classic/tests/error.test.ts", "w").close()
    assert _default_tests_for(repo, "src/core/errors.ts") == \
        "src/classic/tests/error.test.ts"


def test_autodetect_prefers_colocated(tmp_path):
    from agentboard.cli import _default_tests_for
    repo = str(tmp_path)
    os.makedirs(f"{repo}/src")
    open(f"{repo}/src/thing.ts", "w").close()
    open(f"{repo}/src/thing.test.ts", "w").close()
    assert _default_tests_for(repo, "src/thing.ts") == "src/thing.test.ts"


def test_autodetect_ignores_node_modules(tmp_path):
    from agentboard.cli import _default_tests_for
    repo = str(tmp_path)
    os.makedirs(f"{repo}/node_modules/dep/tests")
    os.makedirs(f"{repo}/src")
    open(f"{repo}/src/parser.ts", "w").close()
    open(f"{repo}/node_modules/dep/tests/parser.test.ts", "w").close()
    # only the node_modules match exists -> must NOT return it; falls back
    got = _default_tests_for(repo, "src/parser.ts")
    assert "node_modules" not in got


def test_autodetect_disambiguates_by_closest_path(tmp_path):
    """A monorepo with the SAME test filename in several packages (zod has
    error.test.ts in v3, v4/mini, and v4/classic). Must pick the one in the
    same subtree as the target, not give up and not pick a distant one."""
    from agentboard.cli import _default_tests_for
    repo = str(tmp_path)
    for p in (
        "packages/zod/src/v4/mini/tests/error.test.ts",
        "packages/zod/src/v4/classic/tests/error.test.ts",
        "packages/zod/src/v3/tests/error.test.ts",
        "packages/zod/src/v4/core/errors.ts",
    ):
        os.makedirs(os.path.join(repo, os.path.dirname(p)), exist_ok=True)
        open(os.path.join(repo, p), "w").close()
    got = _default_tests_for(repo, "packages/zod/src/v4/core/errors.ts")
    assert got == "packages/zod/src/v4/classic/tests/error.test.ts", got


# --- zero-config: vitest project auto-detection + init ---

def test_detects_vitest_project_from_config(tmp_path):
    from agentboard.config import detect_vitest_projects
    (tmp_path / "vitest.config.ts").write_text(
        'export default { test: { projects: [{ test: { name: "zod" } }] } }'
    )
    assert detect_vitest_projects(str(tmp_path)) == ["zod"]


def test_no_vitest_config_means_no_project(tmp_path):
    from agentboard.config import detect_vitest_projects
    assert detect_vitest_projects(str(tmp_path)) == []


def test_build_profile_auto_applies_single_project(tmp_path):
    from agentboard.config import Config, build_profile
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "vitest.config.ts").write_text('name: "mypkg"')
    prof = build_profile(str(tmp_path), Config(), "x.test.ts")
    assert "--project" in prof.test_base
    assert "mypkg" in prof.test_base


def test_multiple_projects_not_auto_applied(tmp_path):
    """Ambiguity is left to the user, not guessed."""
    from agentboard.config import Config, build_profile
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "vitest.config.ts").write_text('name: "a"\nname: "b"')
    prof = build_profile(str(tmp_path), Config(), "x.test.ts")
    assert "--project" not in prof.test_base
