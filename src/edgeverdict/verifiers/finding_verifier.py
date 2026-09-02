"""FindingVerifier — the deterministic judge for review findings.

A reviewer agent writes a test asserting some behavior the intent implies. This
runs that test against the branch and classifies the result. The classification
is the whole trust anchor, because a red test is ambiguous on its own:

    - PASS                 -> the tool already does the right thing  -> "handled"
    - ASSERTION failure    -> the tool did the WRONG thing           -> "confirmed_gap"
    - compile/load/crash   -> the TEST is broken, not the tool       -> "broken_test"
    - did not finish       -> nobody knows yet                       -> "timed_out"

The fourth bucket is deliberately NOT auto-resolved: a timeout is ambiguous
evidence (slow test? hung tool? starved sandbox?) and resolving ambiguity is
the human's job. The gate reports the limit it hit and stops. No retries,
no guessing — the board is where a person decides.

That third bucket is what keeps a red result meaningful: a model that writes a
garbage test must NOT be able to manufacture a "gap." Only a test that actually
runs and fails its assertion counts. No LLM is in this decision.

Note the honest ceiling: a confirmed_gap means the tool violated a *stated*
assertion that compiled and ran. It does NOT prove the assertion itself is the
*right* thing to assert — judging that is the second agent / human layer. This
gate confirms "the test is real and the tool fails it," nothing more.

This module owns the gate SEMANTICS only. Everything framework-specific —
injection, naming, run commands, output parsing, what counts as a named
assertion failure — lives behind the Harness seam (harness.py). The default
harness is vitest, so every existing caller is unchanged.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from ..execution import backend_from_env
from ..review import ReviewFinding, ReviewRun
from .harness import Harness, VitestHarness
from .vitest_verifier import (
    RepoProfile,
    _proc_tail,
    build_tool_seed_cmd,
    no_build_isolation_install,
    scrubbed_env,
    unfrozen_install,
)

# Back-compat aliases: the vitest injection rules moved into VitestHarness
# (harness.py) with their provenance comments; these names stay importable
# here because tests and older callers pin them.
_VITEST = VitestHarness()
_inject = _VITEST.inject
_strip_imports = _VITEST.strip_imports
_test_title = _VITEST.test_title


_COPY_IGNORES = {".git", "node_modules", "dist", "__pycache__"}


def _debug_enabled() -> bool:
    """EDGEVERDICT_DEBUG truthy? Kept out of _run's body so the env read does
    not trip the scrubbed-env call-site guard (test_env_scrub) — _run must
    never build its env from os.environ; reading a debug flag here is fine."""
    return os.environ.get("EDGEVERDICT_DEBUG", "").strip() not in (
        "", "0", "false", "no")


def _faithful_copy(src: str, dst: str) -> None:
    """The one copy step the fidelity check vouches for.

    symlinks=True is load-bearing: posthog's repo ships .claude/skills as a
    symlinked DIRECTORY, and the default deref copy materialized its files
    into the sandbox while os.walk (which never descends symlinked dirs)
    left them out of the host manifest — every file under the link became
    "extra in sandbox" and run 6 was vetoed without executing anything.
    Preserving links keeps copy and checker in the same symlink semantics,
    and closes a fidelity hole besides: dereferencing a repo symlink that
    points OUTSIDE the repo would silently import host files into the
    sandbox. Kept next to _copy_discrepancies so the pair stays honest.
    """
    shutil.copytree(
        src,
        dst,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git", "node_modules", "dist", "__pycache__"
        ),
    )


def _copy_discrepancies(src: str, dst: str, limit: int = 5) -> list[str]:
    """Compare two trees (pruning _COPY_IGNORES) by relative path and size.

    The warm sandbox is only trustworthy if it IS the repo. A copy step that
    silently drops a config file is non-determinism entering through the
    operational layer — the verdict would depend on which files survived the
    copy. This is a pure function so it can be tested without a sandbox.
    Returns up to `limit` human-readable discrepancies; empty means faithful.
    """

    def walk(root: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for cur, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _COPY_IGNORES]
            for name in files:
                full = os.path.join(cur, name)
                rel = os.path.relpath(full, root)
                try:
                    out[rel] = os.path.getsize(full)
                except OSError:
                    out[rel] = -1
        return out

    a, b = walk(src), walk(dst)
    diffs: list[str] = []
    for rel in sorted(set(a) | set(b)):
        if rel not in b:
            diffs.append(f"missing from sandbox: {rel}")
        elif rel not in a:
            diffs.append(f"extra in sandbox: {rel}")
        elif a[rel] != b[rel] and -1 not in (a[rel], b[rel]):
            diffs.append(f"size mismatch: {rel} ({a[rel]} -> {b[rel]} bytes)")
        if len(diffs) >= limit:
            diffs.append("...")
            break
    return diffs


def _strip_pm_noise(tail: str) -> str:
    """Cause first: npm's warn/notice chatter arrives ahead of the real
    error and buried it on two gauntlet boards. _proc_tail labels its
    first line ("stderr: npm warn ..."), so the label must be peeled
    before filtering — the first version of this filter checked the
    labeled line and kept everything (the prefix bug the third board
    exposed). The label is reattached to whatever real cause remains."""
    label = ""
    body = tail
    for lb in ("stderr: ", "stdout: "):
        if body.startswith(lb):
            label, body = lb, body[len(lb):]
            break
    kept = [ln for ln in body.splitlines()
            if not ln.strip().lower().startswith(("npm warn", "npm notice"))
            and not any(n in ln for n in _INIT_NOISE)]
    cleaned = "\n".join(kept).strip()
    return (label + cleaned) if cleaned else tail


def _warm_cache_enabled() -> bool:
    """Cross-run node_modules + smoke cache is OPT-IN until validated. Set
    EDGEVERDICT_WARM_CACHE=1 to reuse an installed dependency tree across
    separate CLI invocations of the same repo — the install (~90s) and the
    smoke probe (~75s) become a one-time cost per lockfile state instead of
    per run. Off by default: a wrong cache would review against stale deps,
    so we ship it behind a flag and fail SAFE (any doubt -> normal install)."""
    return os.environ.get("EDGEVERDICT_WARM_CACHE", "") == "1"


def _warm_cache_root() -> str:
    base = os.environ.get("EDGEVERDICT_WARM_CACHE_DIR") or os.path.join(
        os.path.expanduser("~"), ".edgeverdict", "warm-cache")
    return base


_LOCKFILES = (
    "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lockb",
)

# -- python warm cache (sig EDGEVERDICT_PY_WARM_CACHE_V1) --------------------
# The python lane installs into a user-site under the warm root
# (PYTHONUSERBASE=/edgeverdict/.edgeverdict-pyuser, set by the docker
# backend), a sibling of the repo copy -- not node_modules, not inside the
# repo. Its dependency state is keyed on the resolver's lockfile when the
# repo ships one (uv, poetry, pdm, pipenv), the pinned requirements files,
# and pyproject.toml (an editable `.[test]` install is a function of its
# declared deps). No such file -> None -> normal install, same fail-safe.
_PY_LOCKFILES = ("uv.lock", "poetry.lock", "pdm.lock", "Pipfile.lock")
_PY_MANIFESTS = ("pyproject.toml", "setup.cfg", "setup.py")
_PYUSER_DIR = ".edgeverdict-pyuser"      # under the warm root (mount)
_PY_CACHE_TREE = "pyuser"                # entry subdir holding the user-site


def _py_dep_files(d: str) -> list[str]:
    """Dependency-state files in one directory, sorted for a stable hash:
    lockfiles, requirements*.txt, then the manifest."""
    out = [os.path.join(d, n) for n in _PY_LOCKFILES]
    try:
        out.extend(sorted(os.path.join(d, n) for n in os.listdir(d)
                          if n.startswith("requirements") and n.endswith(".txt")))
    except OSError:
        pass
    out.extend(os.path.join(d, n) for n in _PY_MANIFESTS)
    return out


def _dep_fingerprint(repo_root: str, workdir_rel: str,
                     install_cmd: list[str], kind: str = "") -> str | None:
    """A stable key for the installed dependency state: hash of the lockfile
    (root and package-level, whichever exist) plus the install command. If no
    lockfile is found the deps aren't reproducible enough to cache -> None
    (caller falls back to a normal install). node_modules is a pure function
    of the lockfile, so a matching hash means a matching install.

    kind="pytest" keys on the python dependency files instead
    (sig EDGEVERDICT_PY_WARM_CACHE_V1); every other kind is unchanged."""
    h = hashlib.sha256()
    found = False
    py = kind == "pytest"
    # look for a lockfile at the repo root AND at the package workdir (a
    # monorepo package may have its own, or share the root's).
    seen: set[str] = set()
    for rel_base in ("", workdir_rel):
        d = os.path.normpath(os.path.join(repo_root, rel_base))
        candidates = (_py_dep_files(d) if py
                      else [os.path.join(d, lf) for lf in _LOCKFILES])
        for p in candidates:
            lf = os.path.basename(p)
            if p in seen:
                continue
            seen.add(p)
            if os.path.isfile(p):
                try:
                    with open(p, "rb") as fh:
                        h.update(lf.encode())
                        h.update(fh.read())
                    found = True
                except OSError:
                    return None
    if not found:
        return None
    h.update(("\0".join(install_cmd)).encode())
    # scope to the package dir too, so two packages in one monorepo sharing
    # the root lockfile still get distinct node_modules trees.
    h.update(workdir_rel.encode())
    return h.hexdigest()[:16]


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


_INIT_NOISE = ("TINI_SUBREAPER", "Tini is not running as PID 1",
               "[WARN  tini", "[WARN tini")


def _runner_tail(proc: "subprocess.CompletedProcess[str]", limit: int = 500) -> str:
    """The last meaningful lines of a dead runner's output. stderr is
    preferred (that's where config/boot errors go) but stdout is consulted
    when stderr is empty -- the stream you drop is the stream holding the
    cause. Container-init noise (tini's subreaper warning) is filtered
    first: it is printed on every boot, so a stream holding only that
    noise counts as empty and the other stream gets consulted. ANSI color
    is stripped so boards stay readable."""
    for stream in (proc.stderr, proc.stdout):
        text = _ANSI_RE.sub("", stream or "").strip()
        if not text:
            continue
        lines = [ln.strip() for ln in text.splitlines()
                 if ln.strip() and not any(n in ln for n in _INIT_NOISE)]
        if not lines:
            continue
        tail = " | ".join(lines[-6:])
        return tail[-limit:]
    return ""


def _invalidate_cache_entry(fp: str) -> None:
    """Remove one warm-cache entry wholesale. Best-effort: the entry is
    already suspect, and a failed delete just means the next run retries."""
    shutil.rmtree(os.path.join(_warm_cache_root(), fp), ignore_errors=True)


# -- environment preflight + first-failure triage ----------------------------
# (sig EDGEVERDICT_ENV_PREFLIGHT_V1)
# One afternoon priced this: a missing env var is knowable in ZERO seconds,
# but without these checks it surfaces as minutes of npm DNS backoff
# (EAI_AGAIN under --network none), or as OOM-killed installs at 90-165s a
# run under the default 2g/512m sandbox limits — each retried by a ladder
# that cannot help, each with the real cause swallowed. Three principles:
# refuse up front what cannot succeed; when the first attempt names a cause
# no retry can fix, stop and say the fix; degrade out loud WITH the cause.

_NETWORK_FAILURE_MARKS = ("EAI_AGAIN", "getaddrinfo", "ENOTFOUND")
_RESOURCE_FAILURE_MARKS = (
    "ENOSPC", "No space left on device", "no space left on device",
    "heap out of memory", "Killed", "ENOMEM",
)


def _install_failure_class(proc) -> str:
    """"network" | "resources" | "" for a failed install — the classes whose
    retries are guaranteed wasted (the network will not appear, the memory
    will not grow). Empty string means the ordinary retry ladder applies."""
    rc = getattr(proc, "returncode", 0)
    tail = (proc.stderr or "") + (proc.stdout or "")
    if any(m in tail for m in _NETWORK_FAILURE_MARKS):
        return "network"
    if rc in (137, -9) or any(m in tail for m in _RESOURCE_FAILURE_MARKS):
        return "resources"
    # the repo pins an interpreter the sandbox image does not have: pip
    # reports every candidate as "Requires-Python ..." and finds none. No
    # retry can grow a new python (sig EDGEVERDICT_UV_LOCK_INSTALL_V1).
    if "Requires-Python" in tail and "Could not find a version" in tail:
        return "python"
    return ""


def _repo_requires_python(repo_root: str) -> str:
    """[project].requires-python as written, or ""."""
    try:
        import tomllib
        with open(os.path.join(repo_root, "pyproject.toml"), "rb") as fh:
            return str(tomllib.load(fh).get("project", {})
                       .get("requires-python", "") or "")
    except (OSError, ValueError):
        return ""


_DEFAULT_LIMIT_HINT = (
    "sandbox limits are at defaults (memory 2g, tmpfs 512m) on a WORKSPACE "
    "repo — monorepo installs commonly exceed them and die as OOM or "
    "no-space with misleading errors. If this repo is large, set "
    "EDGEVERDICT_SANDBOX_MEMORY=8g and EDGEVERDICT_TMPFS_SIZE=8g.")


def _limits_at_defaults(backend) -> bool:
    limits = getattr(backend, "limits", None)
    if limits is None:
        return False
    return (getattr(limits, "memory", "") == "2g"
            and getattr(limits, "tmpfs_size", "") == "512m")


# -- cache root anchoring (sig EDGEVERDICT_CACHE_ROOT_ANCHOR_V1) -------------
# The direct runner repoints _workdir to the PACKAGE dir (that is where the
# resolved vitest binary and its package-relative test path live), and it
# activates BEFORE the cache write. Cache paths must NOT follow it: the warm
# cache stores the workspace ROOT node_modules — the tree that owns the .pnpm
# store. Left on _workdir, the write captured apps/<pkg>/node_modules (a
# forest of relative symlinks into a store it does not contain), labeled it
# "node_modules", and the next run restored it to the ROOT: every symlink
# dangled and the runner died booting (MODULE_NOT_FOUND, empty requireStack)
# with smoke skipped by the per-project marker. Install/restore/heal/write
# therefore anchor at _rootdir; _workdir keeps meaning exactly one thing:
# where run commands execute.


def _cache_entry_poisoned(cached_nm: str, install_cmd: list[str]) -> bool:
    """True when a cached "root" node_modules is recognizably a PACKAGE tree
    cached under the root's name (the pre-root-anchor bug): a pnpm-managed
    tree whose top level has no .pnpm virtual store. Restoring such a tree
    to the root leaves every per-package relative symlink dangling."""
    if not any("pnpm" in part for part in install_cmd):
        return False
    if not os.path.isdir(cached_nm):
        return False
    return not os.path.isdir(os.path.join(cached_nm, ".pnpm"))


# -- materialized whole-tree cache (sig EDGEVERDICT_MATERIALIZED_TREE_V1) ----
# pnpm's per-package symlinks are RELATIVE (readlink apps/x/node_modules/dep
# = ../../../node_modules/.pnpm/dep@v/node_modules/dep): they survive any
# clone that keeps the relative layout. So a workspace entry can persist the
# per-package node_modules trees ALONGSIDE the root tree and a restore can
# place both at their recorded repo-relative positions — the whole
# materialized layout comes back valid with NO relink. Entries carrying the
# marker skip the offline relink entirely; entries without it (older format,
# or a capture that failed mid-way) keep the proven relink path. Capture and
# restore are best-effort in the same spirit as the rest of the cache: any
# doubt degrades to the slower correct path, never to a broken tree.

_PKG_TREES_DIR = "pkg-trees"
_MATERIALIZED_MARKER = "materialized.ok"


def _clone_tree(src: str, dst: str) -> None:
    """Copy a directory tree preserving symlinks, using APFS copy-on-write
    cloning when the platform offers it. On macOS `/bin/cp -cR` clones file
    data via clonefile(2) — measured minutes-to-seconds on a multi-GB
    node_modules versus a byte-copying copytree. Anywhere cloning is
    unavailable or fails, fall back to shutil.copytree(symlinks=True): the
    result is identical, only slower. dst must not exist."""
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["/bin/cp", "-cR", src, dst], check=False,
                               capture_output=True, text=True, timeout=1800)
            if r.returncode == 0 and os.path.isdir(dst):
                return
            shutil.rmtree(dst, ignore_errors=True)
        except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
            shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, symlinks=True)


def _enumerate_pkg_nm(root: str) -> list[str]:
    """Repo-relative dirs of every PER-PACKAGE node_modules in a workspace
    tree (apps/x, packages/y, ...) — the root's own node_modules excluded,
    descent into any node_modules and into .git pruned so nested trees inside
    the store are never double-captured."""
    out: list[str] = []
    root = os.path.normpath(root)
    for cur, dirnames, _files in os.walk(root):
        if "node_modules" in dirnames:
            rel = os.path.relpath(cur, root)
            if rel != ".":
                out.append(rel.replace(os.sep, "/"))
            dirnames.remove("node_modules")
        if ".git" in dirnames:
            dirnames.remove(".git")
    return sorted(out)


def _capture_pkg_trees(rootdir: str, cdir: str) -> bool:
    """Persist every per-package node_modules into the cache entry under
    pkg-trees/<pkg_rel>/node_modules, preserving the repo-relative layout the
    relative symlinks depend on. Atomic (built under a tmp dir, os.replace'd
    in) and best-effort: False means the entry simply stays non-materialized
    and the restore path keeps using the offline relink."""
    pkgs = _enumerate_pkg_nm(rootdir)
    if not pkgs:
        return False
    tmp = cdir + ".tmp-" + _PKG_TREES_DIR
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        for rel in pkgs:
            src = os.path.join(rootdir, rel, "node_modules")
            dst = os.path.join(tmp, rel, "node_modules")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            _clone_tree(src, dst)
        final = os.path.join(cdir, _PKG_TREES_DIR)
        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
        with open(os.path.join(cdir, _MATERIALIZED_MARKER), "w") as mf:
            mf.write("\n".join(pkgs))
        return True
    except (OSError, shutil.Error):
        shutil.rmtree(tmp, ignore_errors=True)
        return False


def _restore_pkg_trees(cdir: str, rootdir: str) -> bool:
    """Place a materialized entry's per-package trees back at their recorded
    repo-relative positions. True only when the entry carries the marker and
    EVERY tree restored — a partial workspace is worse than none, so any
    failure removes what was placed and reports False (caller falls back to
    the offline relink)."""
    trees = os.path.join(cdir, _PKG_TREES_DIR)
    if not (os.path.isfile(os.path.join(cdir, _MATERIALIZED_MARKER))
            and os.path.isdir(trees)):
        return False
    placed: list[str] = []
    try:
        for rel in _enumerate_pkg_nm(trees):
            src = os.path.join(trees, rel, "node_modules")
            dst = os.path.join(rootdir, rel, "node_modules")
            shutil.rmtree(dst, ignore_errors=True)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            _clone_tree(src, dst)
            placed.append(dst)
        return True
    except (OSError, shutil.Error):
        for d in placed:
            shutil.rmtree(d, ignore_errors=True)
        return False


# -- v11 workspace relink (sig EDGEVERDICT_WORKSPACE_RELINK_V1) --------------
# The warm cache persists the ROOT node_modules only. In a workspace repo
# (pnpm-workspace.yaml / package.json "workspaces") the package manager also
# materializes PER-PACKAGE node_modules (bin shims, workspace symlinks); a
# restore without them hands the runner a tree it cannot resolve — the v8
# proof-run gap. The fix is not to cache every nested tree (fragile, huge):
# re-run the install with --offline after restore. The store/caches rode
# along with the restore, so the relink materializes per-package trees with
# ZERO network, in seconds. If the offline relink fails, the entry is not
# trustworthy: invalidate + fall through to a fresh install (which re-caches).

def _is_workspace_repo(repo_root: str) -> bool:
    """True when the repo declares a package-manager workspace: a
    pnpm-workspace.yaml at the root, or a root package.json carrying a
    "workspaces" field. Fail-safe: unreadable/absent manifests -> False
    (plain repos never pay the relink)."""
    if os.path.isfile(os.path.join(repo_root, "pnpm-workspace.yaml")):
        return True
    pj = os.path.join(repo_root, "package.json")
    if os.path.isfile(pj):
        try:
            with open(pj, encoding="utf-8") as fh:
                return '"workspaces"' in fh.read()
        except OSError:
            return False
    return False


def _offline_relink_cmd(install_cmd: list[str]) -> list[str] | None:
    """The --offline re-run of the profile's install command, used only on
    a cache-restored workspace tree. None when the command is not an
    install (nothing to re-run) or already offline (idempotent). The
    relink must never reach the network: everything it needs rode along
    in the restored npm-cache/pnpm-store."""
    if "install" not in install_cmd:
        return None
    if "--offline" in install_cmd:
        return None
    return [*install_cmd, "--offline"]


def _project_smoke_marker(tests_file: str) -> str:
    """Cache-entry filename vouching that THIS test project's runner boots.
    Dependencies are a property of the lockfile; a booting runner is a
    property of the test file's own project (vitest config, environment,
    runner bootstrap). One dep tree can serve many projects, so smoke
    markers are per-project: pg-meta's smoke pass must never vouch for
    studio's (the exact over-share that hid studio's dead runner behind
    twelve broken_tests instead of one pre-spend environment failure)."""
    proj = os.path.dirname(tests_file) or "."
    digest = hashlib.sha1(proj.encode()).hexdigest()[:12]
    return f"smoke.{digest}.ok"


def _note_heal(cdir: str, prep_error: str) -> None:
    """Record whether this entry was written by a pass whose smoke FAILED
    on a fresh install. Such an entry is not to be self-healed again: the
    deps are what the install produces, and a second reinstall cannot
    change the smoke result. A clean pass clears the marker.
    (sig EDGEVERDICT_CACHE_ON_INSTALL_V1)"""
    marker = os.path.join(cdir, "heal.tried")
    try:
        if prep_error:
            with open(marker, "w") as mf:
                mf.write(prep_error[:500])
        elif os.path.isfile(marker):
            os.remove(marker)
    except OSError:
        pass


def _nm_complete(cdir: str) -> bool:
    """Evidence that a cached node_modules finished writing. deps.ok is the
    modern marker; a legacy plain smoke.ok (written after the tree, pre
    per-project markers) counts too, as does any per-project marker."""
    if os.path.isfile(os.path.join(cdir, "deps.ok")):
        return True
    if os.path.isfile(os.path.join(cdir, "smoke.ok")):
        return True
    try:
        return any(n.startswith("smoke.") and n.endswith(".ok")
                   for n in os.listdir(cdir))
    except OSError:
        return False


class FindingVerifier:
    def __init__(
        self,
        repo_root: str,
        profile: RepoProfile,
        tests_file: str,
        timeout: int = 1800,
        reuse_warm: bool = False,
        project_dir: str = ".",
        log=print,
        harness: Harness | None = None,
        execution_backend=None,
    ):
        self.repo_root = repo_root
        # print-shaped narration sink; the caller picks where lines go (the
        # CLI passes print, the MCP server a per-call buffer). See api.py.
        self.log = log
        # Repo-relative dir the toolchain runs in. The warm copy is still
        # the whole repo (tests_file and the result file stay repo-relative),
        # but install/build/smoke/test all execute HERE, so a package nested
        # inside a larger repo works. "." for every single-package repo.
        self.project_dir = project_dir
        self.profile = profile
        # The framework seam. Default vitest so every existing caller keeps
        # its exact behavior; api.py selects from the profile.
        self.harness = harness or VitestHarness()
        self.tests_file = (
            tests_file  # where agent tests get injected (helpers in scope)
        )
        # Command-time tests path. The pytest harness passes the path
        # straight to pytest, which resolves it against cwd = project_dir;
        # a repo-relative path doubles the prefix in monorepos
        # (libs/pkg/libs/pkg/...). Injection always writes at the repo-relative
        # path either way — only the COMMAND path is translated.
        self._cmd_tests_file = self.tests_file
        # (1) pnpm --filter execs vitest INSIDE the package dir, so the
        # positional test path must be package-relative or vitest looks for
        # apps/studio/apps/studio/... , finds nothing, and scans the whole
        # workspace (the ~475s hang the debug log exposed). profile.pkg_dir is
        # that package dir. (sig EDGEVERDICT_FILTER_PATH_STRIP_V1)
        _pkg_dir = getattr(self.profile, "pkg_dir", "") or ""
        if _pkg_dir and _pkg_dir not in (".", ""):
            import posixpath
            rel = posixpath.relpath(
                self.tests_file.replace(os.sep, "/"),
                _pkg_dir.replace(os.sep, "/"),
            )
            if not rel.startswith(".."):  # only when the file is under pkg_dir
                self._cmd_tests_file = rel
        # (2) project_dir-based translation (pytest, and vitest profiles that
        # set project_relative_cmd_paths without a filter).
        elif project_dir not in (".", "", None) and getattr(
            self.harness, "project_relative_cmd_paths", False
        ):
            import posixpath
            self._cmd_tests_file = posixpath.relpath(
                self.tests_file.replace(os.sep, "/"),
                project_dir.replace(os.sep, "/"),
            )
        self.timeout = timeout
        self.reuse_warm = (
            reuse_warm  # keep the warm base across run() calls (for N runs)
        )
        # warm-base state (built once, reused per finding)
        self._warm_repo: str | None = None
        self._warm_root: str | None = None
        self._cache_fp: str | None = None  # v13: warm-cache key for re-stow
        self._materialized_restore = False  # per-package trees restored
        self._pristine_tests: str | None = None
        self._prep_error: str = ""
        # Repository lifecycle commands and generated tests never run
        # directly on the host unless the operator explicitly opts in.
        # An explicit backend wins (the demo passes a trusted-fixture
        # LocalBackend for its own in-package target); everything else
        # resolves from the environment, docker by default.
        self._execution_backend = (
            execution_backend
            if execution_backend is not None
            else backend_from_env(log=log)
        )

    def _workdir(self, repo: str) -> str:
        # When a pnpm --filter/pkg_dir is active, the resolved vitest binary
        # runs FROM the package dir (that is where its node_modules/.bin and
        # its package-relative test path resolve). Otherwise the project_dir.
        prof = getattr(self, "profile", None)
        pkg = getattr(prof, "pkg_dir", "") if prof else ""
        if getattr(self, "_direct_runner_active", False) and pkg not in ("", "."):
            return os.path.normpath(os.path.join(repo, pkg))
        return os.path.normpath(os.path.join(repo, self.project_dir))

    def _rootdir(self, repo: str) -> str:
        # The anchor for INSTALL and CACHE operations: the project dir,
        # independent of direct-runner activation. _workdir follows the
        # resolved runner into the package dir; the dependency tree the
        # cache stores and restores lives at the workspace root and must
        # never follow it (sig EDGEVERDICT_CACHE_ROOT_ANCHOR_V1).
        return os.path.normpath(os.path.join(repo, self.project_dir))

    def _resolve_direct_runner(self, repo: str) -> None:
        """After install, swap test_base from `npx pnpm [--filter x] exec
        vitest run` to a DIRECT call of the resolved vitest binary.
        (sig EDGEVERDICT_DIRECT_RUNNER_V2)

        Why: `pnpm exec` re-runs pnpm's whole workspace/lockfile resolution on
        EVERY invocation — measured on supabase/studio at ~76s per call, paid
        by the batch AND each of the 4 confirm lanes (~307s of confirm). The
        vitest binary the install already linked runs the identical file in
        ~0.8s. Install stays via pnpm (once); only the per-test RUN goes direct.

        With a --filter, the binary lives in the package dir and the run must
        happen there (see _workdir), so we resolve <pkg_dir>/node_modules/.bin/
        vitest and drop the pnpm/--filter/exec wrapper, keeping the tail after
        `run` (--project, extra args). No-op (keeps the pnpm-exec fallback) if
        the binary isn't found or this isn't a vitest profile, so nothing
        regresses on layouts we can't resolve."""
        if getattr(self.profile, "kind", None) != "vitest":
            return
        pkg = getattr(self.profile, "pkg_dir", "") or ""
        # search dirs, most specific first: the package dir (filter case),
        # then the workdir, then walk up to repo root.
        starts = []
        if pkg not in ("", "."):
            starts.append(os.path.normpath(os.path.join(repo, pkg)))
        starts.append(os.path.normpath(os.path.join(repo, self.project_dir)))
        found = None
        seen = set()
        for start in starts:
            cur = start
            while True:
                if cur in seen:
                    break
                seen.add(cur)
                cand = os.path.join(cur, "node_modules", ".bin", "vitest")
                if os.path.isfile(cand) or os.path.islink(cand):
                    found = cand
                    break
                if os.path.normpath(cur) == os.path.normpath(repo):
                    break
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent
            if found:
                break
        if not found:
            return  # keep the pnpm-exec fallback
        old = self.profile.test_base
        tail: list[str] = []
        if "run" in old:
            tail = old[old.index("run") + 1:]
        # Store the binary REPO-RELATIVE, not absolute: confirm lanes run in
        # PRIVATE copies of the warm repo (repo_override), and each copy has
        # its OWN node_modules/.bin/vitest. An absolute path would point every
        # lane at the ORIGINAL warm repo's binary while cwd is the copy — a
        # cross-repo mismatch. _direct_test_base(repo) rebinds the binary to
        # whichever repo is actually running.
        self._direct_runner_rel = os.path.relpath(found, repo)
        self._direct_runner_tail = tail
        self._direct_runner_active = True
        # test_base for the shared warm repo (batch, non-override runs)
        self.profile.test_base = self._direct_test_base(repo)
        probe = getattr(self.profile, "smoke_probe", None)
        if probe and getattr(self.profile, "smoke_cmd", None):
            # the probe arg must be package-relative for the same reason the
            # test path is (direct runner execs from the package dir); strip
            # pkg_dir so vitest finds the probe beside the package tests.
            probe_arg = probe[0]
            _pkg = getattr(self.profile, "pkg_dir", "") or ""
            if _pkg not in ("", "."):
                import posixpath
                _r = posixpath.relpath(probe[0].replace(os.sep, "/"),
                                       _pkg.replace(os.sep, "/"))
                if not _r.startswith(".."):
                    probe_arg = _r
            self.profile.smoke_cmd = self.profile.test_base + [probe_arg]
        self.log(f"  direct runner: {self._direct_runner_rel} "
                 "(bypassing pnpm exec per-call overhead)")

    def _direct_test_base(self, repo: str) -> list[str]:
        """The direct-runner test_base bound to a SPECIFIC repo — the shared
        warm repo for batch, or a confirm lane's private copy. Rebuilds the
        absolute binary path from the repo-relative one so each copy runs its
        own vitest binary."""
        binary = os.path.join(repo, self._direct_runner_rel)
        return [binary, "run", *self._direct_runner_tail]

    def _run(self, args, cwd):
        # scrubbed_env remains defense in depth. The backend applies a
        # stricter allowlist before anything reaches untrusted code.
        env = scrubbed_env(self.profile.env, cache_root=self._warm_root)
        # EDGEVERDICT_DEBUG=1: print the EXACT command + cwd BEFORE running,
        # and the wall-clock after — so a hang is visible (you see which
        # command is stuck instead of a frozen terminal), and each phase's
        # real cost is named instead of guessed. (sig EDGEVERDICT_DEBUG_RUN_V1)
        _dbg = _debug_enabled()
        if _dbg:
            rel = os.path.relpath(cwd, self._warm_root) if self._warm_root else cwd
            self.log(f"  [debug] RUN (cwd={rel}): {' '.join(str(a) for a in args)}")
            _t0 = time.monotonic()
        try:
            result = self._execution_backend.run(
                args, cwd=cwd, env=env, timeout=self.timeout
            )
        finally:
            if _dbg:
                self.log(f"  [debug] DONE in {time.monotonic() - _t0:.1f}s")
        # a nonzero exit's CAUSE must never be invisible: under debug, print
        # the failing command's output tail right where it failed, instead
        # of leaving the cause to whichever later branch happens to (or
        # forgets to) surface it. (sig EDGEVERDICT_DEBUG_RUN_V1)
        if _dbg and getattr(result, "returncode", 0) != 0:
            tail = _runner_tail(result, limit=800)
            if tail:
                self.log(f"  [debug] FAILED rc={result.returncode}: {tail}")
        return result

    def _fresh_result_path(self, repo: str) -> str:
        """Where this run's machine-readable results go — with any stale
        artifact from a previous finding removed first. A runner that dies
        before writing (config error, killed on timeout) must yield "no
        output", never a silently re-read verdict from the last finding."""
        out = os.path.join(repo, self.harness.result_file)
        try:
            os.remove(out)
        except FileNotFoundError:
            pass
        return out

    # -- warm base: copy + install + build ONCE ------------------------------
    def _ensure_warm(self) -> None:
        """Build the warm base if it doesn't exist: one copy, one install, one
        build. Every finding reuses this dependency tree; only the tests file
        changes per finding. This is the whole perf win — install/build stop
        being per-finding and become per-run (or, with reuse_warm, per-session).
        """
        if self._warm_repo is not None:
            return
        self._warm_root = tempfile.mkdtemp(prefix="edgeverdict_warm_")
        repo = os.path.join(self._warm_root, "repo")
        phases: list[str] = []
        t0 = time.monotonic()
        _faithful_copy(self.repo_root, repo)
        phases.append(f"copy {time.monotonic() - t0:.1f}s")
        t0 = time.monotonic()
        # the sandbox must BE the repo — a dropped file here would make
        # verdicts depend on copy luck. Fail loudly, never review a ghost.
        diffs = _copy_discrepancies(self.repo_root, repo)
        phases.append(f"fidelity {time.monotonic() - t0:.1f}s")
        if diffs:
            self._prep_error = "sandbox fidelity check failed: " + "; ".join(diffs)
            self._warm_repo = repo
            return
        # capture the pristine tests file ONCE, before any injection
        tpath = os.path.join(repo, self.tests_file)
        if not os.path.isfile(tpath):
            self._prep_error = f"tests file not found: {self.tests_file}"
            self._warm_repo = repo
            return
        with open(tpath, encoding="utf-8") as f:
            self._pristine_tests = f.read()
        # install + build ONCE. A hang here is the same loud prep-error as a
        # nonzero exit — the smoke probe below always caught TimeoutExpired,
        # but install/build let it escape as a traceback through the run.
        # An empty install_cmd means the profile declares no install step
        # (python repos: the running environment is assumed provisioned).
        # cross-run cache: if an installed node_modules for this exact
        # dependency state (lockfile hash) is cached AND it already passed
        # smoke, restore it and skip install+build+smoke entirely. The repo
        # SOURCE is always freshly copied above, so a cached dep tree only
        # ever pairs with fresh code — no stale-code review risk. Fail SAFE:
        # any cache miss/error falls through to the normal install path.
        cache_hit = False       # deps restored (install skipped)
        smoke_skip = False      # THIS project's smoke already vouched for
        fp: str | None = None
        py_lane = (getattr(self.profile, "kind", "") or "") == "pytest"
        if _warm_cache_enabled() and self.profile.install_cmd:
            fp = _dep_fingerprint(
                self.repo_root, self.project_dir, self.profile.install_cmd,
                kind=getattr(self.profile, "kind", "") or "")
            self._cache_fp = fp  # v13: run() re-stows vite artifacts by key
            if fp and py_lane:
                # python lane: the cached tree is the user-site under the
                # warm root, not a node_modules (sig EDGEVERDICT_PY_WARM_CACHE_V1)
                cdir = os.path.join(_warm_cache_root(), fp)
                cached_py = os.path.join(cdir, _PY_CACHE_TREE)
                proj_marker = os.path.join(
                    cdir, _project_smoke_marker(self.tests_file))
                if os.path.isdir(cached_py) and _nm_complete(cdir) \
                        and self._warm_root:
                    dest_py = os.path.join(self._warm_root, _PYUSER_DIR)
                    try:
                        t0 = time.monotonic()
                        shutil.rmtree(dest_py, ignore_errors=True)
                        _clone_tree(cached_py, dest_py)
                        smoke_skip = os.path.isfile(proj_marker)
                        phases.append(
                            f"cache-restore {time.monotonic() - t0:.1f}s "
                            "(python user-site"
                            + ("; skipped install+smoke)" if smoke_skip
                               else "; skipped install; smoke runs: first "
                                    "time this test project rides this "
                                    "dep cache)"))
                        cache_hit = True
                    except (OSError, shutil.Error):
                        shutil.rmtree(dest_py, ignore_errors=True)
                        cache_hit = False
                        smoke_skip = False
            elif fp:
                cdir = os.path.join(_warm_cache_root(), fp)
                cached_nm = os.path.join(cdir, "node_modules")
                proj_marker = os.path.join(
                    cdir, _project_smoke_marker(self.tests_file))
                # a poisoned entry (a package tree cached under the root's
                # name by the pre-root-anchor bug) restores into a tree of
                # dangling symlinks with smoke skipped by its marker — the
                # one failure the self-heal below cannot see. Recognize and
                # invalidate it up front (sig EDGEVERDICT_CACHE_ROOT_ANCHOR_V1).
                if _cache_entry_poisoned(cached_nm, self.profile.install_cmd):
                    self.log("  warm base: cached tree has no .pnpm store "
                             "(a package tree was cached as the root by a "
                             "pre-root-anchor build); invalidating cache "
                             "entry " + fp + " and installing fresh")
                    _invalidate_cache_entry(fp)
                    phases.append("cache-poisoned (invalidated)")
                elif os.path.isdir(cached_nm) and _nm_complete(cdir):
                    dest_nm = os.path.join(self._rootdir(repo), "node_modules")
                    try:
                        t0 = time.monotonic()
                        # clone the cached tree in (a copy, not a symlink:
                        # the sandbox mount + writes during the run must not
                        # mutate the shared cache; CoW cloning makes the
                        # copy cheap where the filesystem supports it).
                        _clone_tree(cached_nm, dest_nm)
                        # the runner BOOTSTRAP rides along: the gate phase
                        # has no network by design, and profiles that launch
                        # via a package-manager bootstrap (npx pnpm) resolve
                        # it from these caches. node_modules without them is
                        # a car without keys -- the exact gap that starved
                        # studio's runner into 12 broken_tests at 74s of
                        # npm retry backoff each.
                        for extra in ("npm-cache", "pnpm-store"):
                            src_x = os.path.join(cdir, extra)
                            if os.path.isdir(src_x) and self._warm_root:
                                dst_x = os.path.join(self._warm_root, extra)
                                shutil.rmtree(dst_x, ignore_errors=True)
                                shutil.copytree(src_x, dst_x, symlinks=True)
                        # materialized entries also carry the per-package
                        # trees; placing them here makes the offline relink
                        # unnecessary (sig EDGEVERDICT_MATERIALIZED_TREE_V1).
                        self._materialized_restore = _restore_pkg_trees(
                            cdir, self._rootdir(repo))
                        smoke_skip = os.path.isfile(proj_marker)
                        phases.append(
                            f"cache-restore {time.monotonic() - t0:.1f}s "
                            + ("(materialized, relink-free"
                               if self._materialized_restore
                               else "(root tree")
                            + ("; skipped install+smoke)" if smoke_skip
                               else "; skipped install; smoke runs: first "
                                    "time this test project rides this "
                                    "dep cache)"))
                        cache_hit = True
                    except (OSError, shutil.Error):
                        # restore failed -> fall through to normal install
                        shutil.rmtree(dest_nm, ignore_errors=True)
                        cache_hit = False
                        smoke_skip = False
                        self._materialized_restore = False
        # v11 workspace relink: a restored ROOT node_modules is not a whole
        # workspace — materialize per-package trees offline, or distrust
        # the entry entirely (sig EDGEVERDICT_WORKSPACE_RELINK_V1). A
        # MATERIALIZED restore already placed the per-package trees, so it
        # skips this entirely (sig EDGEVERDICT_MATERIALIZED_TREE_V1).
        if (cache_hit and fp and not py_lane
                and _is_workspace_repo(self.repo_root)
                and not getattr(self, "_materialized_restore", False)):
            relink = _offline_relink_cmd(self.profile.install_cmd)
            if relink is not None:
                t0 = time.monotonic()
                rl = None
                try:
                    rl = self._run(relink, self._rootdir(repo))
                except subprocess.TimeoutExpired:
                    rl = None
                if rl is None or rl.returncode != 0:
                    self.log("  warm base: offline relink failed on a "
                             "cache-restored workspace; invalidating cache "
                             "entry " + fp + " and retrying with a fresh "
                             "install")
                    _invalidate_cache_entry(fp)
                    shutil.rmtree(os.path.join(self._rootdir(repo),
                                               "node_modules"),
                                  ignore_errors=True)
                    cache_hit = False
                    smoke_skip = False
                    phases.append("relink-failed (cache invalidated)")
                else:
                    phases.append(
                        f"relink {time.monotonic() - t0:.1f}s "
                        "(workspace per-package node_modules, offline)")
        healed = False
        # deps are banked on INSTALL success, not smoke success (sig
        # EDGEVERDICT_CACHE_ON_INSTALL_V1): a smoke failure can be the
        # environment (no USER for getpass, a missing service), which a
        # reinstall never fixes; without this every such run paid the full
        # install again (posthog: 355s, lost to a getuser() OSError).
        install_ok = False
        while True:
            if not cache_hit and self.profile.install_cmd:
                # refuse up front what cannot succeed: a networked install
                # under a --network none policy. Without this it burns
                # minutes of DNS retry backoff before failing with the
                # cause buried. Backends without a network policy (local)
                # are unrestricted and skip the check; --offline installs
                # need no network. (sig EDGEVERDICT_ENV_PREFLIGHT_V1)
                _backend = getattr(self, "_execution_backend", None)
                _policy = getattr(_backend, "network_policy", None)
                if (_policy == "none"
                        and "--offline" not in self.profile.install_cmd):
                    self._prep_error = (
                        "install needs network but the sandbox network "
                        "policy is 'none' — nothing was attempted. Re-run "
                        "with EDGEVERDICT_SANDBOX_NETWORK=install to "
                        "consent to network for the install phase.")
                    phases.append("install-preflight (no network consent)")
                    break
                # a large workspace on default sandbox limits usually dies
                # mid-install with misleading errors; say so BEFORE the
                # spend, with the fix named, not after.
                if (_is_workspace_repo(self.repo_root)
                        and _limits_at_defaults(_backend)):
                    self.log("  install: warning: " + _DEFAULT_LIMIT_HINT)
                t0 = time.monotonic()
                try:
                    inst = self._run(self.profile.install_cmd, self._rootdir(repo))
                    # first-failure triage: when the cause is one no retry
                    # can fix (no network / not enough memory or space),
                    # stop and name the fix instead of paying the whole
                    # retry ladder. (sig EDGEVERDICT_ENV_PREFLIGHT_V1)
                    _fclass = ("" if inst.returncode == 0
                               else _install_failure_class(inst))
                    if _fclass:
                        phases.append(
                            f"install {time.monotonic() - t0:.1f}s "
                            f"({_fclass} failure; retries skipped)")
                        if _fclass == "network":
                            _fix = (
                                "the sandbox could not reach the package "
                                "registry (DNS). If network was intended, "
                                "re-run with EDGEVERDICT_SANDBOX_NETWORK="
                                "install. ")
                        elif _fclass == "python":
                            _pin = _repo_requires_python(self.repo_root)
                            _ver = "".join(
                                ch for ch in _pin if ch.isdigit() or ch == ".")
                            _fix = (
                                "the repo requires python "
                                + (_pin or "(unreadable)")
                                + " and the sandbox image ships a different "
                                "one. Build a matching image: `docker build "
                                "-f docker/Dockerfile.sandbox --build-arg "
                                f"PYTHON_VERSION={_ver or '<version>'} -t "
                                f"edgeverdict-sandbox:py{_ver or '<version>'}"
                                " .` then re-run with EDGEVERDICT_SANDBOX_"
                                f"IMAGE=edgeverdict-sandbox:py{_ver or '<version>'}. ")
                        else:
                            _fix = (
                                "the install ran out of memory or disk in "
                                "the sandbox. Raise EDGEVERDICT_SANDBOX_MEMORY "
                                "(e.g. 8g) and EDGEVERDICT_TMPFS_SIZE (e.g. "
                                "8g) and re-run. ")
                        self._prep_error = ("install failed: " + _fix
                                            + _strip_pm_noise(_proc_tail(inst)))
                        break
                    retry = unfrozen_install(self.profile.install_cmd)
                    if inst.returncode != 0 and retry is not None:
                        # stale lockfile, most likely — degrade to the permissive
                        # install rather than benching the run. Degrading out
                        # loud includes the CAUSE out loud: the frozen
                        # failure's own tail, always, not only under debug —
                        # a swallowed cause turns every frozen retry into a
                        # guessing game about lockfiles vs network vs config.
                        self.log("  install: frozen lockfile install failed; "
                                 "retrying with --no-frozen-lockfile")
                        self.log("  install: frozen failure cause: "
                                 + (_strip_pm_noise(_proc_tail(inst))
                                    or "(no output)"))
                        inst = self._run(retry, self._rootdir(repo))
                    fallback = getattr(self.profile, "install_fallback_cmd", None)
                    if (inst.returncode != 0 and fallback
                            and fallback != self.profile.install_cmd):
                        # the primary install carries heuristic host-file
                        # supplements; a guessed package name must never bench
                        # the run — degrade to declared deps only, out loud.
                        self.log("  install: supplemented install failed; "
                                 "retrying with declared dependencies only")
                        inst = self._run(fallback, self._rootdir(repo))
                    # third rung: a pip BUILD-ISOLATION failure (fetching the
                    # [build-system] requires into pip's throwaway build env)
                    # fails on a fresh resample but not on a warm-cached base,
                    # so the same target flips to "environment failure" between
                    # runs. Detect the build-dep signature and retry with the
                    # build tools seeded + --no-build-isolation. Degrade out
                    # loud; never let a fragile build-env fetch zero the run.
                    tail = _proc_tail(inst)
                    build_iso_failed = inst.returncode != 0 and (
                        "build dependencies" in tail
                        or "getting requirements to build" in tail
                        or "install build dependencies" in tail
                    )
                    nbi = no_build_isolation_install(self.profile.install_cmd)
                    seed = build_tool_seed_cmd(self.profile.install_cmd)
                    if build_iso_failed and nbi is not None and seed is not None:
                        self.log("  install: build-isolation failed (fetching "
                                 "build deps); seeding setuptools+wheel and "
                                 "retrying with --no-build-isolation")
                        seed_res = self._run(seed, self._rootdir(repo))
                        if seed_res.returncode == 0:
                            inst = self._run(nbi, self._rootdir(repo))
                    phases.append(f"install {time.monotonic() - t0:.1f}s")
                    if inst.returncode != 0:
                        self._prep_error = f"install failed: {_proc_tail(inst)}"
                except subprocess.TimeoutExpired:
                    self._prep_error = f"install did not finish within {self.timeout}s"
            if not cache_hit and not self._prep_error and self.profile.build_cmd:
                t0 = time.monotonic()
                try:
                    bld = self._run(self.profile.build_cmd, self._rootdir(repo))
                    phases.append(f"build {time.monotonic() - t0:.1f}s")
                    if bld.returncode != 0:
                        self._prep_error = f"build failed: {_proc_tail(bld)}"
                except subprocess.TimeoutExpired:
                    self._prep_error = f"build did not finish within {self.timeout}s"
            if not cache_hit and not self._prep_error:
                install_ok = True
            # After install/restore linked the deps, swap the per-test command
            # from `pnpm exec` to the resolved vitest binary — the biggest
            # measured speed lever (pnpm exec re-resolves the workspace every
            # call, ~76s; the binary runs the same file in ~0.8s). Done before
            # smoke so the probe runs direct too and vouches for the binary.
            if not self._prep_error:
                self._resolve_direct_runner(repo)
            # functional smoke probe: prove the runner starts before judging
            # anything. An exit code can lie across toolchain versions; a probe
            # that actually launches the runner cannot.
            if (not smoke_skip and not self._prep_error
                    and getattr(self.profile, "smoke_cmd", None)):
                t0 = time.monotonic()
                probe = getattr(self.profile, "smoke_probe", None)
                probe_path = ""
                if probe:
                    # probe[0] is repo-relative; when a --filter/direct-runner
                    # makes _workdir the PACKAGE dir, a repo-relative join
                    # doubles the prefix (apps/studio/apps/studio/...). Strip
                    # pkg_dir so the probe lands beside the package-relative
                    # test path.
                    probe_rel = probe[0]
                    _pkg = getattr(self.profile, "pkg_dir", "") or ""
                    if (getattr(self, "_direct_runner_active", False)
                            and _pkg not in ("", ".")):
                        import posixpath
                        _r = posixpath.relpath(
                            probe[0].replace(os.sep, "/"),
                            _pkg.replace(os.sep, "/"))
                        if not _r.startswith(".."):
                            probe_rel = _r
                    probe_path = os.path.join(self._workdir(repo), probe_rel)
                    with open(probe_path, "w", encoding="utf-8") as pf:
                        pf.write(probe[1])
                try:
                    smoke = self._run(self.profile.smoke_cmd, self._workdir(repo))
                    phases.append(f"smoke {time.monotonic() - t0:.1f}s")
                    if smoke.returncode != 0:
                        self._prep_error = (
                            "environment smoke probe failed: "
                            + _strip_pm_noise(_proc_tail(smoke))
                        )
                except subprocess.TimeoutExpired:
                    self._prep_error = (
                        f"environment smoke probe did not finish within {self.timeout}s"
                    )
                finally:
                    if probe_path and os.path.exists(probe_path):
                        os.remove(probe_path)
            # -- self-healing cache -------------------------------------
            # Smoke failing on a CACHE-RESTORED base indicts the cache, not
            # the repo: the restored tree can predate what the runner needs
            # (a legacy entry without the runner bootstrap starved studio's
            # smoke on zero network). Invalidate the entry and retry ONCE
            # with a fresh install -- which also re-caches the entry in the
            # current format. A smoke failure on a fresh install is the
            # repo's own truth and stands.
            _heal_marker = (os.path.join(_warm_cache_root(), fp, "heal.tried")
                            if fp else "")
            if (self._prep_error and cache_hit and not healed and fp
                    and self.profile.install_cmd
                    and not os.path.isfile(_heal_marker)):
                self.log("  warm base: smoke failed on a cache-restored "
                         "base; invalidating cache entry " + fp
                         + " and retrying with a fresh install")
                _invalidate_cache_entry(fp)
                if py_lane and self._warm_root:
                    shutil.rmtree(os.path.join(self._warm_root, _PYUSER_DIR),
                                  ignore_errors=True)
                else:
                    shutil.rmtree(os.path.join(self._rootdir(repo),
                                               "node_modules"),
                                  ignore_errors=True)
                cache_hit = False
                smoke_skip = False
                self._materialized_restore = False
                self._prep_error = ""
                healed = True
                phases.append("cache-invalidated (self-heal)")
                continue
            break
        # populate the cross-run cache: a fresh install that passed smoke is
        # exactly what the next run of this dep state wants. Only when we did
        # NOT hit the cache, there's no prep error, and we have a fingerprint.
        # Best-effort: a cache-write failure never affects this run.
        if (_warm_cache_enabled() and not cache_hit and install_ok
                and fp and self.profile.install_cmd and py_lane
                and self._warm_root):
            # python lane: persist the user-site the install populated
            # (sig EDGEVERDICT_PY_WARM_CACHE_V1). The editable finder it
            # holds points at the container path of the repo copy, which is
            # the same every run, so a restored site resolves fresh source.
            src_py = os.path.join(self._warm_root, _PYUSER_DIR)
            if os.path.isdir(src_py):
                cdir = os.path.join(_warm_cache_root(), fp)
                try:
                    os.makedirs(cdir, exist_ok=True)
                    tmp_py = cdir + ".tmp-" + _PY_CACHE_TREE
                    shutil.rmtree(tmp_py, ignore_errors=True)
                    _clone_tree(src_py, tmp_py)
                    final_py = os.path.join(cdir, _PY_CACHE_TREE)
                    shutil.rmtree(final_py, ignore_errors=True)
                    os.replace(tmp_py, final_py)
                    with open(os.path.join(cdir, "deps.ok"), "w") as mf:
                        mf.write(fp)
                    _note_heal(cdir, self._prep_error)
                    self.log("  warm base: cached python user-site for "
                             f"reuse (key {fp})")
                except (OSError, shutil.Error):
                    pass  # cache is best-effort; never break the run
        elif (_warm_cache_enabled() and not cache_hit and install_ok
                and fp and self.profile.install_cmd):
            # the ROOT tree, never _workdir's: the direct runner has been
            # active since before smoke, and following it here is the bug
            # that cached a package tree under the root's name
            # (sig EDGEVERDICT_CACHE_ROOT_ANCHOR_V1).
            src_nm = os.path.join(self._rootdir(repo), "node_modules")
            if os.path.isdir(src_nm):
                cdir = os.path.join(_warm_cache_root(), fp)
                try:
                    os.makedirs(cdir, exist_ok=True)
                    tmp_nm = cdir + ".tmp-node_modules"
                    shutil.rmtree(tmp_nm, ignore_errors=True)
                    _clone_tree(src_nm, tmp_nm)
                    final_nm = os.path.join(cdir, "node_modules")
                    shutil.rmtree(final_nm, ignore_errors=True)
                    os.replace(tmp_nm, final_nm)
                    # workspace repos: persist the per-package trees too, so
                    # the next restore is materialized and relink-free
                    # (sig EDGEVERDICT_MATERIALIZED_TREE_V1). Best-effort —
                    # a failed capture leaves a valid relink-path entry.
                    if _is_workspace_repo(self.repo_root):
                        _capture_pkg_trees(self._rootdir(repo), cdir)
                    # the runner bootstrap rides along with the dep tree:
                    # the install phase (the only networked phase) fetched
                    # the package-manager bootstrap into the session caches;
                    # persist them so a future cache-restore can launch the
                    # runner with zero network.
                    if self._warm_root:
                        for extra in ("npm-cache", "pnpm-store"):
                            src_x = os.path.join(self._warm_root, extra)
                            if not os.path.isdir(src_x):
                                continue
                            tmp_x = cdir + ".tmp-" + extra
                            shutil.rmtree(tmp_x, ignore_errors=True)
                            shutil.copytree(src_x, tmp_x, symlinks=True)
                            final_x = os.path.join(cdir, extra)
                            shutil.rmtree(final_x, ignore_errors=True)
                            os.replace(tmp_x, final_x)
                    # deps.ok written after the tree, so a half-written
                    # cache (node_modules present, marker absent) is never
                    # served. Smoke markers are separate and per-project.
                    with open(os.path.join(cdir, "deps.ok"), "w") as mf:
                        mf.write(fp)
                    _note_heal(cdir, self._prep_error)
                    self.log("  warm base: cached deps for reuse "
                             f"(key {fp})")
                except (OSError, shutil.Error):
                    pass  # cache is best-effort; never break the run
        # per-project smoke marker: written whenever smoke actually RAN and
        # passed for this tests file's project -- on a fresh install AND on
        # an nm-restored run whose project was riding this dep cache for
        # the first time. One dep tree, many projects, each vouched
        # individually.
        if (_warm_cache_enabled() and not smoke_skip and not self._prep_error
                and fp and self.profile.install_cmd
                and getattr(self.profile, "smoke_cmd", None)):
            cdir = os.path.join(_warm_cache_root(), fp)
            if (os.path.isdir(os.path.join(cdir, "node_modules"))
                    or os.path.isdir(os.path.join(cdir, _PY_CACHE_TREE))):
                try:
                    marker = os.path.join(
                        cdir, _project_smoke_marker(self.tests_file))
                    with open(marker, "w") as mf:
                        mf.write(fp)
                except OSError:
                    pass  # best-effort
        self.log("  warm base: " + ", ".join(phases))
        self._warm_repo = repo

    # -- v13 vite re-stow (sig EDGEVERDICT_VITE_RESTOW_V1) -------------------
    def _restow_vite_cache(self) -> None:
        """Persist the run's vite dep-optimizer artifacts (node_modules/.vite)
        back into the warm-cache entry. The restore path already carries a
        .vite dir if the cached tree had one — but the FIRST run per dep
        state pays the full cold-transform cost (~priced at minutes on
        studio) and, without this, pays it again every run. Re-stowing after
        each run makes the transforms a one-time cost per lockfile state,
        the same deal install and smoke already get. Atomic (tmp +
        os.replace) so a killed run never leaves a half-written cache, and
        best-effort: a re-stow failure never affects the finished run."""
        if not (_warm_cache_enabled() and self._cache_fp and self._warm_repo):
            return
        src = os.path.join(self._workdir(self._warm_repo),
                           "node_modules", ".vite")
        if not os.path.isdir(src):
            return
        cdir = os.path.join(_warm_cache_root(), self._cache_fp)
        cached_nm = os.path.join(cdir, "node_modules")
        if not os.path.isdir(cached_nm):
            return  # entry gone (invalidated mid-run): nothing to enrich
        # for a filtered package, .vite lives in the PACKAGE tree — the
        # matching slot in the entry is pkg-trees/<pkg_rel>/node_modules
        # (sig EDGEVERDICT_MATERIALIZED_TREE_V1). Stowing it under the
        # cached ROOT tree would restore it where vite never looks. If the
        # entry has no materialized slot for the package, skip: dead weight
        # is not enrichment.
        wd = self._workdir(self._warm_repo)
        rd = self._rootdir(self._warm_repo)
        if os.path.normpath(wd) != os.path.normpath(rd):
            pkg_rel = os.path.relpath(wd, rd).replace(os.sep, "/")
            cached_nm = os.path.join(
                cdir, _PKG_TREES_DIR, pkg_rel, "node_modules")
            if not os.path.isdir(cached_nm):
                return
        try:
            tmp = cdir + ".tmp-vite"
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(src, tmp, symlinks=True)
            final = os.path.join(cached_nm, ".vite")
            shutil.rmtree(final, ignore_errors=True)
            os.replace(tmp, final)
            self.log("  warm base: re-stowed vite cache into entry "
                     + self._cache_fp)
        except (OSError, shutil.Error):
            pass  # cache is best-effort; never break the run

    def close(self) -> None:
        """Delete backend resources and the warm base."""
        self._execution_backend.close()
        if self._warm_root:
            shutil.rmtree(self._warm_root, ignore_errors=True)
        self._warm_root = self._warm_repo = self._pristine_tests = None
        self._prep_error = ""

    def classify(self, finding: ReviewFinding,
                 repo_override: str | None = None) -> ReviewFinding:
        """Inject this finding's test into the warm base's pristine tests file
        and run ONLY it. Reuses the shared dependency tree; resets the tests
        file to pristine first so no finding sees another's injected test.

        repo_override (v12, sig EDGEVERDICT_PARALLEL_CONFIRM_V1): run against
        a PRIVATE copy of the warm repo instead of the shared one — the
        parallel confirmation lanes each write their own tests file and read
        their own result file, so concurrent re-gates never collide. The
        default (None) is the shared warm repo, byte-identical behavior."""
        if finding.covered_by_existing:
            finding.status = "skipped_covered"
            return finding
        self._ensure_warm()
        if self._prep_error:  # install/build failed -> nothing can run
            finding.status = "broken_test"
            finding.observed = self._prep_error
            return finding
        title = self.harness.test_title(finding.test_code or "")
        if not title:
            finding.status = "broken_test"
            finding.observed = "could not read test name"
            return finding
        # Parameterized proposals (@parameterized.expand / @pytest.mark.
        # parametrize) fan one def into N suffixed node ids, so exact-node-id
        # serial selection collects nothing. For these, stamp a unique gate
        # mark into the function name and select by -k on it: the mark is a
        # substring of every generated variant and cannot collide with a
        # host test, preserving the no-misattribution guarantee.
        is_param = getattr(self.harness, "_is_parameterized", None)
        parameterized = bool(is_param and is_param(finding.test_code or ""))
        serial_title = title
        test_code = finding.test_code or ""
        if parameterized:
            _mark = "___evp0___"
            marked = self.harness.mark_title(test_code, _mark)
            if marked is not None:
                test_code = marked
                # recompute the (now-marked) title for -k selection
                serial_title = self.harness.test_title(test_code) or title
        repo = repo_override or self._warm_repo
        assert repo is not None  # set by _ensure_warm when prep succeeded
        host_path: str | None = None
        if self.tests_file:
            host_path = os.path.join(repo, self.tests_file)
        target_path: str | None = None
        if getattr(finding, "source_file", None):
            cand = os.path.join(repo, finding.source_file)
            if os.path.isfile(cand):
                target_path = cand
        injected, err = self.harness.inject(
            self._pristine_tests or "", test_code,
            host_path=host_path, target_path=target_path)
        if injected is None:
            finding.status = "broken_test"
            finding.observed = err
            return finding
        tpath = os.path.join(repo, self.tests_file)
        try:
            # write pristine + THIS finding's test (clean start every time)
            with open(tpath, "w", encoding="utf-8") as f:
                f.write(injected)
            out = self._fresh_result_path(repo)
            # Bind the direct-runner binary to THIS repo (a confirm lane's
            # private copy has its own node_modules/.bin/vitest). Non-direct
            # profiles keep their pnpm-exec test_base unchanged.
            run_profile = self.profile
            if getattr(self, "_direct_runner_active", False):
                run_profile = replace(
                    self.profile, test_base=self._direct_test_base(repo))
            try:
                proc = self._run(
                    self.harness.serial_command(run_profile, self._cmd_tests_file,
                                                serial_title, out,
                                                is_parameterized=parameterized),
                    self._workdir(repo),
                )
            except subprocess.TimeoutExpired:
                finding.status = "timed_out"
                finding.observed = (
                    f"did not finish within {self.timeout}s (subprocess limit)"
                )
                return finding
            finding.status, finding.observed = self.harness.read_verdict(out)
            if finding.observed == "test run produced no JSON output":
                # The runner died before reporting; its last words are the
                # only diagnostic there is (cause-first: never suppress the
                # stream holding the cause). Attach the tail so this reads
                # as a cause, not a shrug.
                tail = _runner_tail(proc)
                if tail:
                    finding.observed += "; runner said: " + tail
            return finding
        finally:
            # restore pristine so the base is clean for the next finding/run
            if self._pristine_tests is not None:
                with open(tpath, "w", encoding="utf-8") as f:
                    f.write(self._pristine_tests)

    # -- verdict brain back-compat -------------------------------------------
    # The classification and parsing bodies moved into VitestHarness; these
    # staticmethods stay because tests (and the determinism harness) pin the
    # names, and because they document that the DEFAULT gate is vitest.

    @staticmethod
    def _classify_failure(fm: str) -> tuple[str, str]:
        return _VITEST.classify_failure(fm)

    @staticmethod
    def _read(out: str) -> tuple[str, str]:
        return _VITEST.read_verdict(out)

    # -- batched gate ---------------------------------------------------------

    _MARK = "___ab{i}___"
    _MARK_PREFIX = "___ab"
    # anything in a PROPOSAL that looks like one of our marks. Attribution
    # matches marks by substring in the executed test's title, so a proposal
    # whose own title carries a lookalike (___ab0___) would hijack finding
    # 0's verdict. Stripping the pattern before the gate adds its own mark
    # guarantees the only mark in any executed title is gate-injected.
    _MARK_RE = re.compile(r"___ab\d+___")

    def _classify_batch(self, findings: list[ReviewFinding]) -> set[int]:
        """Inject every finding's test at once (uniquely marked titles), run
        the suite ONCE filtered to the mark, attribute results per finding.

        Returns the indexes it could NOT confidently attribute — the caller
        re-runs those through the proven serial path. Batch is an
        optimization layer; serial stays the verdict authority for anything
        ambiguous. A defective proposal that breaks collection of the whole
        file therefore poisons nothing: everyone falls back.
        """
        pending = {
            i: f for i, f in enumerate(findings)
            if not f.covered_by_existing
        }
        if not pending:
            return set()
        self._ensure_warm()
        if self._prep_error:
            for f in pending.values():
                f.status = "broken_test"
                f.observed = self._prep_error
            return set()

        content = self._pristine_tests or ""
        batch_host_path: str | None = None
        if self._warm_repo and self.tests_file:
            batch_host_path = os.path.join(self._warm_repo, self.tests_file)
        marked: dict[int, str] = {}
        for i, f in pending.items():
            title = self.harness.test_title(f.test_code or "")
            if not title:
                continue  # serial path will report it properly
            mark = self._MARK.format(i=i)
            # The proposal is de-marked first (see _MARK_RE) so it cannot
            # smuggle another finding's mark into its own title; the harness
            # then stamps the gate's own mark into the test opener.
            code = self.harness.mark_title(
                self._MARK_RE.sub("", f.test_code or ""), mark)
            if code is None:
                continue
            btgt: str | None = None
            if self._warm_repo and getattr(f, "source_file", None):
                bc = os.path.join(self._warm_repo, f.source_file)
                if os.path.isfile(bc):
                    btgt = bc
            injected, _err = self.harness.inject(
                content, code, host_path=batch_host_path, target_path=btgt)
            if injected is None:
                continue
            content, marked[i] = injected, mark
        if not marked:
            return set(pending)

        repo = self._warm_repo
        assert repo is not None  # set by _ensure_warm when prep succeeded
        tpath = os.path.join(repo, self.tests_file)
        try:
            with open(tpath, "w", encoding="utf-8") as fh:
                fh.write(content)
            out = self._fresh_result_path(repo)
            try:
                self._run(
                    self.harness.batch_command(self.profile, self._cmd_tests_file,
                                               self._MARK_PREFIX, out),
                    self._workdir(repo),
                )
            except subprocess.TimeoutExpired:
                # the BATCH hit the subprocess limit — which test hung is
                # unknown, so nobody gets a batched verdict. Serial decides.
                return set(pending)
            attributed = self._attribute(out, marked, pending)
            if not attributed and marked:
                # The scoped run collected NOTHING (e.g. a repo whose vitest
                # `include` filters the positional file to zero — "no test
                # files found"). Retry UNSCOPED so a filtering config never
                # turns the whole batch into false broken_tests. Slower (loads
                # the suite) but correct; studio and most repos never reach
                # this because the scoped run collects normally.
                # (sig EDGEVERDICT_SCOPED_COMMAND_V1)
                out2 = self._fresh_result_path(repo)
                try:
                    self._run(
                        self.harness.batch_command(
                            self.profile, self._cmd_tests_file,
                            self._MARK_PREFIX, out2, scoped=False),
                        self._workdir(repo),
                    )
                except subprocess.TimeoutExpired:
                    return set(pending)
                attributed = self._attribute(out2, marked, pending)
            return set(pending) - attributed
        finally:
            if self._pristine_tests is not None:
                with open(tpath, "w", encoding="utf-8") as fh:
                    fh.write(self._pristine_tests)

    def _attribute(
        self,
        out: str,
        marked: dict[int, str],
        pending: dict[int, ReviewFinding],
    ) -> set[int]:
        """Map batched results back to findings. Only a finding whose marked
        test demonstrably RAN gets a verdict here; everything else is left
        for serial. Verdict logic is the same shared brain as serial (the
        harness's classify_failure)."""
        results = self.harness.read_batch(out)
        if results is None:
            return set()
        # collect every executed test whose title carries one of our marks
        per: dict[int, list] = {}
        for r in results:
            for i, mark in marked.items():
                if mark in r.title:
                    per.setdefault(i, []).append(r)
        done: set[int] = set()
        for i, rs in per.items():
            f = pending[i]
            gap = timeout = load = None
            ran = 0
            for r in rs:
                if r.status in ("passed", "failed"):
                    ran += 1
                if r.status == "failed":
                    kind, first = self.harness.classify_failure(r.failure)
                    if kind == "timeout":
                        timeout = first
                    elif kind == "assertion":
                        gap = first
                    else:
                        load = first
            if not ran:
                continue  # never ran -> serial decides
            if gap:
                f.status, f.observed = "confirmed_gap", gap
            elif timeout:
                f.status, f.observed = "timed_out", timeout
            elif load:
                f.status, f.observed = "broken_test", load
            else:
                f.status, f.observed = (
                    "handled", "test passed — the tool already does this"
                )
            done.add(i)
        return done


    def _confirm_batch_gaps(self, review: "ReviewRun",
                            leftover: set[int]) -> None:
        """A confirmed_gap is a CLAIM, and batch mode can manufacture false
        ones: all proposals share one test file, so module-level state
        (persisted stores, singletons) leaks between tests and turns
        pollution into failures. The supabase studio run made the cost
        concrete -- 12 batch "gaps", 10 of them artifacts of a shared
        valtio+localStorage store, 2 real.

        So a gap found by BATCH must survive ISOLATION before keeping the
        label: each is re-gated through the serial path (its own file, its
        own run). Serial failure -> the gap stands, serially confirmed, with
        the cleaner isolated observed. Serial pass -> the batch failure was
        an artifact; the honest verdict is handled, and the note says so.
        Gaps that already CAME from the serial fallback are already
        isolated and are not re-run. Cost scales with gaps, which are rare
        on healthy runs. EDGEVERDICT_SERIAL_CONFIRM=0 opts out.
        """
        if os.environ.get("EDGEVERDICT_SERIAL_CONFIRM", "1").strip() == "0":
            return
        idxs = [i for i, f in enumerate(review.findings)
                if f.status == "confirmed_gap" and i not in leftover]
        if not idxs:
            return
        t0 = time.monotonic()
        batch_obs = {i: review.findings[i].observed for i in idxs}
        # v12 (sig EDGEVERDICT_PARALLEL_CONFIRM_V1): re-gate the gaps in
        # PARALLEL. Each lane gets a PRIVATE copy of the warm repo — its own
        # tests file, its own result file — so isolation stays airtight while
        # the wall-clock cost stops scaling linearly with gap count (the
        # Aug 10 live run spent 8355s re-gating 12 gaps serially). Workers
        # come from EDGEVERDICT_CONFIRM_WORKERS (default 4). ANY doubt —
        # one gap, workers=1, no warm repo, or copies failing — falls back
        # to the proven sequential path; parallel is an optimization layer,
        # sequential stays the authority.
        lanes = 0
        # Parallel lanes each COPY the whole warm repo (node_modules and all)
        # before running — measured at ~236s of copytree on supabase/studio to
        # run four 0.2s tests. That trade made sense when a re-gate was ~79s
        # (pnpm exec); with the direct runner each re-gate is ~0.2s, so
        # sequential in the shared warm repo is ~0.8s total and copies nothing.
        # Sequential is now the DEFAULT; parallel copies are opt-in for the
        # rare case of genuinely slow per-run environments.
        # (sig EDGEVERDICT_CONFIRM_INPLACE_V1)
        if (self._confirm_parallel_enabled()
                and self._confirm_workers() > 1 and len(idxs) > 1
                and self._warm_repo is not None):
            try:
                lanes = self._regate_parallel(review, idxs)
            except (OSError, shutil.Error):
                lanes = 0  # lane copies failed -> sequential fallback
        if not lanes:
            for i in idxs:
                self.classify(review.findings[i])
        survived = 0
        artifacts = 0
        for i in idxs:
            f = review.findings[i]
            batch_observed = batch_obs[i]
            if f.status == "confirmed_gap":
                survived += 1
                f.observed = (f.observed or batch_observed or "")
                f.observed += " [serially confirmed in isolation]"
            else:
                artifacts += 1
                f.observed = (
                    "passed in isolation; the batch failure was a shared-"
                    "state artifact (all proposals run in one file), not a "
                    "behavior of the change. Batch had observed: "
                    + (batch_observed or "(none)"))
        self.log(
            f"  serial confirmation {time.monotonic() - t0:.1f}s: "
            f"{len(idxs)} batch gap(s) re-gated in isolation — "
            f"{survived} confirmed, {artifacts} batch artifact(s)"
            + (f" [{lanes} parallel lane(s)]" if lanes else ""))

    @staticmethod
    def _confirm_parallel_enabled() -> bool:
        """Parallel confirm lanes COPY the warm repo per lane — only worth it
        when a single re-gate is slow. With the direct runner a re-gate is
        ~0.2s, so the copies (hundreds of seconds on a big monorepo) cost far
        more than they save; sequential in-place is the default. Set
        EDGEVERDICT_CONFIRM_PARALLEL=1 to re-enable the copy-per-lane path."""
        return os.environ.get("EDGEVERDICT_CONFIRM_PARALLEL", "").strip() in (
            "1", "true", "yes", "on")

    @staticmethod
    def _confirm_workers() -> int:
        """Parallel confirmation lane budget. EDGEVERDICT_CONFIRM_WORKERS,
        default 4; anything unparsable or < 1 means 1 (sequential)."""
        raw = os.environ.get("EDGEVERDICT_CONFIRM_WORKERS", "4").strip()
        try:
            return max(1, int(raw or "4"))
        except ValueError:
            return 1

    def _regate_parallel(self, review: ReviewRun, idxs: list[int]) -> int:
        """Re-gate the given findings across private warm-repo copies.
        Returns the lane count used. Raises OSError/shutil.Error ONLY
        before any classify has run (copy phase), so the caller's
        sequential fallback never double-gates. A worker exception marks
        just its finding for a sequential re-gate afterwards."""
        assert self._warm_repo is not None
        lanes = min(self._confirm_workers(), len(idxs))
        lane_roots: list[str] = []
        lane_repos: list[str] = []
        try:
            for _ in range(lanes):
                root = tempfile.mkdtemp(prefix="edgeverdict_confirm_")
                lane_roots.append(root)
                lr = os.path.join(root, "repo")
                shutil.copytree(self._warm_repo, lr, symlinks=True)
                lane_repos.append(lr)
        except (OSError, shutil.Error):
            for r in lane_roots:
                shutil.rmtree(r, ignore_errors=True)
            raise
        work = list(idxs)
        failed: list[int] = []
        lock = threading.Lock()

        def _lane(lane_repo: str) -> None:
            while True:
                with lock:
                    if not work:
                        return
                    i = work.pop()
                f = review.findings[i]
                try:
                    self.classify(f, repo_override=lane_repo)
                except Exception:  # noqa: BLE001 — one lane must not sink the rest
                    with lock:
                        failed.append(i)

        try:
            threads = [threading.Thread(target=_lane, args=(lr,), daemon=True)
                       for lr in lane_repos]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        finally:
            for r in lane_roots:
                shutil.rmtree(r, ignore_errors=True)
        for i in failed:  # sequential authority for anything a lane dropped
            self.classify(review.findings[i])
        return lanes

    def run(self, review: ReviewRun, batch: bool = True) -> ReviewRun:
        """Classify all findings against one warm base. With batch=True the
        gate runs ONE test invocation and serial-fallbacks anything it could
        not confidently attribute; batch=False is the original per-finding
        path. Both produce identical verdicts — tests/test_gate_e2e.py
        asserts fingerprint equality between the two modes.
        """
        try:
            for f in review.findings:
                if f.covered_by_existing:
                    f.status = "skipped_covered"
            self._ensure_warm()
            if self._prep_error:
                review.env_error = self._prep_error
                for f in review.findings:
                    if not f.covered_by_existing:
                        f.status = "broken_test"
                        f.observed = "not executed: environment failure (see banner above)"
                return review
            if batch:
                t0 = time.monotonic()
                leftover = self._classify_batch(review.findings)
                t_batch = time.monotonic() - t0
                t0 = time.monotonic()
                for i in sorted(leftover):
                    self.classify(review.findings[i])
                t_serial = time.monotonic() - t0
                eligible = sum(
                    1 for f in review.findings if not f.covered_by_existing
                )
                self.log(
                    f"  gate: batch {t_batch:.1f}s"
                    + (
                        f" + serial fallback {t_serial:.1f}s for "
                        f"{len(leftover)}/{eligible} finding(s)"
                        if leftover else f", 0/{eligible} fell back"
                    )
                )
                self._confirm_batch_gaps(review, leftover)
            else:
                for f in review.findings:
                    self.classify(f)
            return review
        finally:
            # v13: persist this run's vite transforms before any teardown —
            # runs with reuse_warm re-stow too, so the entry stays current.
            self._restow_vite_cache()
            if not self.reuse_warm:
                self.close()
