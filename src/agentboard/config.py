"""Config loading, profile auto-detection, and pre-flight — the difference
between "read the source to use it" and "point it at your branch".

Nobody should hand-build a RepoProfile or edit a Python constant to review a
change. `.agentboard.toml` at the repo root holds the repo's stable setup
once; everything else is flags. What can be inferred (the profile, from the
lockfile) is inferred. What must be true (refs resolve, files exist, keys
present) is checked up front, so a misconfigured run fails in two seconds
with a fix hint instead of five minutes into a token-spending pipeline.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field

from .verifiers.vitest_verifier import RepoProfile

CONFIG_NAME = ".agentboard.toml"


@dataclass
class Config:
    profile_kind: str = ""            # "pnpm-vitest" | "npm-vitest" | "" (autodetect)
    project: str | None = None        # vitest --project
    filter: str | None = None         # pnpm --filter (monorepo package)
    base: str = ""                    # default base ref
    build: bool = False
    harness_notes: str = ""
    reviewer_model: str = "gpt-5.5"
    critic_model: str = "gpt-5.5"
    run_critic: bool = True
    extra: dict = field(default_factory=dict)


def load_config(repo_root: str) -> Config:
    path = os.path.join(repo_root, CONFIG_NAME)
    if not os.path.isfile(path):
        return Config()
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return Config(
        profile_kind=data.get("profile", ""),
        project=data.get("project"),
        filter=data.get("filter"),
        base=data.get("base", ""),
        build=bool(data.get("build", False)),
        harness_notes=data.get("harness_notes", ""),
        reviewer_model=data.get("reviewer_model", "gpt-5.5"),
        critic_model=data.get("critic_model", "gpt-5.5"),
        run_critic=bool(data.get("critic", True)),
        extra=data,
    )


def detect_profile_kind(repo_root: str) -> str:
    """Infer the package manager from the lockfile. pnpm wins if both exist
    (pnpm repos often keep a stray package-lock around)."""
    if os.path.isfile(os.path.join(repo_root, "pnpm-lock.yaml")):
        return "pnpm-vitest"
    if os.path.isfile(os.path.join(repo_root, "package-lock.json")):
        return "npm-vitest"
    if os.path.isfile(os.path.join(repo_root, "yarn.lock")):
        return "pnpm-vitest"  # closest preset; user can override in config
    return ""


def build_profile(repo_root: str, cfg: Config, tests_file: str) -> RepoProfile:
    kind = cfg.profile_kind or detect_profile_kind(repo_root)
    if kind == "npm-vitest":
        prof = RepoProfile.npm_vitest(
            os.path.basename(repo_root.rstrip("/")),
            project=cfg.project, build=cfg.build,
        )
    else:  # default to pnpm
        prof = RepoProfile.pnpm_vitest(
            os.path.basename(repo_root.rstrip("/")),
            filter=cfg.filter, project=cfg.project, build=cfg.build,
        )
    if cfg.harness_notes:
        prof.harness_notes = cfg.harness_notes.strip()
    return prof


def _resolves(repo_root: str, ref: str) -> bool:
    r = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def fork_point(repo_root: str, head: str) -> str | None:
    """Best-effort base for commit-level review: merge-base with main/master,
    else the immediate parent. Lets 'review my branch' work with no PR."""
    for candidate in ("main", "master", "origin/main", "origin/master"):
        if _resolves(repo_root, candidate):
            r = subprocess.run(
                ["git", "-C", repo_root, "merge-base", candidate, head],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
    return f"{head}~1" if _resolves(repo_root, f"{head}~1") else None


def current_branch(repo_root: str) -> str:
    r = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() or "HEAD"


def intent_from_commits(repo_root: str, base: str, head: str) -> str:
    """Derive intent from the branch's own commit messages when no --intent
    or --issue is given. The gate stays honest: this is the author's stated
    purpose, not a reviewer's guess at the bug."""
    r = subprocess.run(
        ["git", "-C", repo_root, "log", "--format=%B", f"{base}..{head}"],
        capture_output=True, text=True,
    )
    msgs = r.stdout.strip()
    return msgs[:2000] if msgs else ""


def preflight(
    *,
    repo_root: str,
    head: str,
    base: str,
    target: str,
    tests: str,
    reviewer_model: str,
    need_critic: bool,
    critic_model: str,
) -> list[str]:
    """Every check that can fail cheaply, run before any token is spent.
    Returns a list of human-readable problems; empty means go."""
    problems: list[str] = []

    if not os.path.isdir(os.path.join(repo_root, ".git")):
        problems.append(f"not a git repo: {repo_root}")
        return problems  # nothing else is checkable

    if not _resolves(repo_root, head):
        problems.append(
            f"head ref '{head}' does not resolve — uncommitted work is invisible "
            f"to review; commit first, or pass an existing branch/sha."
        )
    if not _resolves(repo_root, base):
        problems.append(
            f"base ref '{base}' does not resolve — pass --base <branch|sha>, "
            f"or fetch it (git fetch origin {base})."
        )

    dirty = subprocess.run(
        ["git", "-C", repo_root, "status", "--porcelain"],
        capture_output=True, text=True,
    ).stdout.strip()
    if dirty:
        problems.append(
            "working tree has uncommitted changes — the review sees committed "
            "code only; commit or stash so head reflects what you mean."
        )

    for label, rel in (("target", target), ("tests", tests)):
        if rel and not os.path.isfile(os.path.join(repo_root, rel)):
            problems.append(f"{label} file not found in repo: {rel}")

    def _model_needs(m: str) -> str:
        return "OPENAI_API_KEY" if (m.startswith("gpt") or m.startswith("o")) else "ANTHROPIC_API_KEY"

    keys = {_model_needs(reviewer_model)}
    if need_critic:
        keys.add(_model_needs(critic_model))
    for k in sorted(keys):
        if not os.environ.get(k):
            problems.append(f"missing {k} (needed by the model you selected)")

    for tool in ("node", "npm", "git"):
        if shutil.which(tool) is None:
            problems.append(f"'{tool}' not on PATH (the gate runs a real test suite)")

    return problems
