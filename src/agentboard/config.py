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


class ConfigError(ValueError):
    """A config problem the user must fix (e.g. a bad --config path).

    Deliberately an Exception subclass, unlike the SystemExit this replaced:
    SystemExit sails straight through `except Exception` at the adapter
    boundaries, so a typo'd --config could kill a long-lived MCP server
    instead of failing one call. The friendly message and exit code 1 are
    the api boundary's job (api.run_review), not this module's."""


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
    base_url: str = ""                # pin the OpenAI-compatible endpoint for
                                      # this repo, so shell state cannot
                                      # silently redefine what a model name
                                      # means (env still wins when set)
    extra: dict = field(default_factory=dict)


def user_config_path(repo_root: str) -> str:
    """Where a repo's config lives when it should not live in the repo.

    Reviewing a repo you don't own must leave its working tree untouched:
    an untracked .agentboard.toml trips pre-push hooks and gets swept up by
    `git add -A`. The same settings can live in the user config dir instead,
    keyed by the repo directory's basename:
    <XDG_CONFIG_HOME or ~/.config>/agentboard/repos/<name>.toml
    """
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    name = os.path.basename(os.path.abspath(repo_root)) or "default"
    return os.path.join(base, "agentboard", "repos", name + ".toml")


def load_config(repo_root: str, config_path: str = "") -> Config:
    """Resolve and load config. Precedence: explicit --config path, then
    .agentboard.toml in the repo, then the per-repo user config file."""
    if config_path:
        path = os.path.expanduser(config_path)
        if not os.path.isfile(path):
            raise ConfigError("--config " + config_path + ": file not found")
    else:
        path = os.path.join(repo_root, CONFIG_NAME)
        if not os.path.isfile(path):
            path = user_config_path(repo_root)
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
        base_url=data.get("base_url", ""),
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
    """Infer the toolchain from the repo's own marker files. A JS lockfile
    wins over Python markers (a Python repo that ships a lockfile is
    declaring a JS toolchain; the reverse — a JS repo with a stray
    pyproject — does not happen). pnpm wins if both JS lockfiles exist
    (pnpm repos often keep a stray package-lock around)."""
    if os.path.isfile(os.path.join(repo_root, "pnpm-lock.yaml")):
        return "pnpm-vitest"
    if os.path.isfile(os.path.join(repo_root, "package-lock.json")):
        return "npm-vitest"
    if os.path.isfile(os.path.join(repo_root, "yarn.lock")):
        return "pnpm-vitest"  # closest preset; user can override in config
    # Python: pytest's own config file, any pyproject, or a setup.cfg that
    # carries a pytest section. Deliberately after the lockfile checks.
    if os.path.isfile(os.path.join(repo_root, "pytest.ini")):
        return "pytest"
    if os.path.isfile(os.path.join(repo_root, "pyproject.toml")):
        return "pytest"
    setup_cfg = os.path.join(repo_root, "setup.cfg")
    if os.path.isfile(setup_cfg):
        try:
            text = open(setup_cfg, encoding="utf-8").read()
        except OSError:
            text = ""
        # both spellings seen in the wild: [tool:pytest] is the documented
        # one; [tool.pytest] appears in files converted from pyproject.
        if "[tool:pytest]" in text or "[tool.pytest" in text:
            return "pytest"
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
    same way ambient pnpm 7 broke Node 22.

    The pin also migrates: supabase/mcp dropped packageManager entirely and
    now pins pnpm in mise.toml ([tools] pnpm = "10"), while its workspace
    file uses pnpm-10 fields (ignoredBuiltDependencies). Falling back to 9
    there ran the whole repo under a pnpm its config was never written for.
    So the search order is: packageManager, mise.toml, .tool-versions, 9."""
    pin = ""
    try:
        with open(os.path.join(scan_root, "package.json"), encoding="utf-8") as fh:
            pin = str(json.load(fh).get("packageManager", ""))
    except (OSError, ValueError):
        pass
    m = re.match(r"pnpm@(\d+)(?:\.(\d+))?(?:\.(\d+))?", pin)
    if m and int(m.group(1)) >= 9:
        return ".".join(p for p in m.groups() if p is not None)
    if not m:  # no packageManager pin — check toolchain managers
        v = _pnpm_from_mise(scan_root) or _pnpm_from_tool_versions(scan_root)
        if v:
            return v
    return "9"


def _pnpm_pin_ok(raw: str) -> str:
    """Normalize a toolchain-manager pin to an npx-usable version, or ''.
    Non-numeric channels ("latest", "lts") and ancient pins are rejected —
    the fallback of 9 handles those."""
    m = re.match(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?$", raw.strip())
    if not m or int(m.group(1)) < 9:
        return ""
    return ".".join(p for p in m.groups() if p is not None)


def _pnpm_from_mise(scan_root: str) -> str:
    """pnpm pin from mise.toml's [tools] table. Values can be a bare string,
    a {version = ...} table, or a list (first entry wins, per mise docs)."""
    try:
        import tomllib
        with open(os.path.join(scan_root, "mise.toml"), "rb") as fh:
            tools = tomllib.load(fh).get("tools", {})
    except (OSError, ValueError):
        return ""
    raw = tools.get("pnpm", "")
    if isinstance(raw, dict):
        raw = raw.get("version", "")
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    return _pnpm_pin_ok(str(raw))


def _pnpm_from_tool_versions(scan_root: str) -> str:
    """pnpm pin from an asdf/mise .tool-versions file ("pnpm 10.12.1")."""
    try:
        with open(os.path.join(scan_root, ".tool-versions"), encoding="utf-8") as fh:
            for line in fh:
                parts = line.split("#", 1)[0].split()
                if len(parts) >= 2 and parts[0] == "pnpm":
                    return _pnpm_pin_ok(parts[1])
    except OSError:
        pass
    return ""


def build_profile(repo_root: str, cfg: Config, tests_file: str,
                  project_dir: str = ".") -> RepoProfile:
    scan_root = os.path.normpath(os.path.join(repo_root, project_dir))
    kind = cfg.profile_kind or detect_profile_kind(scan_root)
    if kind == "pytest":
        # Python profile. No install step by default: the gate runs in the
        # environment the user already provisioned (the same interpreter
        # running agentboard), because "pip install a repo's deps into a
        # per-run venv" is a policy decision with real blast radius —
        # deliberately out of scope until someone needs it. No build step
        # either. Smoke = collect the tests file: proves pytest starts, the
        # file parses, and its imports resolve before any finding is judged.
        import sys
        test_base = [sys.executable, "-m", "pytest"]
        prof = RepoProfile(
            name=os.path.basename(repo_root.rstrip("/")),
            install_cmd=[],
            test_base=test_base,
            build_cmd=None,
            env={"CI": "true"},
            smoke_cmd=test_base + ["--collect-only", "-q", tests_file],
            kind="pytest",
        )
        if cfg.harness_notes:
            prof.harness_notes = cfg.harness_notes.strip()
        return prof
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
            # honor the repo's pinned dependency set when it ships one; the
            # verifier retries unfrozen (with a note) if the pin is stale
            frozen=os.path.isfile(os.path.join(scan_root, "pnpm-lock.yaml")),
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
    provider_base_url: str = "",
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

    def _model_needs(m: str) -> str | None:
        """Env key a model requires, or None. Routing rule lives in
        providers.uses_anthropic; this mirrors it. A non-claude model with
        a base URL (env or the repo config's base_url pin) is a
        local/compatible endpoint: no key needed."""
        from .providers import uses_anthropic
        if uses_anthropic(m):
            return "ANTHROPIC_API_KEY"
        if os.environ.get("OPENAI_BASE_URL", "").strip() or provider_base_url:
            return None
        return "OPENAI_API_KEY"

    keys = {k for k in (_model_needs(reviewer_model),) if k}
    if need_critic:
        k = _model_needs(critic_model)
        if k:
            keys.add(k)
    for k in sorted(keys):
        if not os.environ.get(k):
            problems.append(
                f"missing {k} (needed by the model you selected; for a "
                "local OpenAI-compatible server, set OPENAI_BASE_URL instead)"
            )

    # Key-shape truth, learned live: an OpenRouter key (they start sk-or-)
    # aimed at api.openai.com can only be rejected, five expensive seconds
    # from now. Say so here, with the fix.
    if "OPENAI_API_KEY" in keys:
        key = os.environ.get("OPENAI_API_KEY", "")
        if key.startswith("sk-or-"):
            problems.append(
                "OPENAI_API_KEY looks like an OpenRouter key (sk-or-...) but "
                "no base URL is set, so requests would go to api.openai.com "
                "and be rejected. Either export "
                "OPENAI_BASE_URL=https://openrouter.ai/api/v1 or set your "
                "real OpenAI key."
            )

    for tool in ("node", "npm", "git"):
        if shutil.which(tool) is None:
            problems.append(f"'{tool}' not on PATH (the gate runs a real test suite)")

    return problems
