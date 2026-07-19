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

import json
import os
import re
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


def detect_vitest_projects(repo_root: str) -> list[str]:
    """Best-effort list of vitest project names declared in the repo's config.

    Workspace repos (zod, many monorepos) require `--project <name>` or vitest
    errors with "No projects were found". Guessing it removes the single most
    common reason a repo needs hand-written config. We scan the common config
    files for `name: "..."` inside a projects/workspace/test block. Purely
    heuristic; when unsure we return [] and let the run proceed without
    --project (correct for non-workspace repos)."""
    import re

    candidates = [
        "vitest.config.ts", "vitest.config.js", "vitest.config.mjs",
        "vitest.workspace.ts", "vitest.workspace.js",
        "vite.config.ts", "vite.config.js",
    ]
    names: list[str] = []
    for fname in candidates:
        fpath = os.path.join(repo_root, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            text = open(fpath, encoding="utf-8").read()
        except OSError:
            continue
        # look for `name: "x"` or `name: 'x'` (project definitions)
        for m in re.finditer(r"""\bname\s*:\s*['"]([\w.-]+)['"]""", text):
            if m.group(1) not in names:
                names.append(m.group(1))
    return names


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


LOCKFILES = ("pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lockb")


def detect_project_dir(repo_root: str, target: str) -> str:
    """Repo-relative directory the JS toolchain should run in.

    The git repo root is not always the JS project root: a package can be
    nested inside a larger repo (agentboard's own Python repo carries a JS
    fixture under src/agentboard/demo/target/). Installing at the repo root
    then fails with "no package.json found".

    Walk up from the target: nearest ancestor holding a LOCKFILE wins,
    because that is the install root for workspaces (zod is a pnpm
    workspace whose install must run at the top, and its nested
    packages/zod/package.json must NOT win). Only if no lockfile exists
    anywhere on the path does the nearest package.json win. Repo root is
    the fallback, which is what every single-package repo resolves to, so
    existing behavior is unchanged.
    """
    root = os.path.abspath(repo_root)
    d = os.path.dirname(os.path.abspath(os.path.join(root, target)))
    chain: list[str] = []
    while True:
        chain.append(d)
        if os.path.normpath(d) == os.path.normpath(root) or len(d) <= len(root):
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    for candidate in chain:
        if any(os.path.isfile(os.path.join(candidate, lf)) for lf in LOCKFILES):
            return os.path.relpath(candidate, root)
    for candidate in chain:
        if os.path.isfile(os.path.join(candidate, "package.json")):
            return os.path.relpath(candidate, root)
    return "."


def detect_pnpm_version(scan_root: str) -> str:
    """The pnpm version the repo's config is guaranteed to parse under.

    Reads package.json's packageManager pin. A modern pin (>= 9) is honored
    exactly (any +sha512 integrity suffix stripped — npx wants a plain
    version). An old pin (pnpm 7/8 cannot run on Node 22: ERR_INVALID_THIS)
    or no pin falls back to 9. Found on pathe: pnpm 10/11 repos use
    pnpm-workspace.yaml as a plain config file with no `packages` field,
    which pnpm 9 rejects — so pinning 9 for everyone breaks modern repos the
    same way ambient pnpm 7 broke Node 22."""
    try:
        with open(os.path.join(scan_root, "package.json"), encoding="utf-8") as fh:
            pin = str(json.load(fh).get("packageManager", ""))
    except (OSError, ValueError):
        return "9"
    m = re.match(r"pnpm@(\d+)(?:\.(\d+))?(?:\.(\d+))?", pin)
    if not m or int(m.group(1)) < 9:
        return "9"
    return ".".join(p for p in m.groups() if p is not None)


def build_profile(repo_root: str, cfg: Config, tests_file: str,
                  project_dir: str = ".") -> RepoProfile:
    scan_root = os.path.normpath(os.path.join(repo_root, project_dir))
    kind = cfg.profile_kind or detect_profile_kind(scan_root)
    project = cfg.project
    if project is None:
        detected = detect_vitest_projects(scan_root)
        # only auto-apply when exactly one project is declared; ambiguity
        # (multiple projects) is left to the user / --project to avoid guessing
        if len(detected) == 1:
            project = detected[0]
    if kind == "npm-vitest":
        prof = RepoProfile.npm_vitest(
            os.path.basename(repo_root.rstrip("/")),
            project=project, build=cfg.build,
        )
    else:  # default to pnpm
        prof = RepoProfile.pnpm_vitest(
            os.path.basename(repo_root.rstrip("/")),
            filter=cfg.filter, project=project, build=cfg.build,
            pnpm_version=detect_pnpm_version(scan_root),
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
    worktree: bool = False,
) -> list[str]:
    """Every check that can fail cheaply, run before any token is spent.
    Returns a list of human-readable problems; empty means go.

    `worktree=True` is the agent-session mode: the review's subject IS the
    dirty working tree (diffed and executed as the same on-disk facts), so
    the dirty-tree check inverts from a blocker to the point of the run."""
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

    # only TRACKED modifications count as dirty. An untracked file (a fresh
    # .agentboard.toml, local scratch) doesn't change what HEAD reviews and
    # must not block the run — this exact false-positive stopped the first
    # real review until the config was committed.
    if not worktree:
        tracked_dirty = subprocess.run(
            ["git", "-C", repo_root, "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True,
        ).stdout.strip()
        if tracked_dirty:
            problems.append(
                "tracked files have uncommitted changes — the review sees "
                "committed code only; commit or stash so head reflects what "
                "you mean, or pass --worktree to review the dirty tree itself."
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
