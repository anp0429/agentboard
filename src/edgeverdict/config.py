# EDGEVERDICT_WORKSPACE_FILTER_V1
"""Config loading, profile auto-detection, and pre-flight — the difference
between "read the source to use it" and "point it at your branch".

Nobody should hand-build a RepoProfile or edit a Python constant to review a
change. `.edgeverdict.toml` at the repo root holds the repo's stable setup
once; everything else is flags. What can be inferred (the profile, from the
lockfile) is inferred. What must be true (refs resolve, files exist, keys
present) is checked up front, so a misconfigured run fails in two seconds
with a fix hint instead of five minutes into a token-spending pipeline.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field

from .verifiers.vitest_verifier import RepoProfile

CONFIG_NAME = ".edgeverdict.toml"


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
    an untracked .edgeverdict.toml trips pre-push hooks and gets swept up by
    `git add -A`. The same settings can live in the user config dir instead,
    keyed by the repo directory's basename:
    <XDG_CONFIG_HOME or ~/.config>/edgeverdict/repos/<name>.toml
    """
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    name = os.path.basename(os.path.abspath(repo_root)) or "default"
    return os.path.join(base, "edgeverdict", "repos", name + ".toml")


def load_config(repo_root: str, config_path: str = "") -> Config:
    """Resolve and load config. Precedence: explicit --config path, then
    .edgeverdict.toml in the repo, then the per-repo user config file."""
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


def detect_profile_kind(repo_root: str, tests_file: str = "") -> str:
    """Infer the toolchain from the repo's own marker files, letting the
    file that is about to EXECUTE break polyglot ties. The old rule ("a JS
    lockfile wins over Python markers; a JS repo with a stray pyproject
    does not happen") met its counterexample in posthog/posthog: a true
    polyglot monorepo with pnpm-lock.yaml AND pyproject.toml at the root,
    where the review target and host were Python. JS-wins ran pnpm install
    six times (frontend workspaces, node-rdkafka builds) while the python
    env stayed empty, and every python execution broke on missing deps —
    a whole run of environmental false "broken". A .py tests_file names
    pytest as the harness, so it names the installer too. No hint, or a
    JS-suffixed hint, preserves the legacy order exactly. pnpm wins if
    both JS lockfiles exist (pnpm repos often keep a stray package-lock
    around)."""
    if tests_file.endswith(".py") and _python_markers(repo_root):
        return "pytest"
    if os.path.isfile(os.path.join(repo_root, "pnpm-lock.yaml")):
        return "pnpm-vitest"
    if os.path.isfile(os.path.join(repo_root, "package-lock.json")):
        return "npm-vitest"
    if os.path.isfile(os.path.join(repo_root, "yarn.lock")):
        return "pnpm-vitest"  # closest preset; user can override in config
    # Python: pytest's own config file, any pyproject, or a setup.cfg that
    # carries a pytest section. Deliberately after the lockfile checks.
    if _python_markers(repo_root):
        return "pytest"
    return ""


def _python_markers(repo_root: str) -> bool:
    """The python-toolchain evidence detect_profile_kind trusts."""
    if os.path.isfile(os.path.join(repo_root, "pytest.ini")):
        return True
    if os.path.isfile(os.path.join(repo_root, "pyproject.toml")):
        return True
    setup_cfg = os.path.join(repo_root, "setup.cfg")
    if os.path.isfile(setup_cfg):
        try:
            text = open(setup_cfg, encoding="utf-8").read()
        except OSError:
            text = ""
        # both spellings seen in the wild: [tool:pytest] is the documented
        # one; [tool.pytest] appears in files converted from pyproject.
        if "[tool:pytest]" in text or "[tool.pytest" in text:
            return True
    return False


LOCKFILES = ("pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lockb")


def detect_project_dir(repo_root: str, target: str) -> str:
    """Repo-relative directory the JS toolchain should run in.

    The git repo root is not always the JS project root: a package can be
    nested inside a larger repo (edgeverdict's own Python repo carries a JS
    fixture under src/edgeverdict/demo/target/). Installing at the repo root
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
    if target.endswith(".py"):
        # Python monorepos install per-package at the nearest pyproject
        # (setup.cfg/setup.py for older layouts). A JS lockfile at the
        # monorepo root must NOT win for a Python target -- mixed repos
        # like deepagents carry both toolchains.
        walk = d
        while True:
            for marker in ("pyproject.toml", "setup.cfg", "setup.py"):
                if os.path.isfile(os.path.join(walk, marker)):
                    rel = os.path.relpath(walk, root)
                    return "." if rel == os.curdir else rel
            if os.path.normpath(walk) == os.path.normpath(root) or len(walk) <= len(root):
                break
            parent = os.path.dirname(walk)
            if parent == walk:
                break
            walk = parent
        return "."
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


def detect_workspace_filter(scan_root: str, tests_file: str) -> tuple[str | None, str | None]:
    """(package_name, package_rel_dir) for the pnpm-workspace package that
    contains tests_file — ONLY when root exec would fail: the workspace root
    cannot resolve vitest (not in its own deps/devDeps) but the target's
    package can. First real-world vote: supabase/mcp keeps vitest solely in
    packages/*, so `pnpm exec vitest` at the root dies with
    ERR_PNPM_RECURSIVE_EXEC_FIRST_FAIL. Deliberately a no-op on workspaces
    that pass today via root exec (zod pins vitest at the root), so gauntlet
    behavior cannot drift. Returns (None, None) whenever unsure — the
    existing root-exec path is the fallback, and it fails loudly."""
    import json
    import posixpath

    def _has_vitest(pkg_json_path: str) -> bool:
        try:
            with open(pkg_json_path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return False
        deps = {**d.get("dependencies", {}), **d.get("devDependencies", {})}
        return "vitest" in deps

    if not os.path.isfile(os.path.join(scan_root, "pnpm-workspace.yaml")):
        return None, None
    if _has_vitest(os.path.join(scan_root, "package.json")):
        return None, None
    d = posixpath.dirname(tests_file)
    while d not in ("", ".", "/"):
        pkg_path = os.path.join(scan_root, d, "package.json")
        if os.path.isfile(pkg_path):
            try:
                with open(pkg_path, encoding="utf-8") as fh:
                    pkg = json.load(fh)
            except (OSError, ValueError):
                return None, None
            name = pkg.get("name")
            if name and _has_vitest(pkg_path):
                return name, d
            return None, None
        d = posixpath.dirname(d)
    return None, None


def _docker_backend_selected() -> bool:
    """Mirror backend_from_env's choice without constructing a backend.

    Constructing DockerBackend probes the daemon and raises when absent;
    the profile only needs to know which way the environment points. The
    default here must stay in lockstep with execution.backend_from_env:
    docker unless EDGEVERDICT_EXECUTION_BACKEND says local.
    """
    return os.environ.get("EDGEVERDICT_EXECUTION_BACKEND", "docker").strip().lower() != "local"


def _host_file_install_supplement(scan_root: str, tests_file: str) -> list[str]:
    """Distributions the HOST tests file needs that declared deps may miss.

    The vitest lane never has this problem: npm install reads the complete
    devDependencies. Python repos routinely under-declare test deps
    (posthog-python declares a single dev dependency), so the host file's
    own imports — and every conftest.py on its path, which load at
    collect time — are an install input. Rules keep it conservative:
    module-level imports only; stdlib excluded; modules that exist in the
    repo tree excluded (they're local code, not distributions); relative
    imports excluded; PEP 503 makes underscore names pip-installable
    as-is, so only classic name-diverging cases are mapped. This runs
    only for the sandboxed lane, where installs land in a filesystem
    that evaporates with the run.
    """
    import ast
    import sys

    mapping = {
        "yaml": "PyYAML", "PIL": "Pillow", "cv2": "opencv-python",
        "sklearn": "scikit-learn", "dotenv": "python-dotenv",
        "attr": "attrs", "dateutil": "python-dateutil", "bs4": "beautifulsoup4",
    }
    stdlib = set(getattr(sys, "stdlib_module_names", ()))

    files: list[str] = []
    tf_abs = os.path.join(scan_root, tests_file) if not os.path.isabs(tests_file) else tests_file
    if os.path.isfile(tf_abs):
        files.append(tf_abs)
    walk = os.path.dirname(tf_abs)
    root_norm = os.path.normpath(scan_root)
    while True:
        c = os.path.join(walk, "conftest.py")
        if os.path.isfile(c):
            files.append(c)
        if os.path.normpath(walk) == root_norm or len(walk) <= len(root_norm):
            break
        parent = os.path.dirname(walk)
        if parent == walk:
            break
        walk = parent

    def _optional_import_lines(tree) -> set[int]:
        """Line numbers of imports inside try/except ImportError blocks —
        optional by construction (py2 fallbacks like `from Queue import
        Queue` in posthog-python's tests broke the whole install when the
        supplement tried to pip install the py2 stdlib name)."""
        out: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            names = []
            for h in node.handlers:
                t = h.type
                subs: list = []
                if isinstance(t, ast.Tuple):
                    subs = list(t.elts)
                elif t is not None:
                    subs = [t]
                for sub in subs:
                    if isinstance(sub, ast.Name):
                        names.append(sub.id)
            if not any(n in ("ImportError", "ModuleNotFoundError", "Exception")
                       for n in names):
                continue
            for part in list(node.body) + [x for h in node.handlers
                                           for x in h.body]:
                for sub in ast.walk(part):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        out.add(sub.lineno)
        return out

    tops: list[str] = []
    for f in files:
        try:
            tree = ast.parse(open(f, encoding="utf-8").read())
        except Exception:
            continue
        optional = _optional_import_lines(tree)
        for node in ast.walk(tree):
            if getattr(node, "lineno", None) in optional:
                continue
            if isinstance(node, ast.Import):
                tops.extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                tops.append(node.module.split(".")[0])

    out: list[str] = []
    seen: set[str] = set()
    lowered_stdlib = {m.lower() for m in stdlib}
    for name in tops:
        if not name or name in seen or name.lower() in lowered_stdlib:
            continue
        seen.add(name)
        # local repo code, not a distribution
        if (os.path.isdir(os.path.join(scan_root, name))
                or os.path.isfile(os.path.join(scan_root, name + ".py"))
                or os.path.isdir(os.path.join(scan_root, "src", name))
                or os.path.isdir(os.path.join(scan_root, "tests", name))):
            continue
        out.append(mapping.get(name, name))
    return sorted(out)[:40]


def _python_sandbox_install(scan_root: str, tests_file: str = "") -> list[str]:
    """Editable-install command for the sandboxed python lane.

    Installs the project at the detected project dir, adding a declared
    test extra when pyproject names one (test > tests > dev). The command
    says `python`, which is the container's interpreter; this command is
    only emitted when the docker backend is selected.
    """
    spec = "."
    group_reqs: list[str] = []
    pyproject = os.path.join(scan_root, "pyproject.toml")
    if os.path.isfile(pyproject):
        try:
            import tomllib
            with open(pyproject, "rb") as fh:
                data = tomllib.load(fh)
            extras = data.get("project", {}).get("optional-dependencies", {})
            for name in ("test", "tests", "dev"):
                if name in extras:
                    spec = f".[{name}]"
                    break
            else:
                # PEP 735 dependency-groups (deepagents-style). pip's
                # --group flag is too new to assume in the image, so pass
                # the group's packages as explicit requirements instead.
                # Entries are strings or {include-group = "..."} dicts;
                # resolve includes one level of nesting at a time.
                groups = data.get("dependency-groups", {})

                def _resolve(name: str, seen: frozenset[str]) -> list[str]:
                    if name in seen:
                        return []
                    out: list[str] = []
                    for entry in groups.get(name, []):
                        if isinstance(entry, str):
                            out.append(entry)
                        elif isinstance(entry, dict) and "include-group" in entry:
                            out.extend(_resolve(entry["include-group"],
                                                seen | {name}))
                    return out

                for name in ("test", "tests", "dev"):
                    if name in groups:
                        group_reqs = _resolve(name, frozenset())
                        break
        except Exception:
            spec, group_reqs = ".", []
    # --user: the container root filesystem is read-only under a non-root
    # user, so site-packages is unwritable; the DockerBackend points
    # PYTHONUSERBASE inside the bind-mounted warm copy, the one surface
    # that is both writable and executable (noexec /tmp would break
    # compiled wheels). --no-cache-dir: there is no writable home for a
    # pip cache. Harmless under any backend; only emitted for docker.
    supplement = (_host_file_install_supplement(scan_root, tests_file)
                  if tests_file else [])

    # Editable install requires a build backend. A repo with no
    # [build-system] table is not a buildable distribution -- pip falls
    # back to legacy setuptools, which cannot package a flat multi-package
    # tree and dies in seconds ("Failed to build ... when getting
    # requirements to build editable"). posthog/posthog is the vote: a
    # uv-managed project ([project] + uv.lock, NO [build-system]) meant to
    # run in-place with the repo root on the path, never pip-installed as a
    # package. For that shape, install the DEPENDENCIES only and skip the
    # -e . build: the container workdir is already the repo root, so
    # `posthog` imports straight from the tree once its deps are present.
    # Repos WITH a build-system keep the editable install unchanged.
    if not _has_build_system(scan_root):
        deps = _declared_dependencies(scan_root)
        return ["python", "-m", "pip", "install", "--quiet", "--user",
                "--no-cache-dir", *deps, *group_reqs, *supplement]

    return ["python", "-m", "pip", "install", "--quiet", "--user",
            "--no-cache-dir", "-e", spec, *group_reqs, *supplement]


def _has_build_system(scan_root: str) -> bool:
    """True when pyproject declares a build backend -- the precondition for
    an editable install. Absent table => not a buildable distribution."""
    pyproject = os.path.join(scan_root, "pyproject.toml")
    if not os.path.isfile(pyproject):
        # setup.py/setup.cfg projects are setuptools-buildable by definition.
        return (os.path.isfile(os.path.join(scan_root, "setup.py"))
                or os.path.isfile(os.path.join(scan_root, "setup.cfg")))
    try:
        import tomllib
        with open(pyproject, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return False
    return bool(data.get("build-system", {}).get("build-backend")
                or data.get("build-system", {}).get("requires"))


def _declared_dependencies(scan_root: str) -> list[str]:
    """[project.dependencies] as install args, for the deps-only lane used
    when a repo has no build-system. Test/dev extras and PEP 735 groups are
    added by the caller; the host-file supplement covers third-party
    imports the manifest may miss. Returns [] on any parse trouble -- the
    supplement and declared groups still install, and smoke names whatever
    is genuinely missing."""
    pyproject = os.path.join(scan_root, "pyproject.toml")
    if not os.path.isfile(pyproject):
        return []
    try:
        import tomllib
        with open(pyproject, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return []
    deps = data.get("project", {}).get("dependencies", [])
    return [d for d in deps if isinstance(d, str)]


def build_profile(repo_root: str, cfg: Config, tests_file: str,
                  project_dir: str = ".") -> RepoProfile:
    scan_root = os.path.normpath(os.path.join(repo_root, project_dir))
    kind = cfg.profile_kind or detect_profile_kind(scan_root, tests_file)
    if kind == "pytest":
        # Python profile. Under the LOCAL backend there is no install step:
        # the gate runs in the environment the operator already provisioned,
        # because "pip install a repo's deps into the operator's env" has
        # real blast radius. The docker sandbox dissolves exactly that
        # objection -- an install inside the container touches a filesystem
        # that evaporates with the run -- so when the docker backend is
        # selected the profile installs the project (with its test extra
        # when one is declared) into the container. The DockerBackend maps
        # sys.executable to the container's `python`. No build step either
        # way. Smoke = collect the tests file: proves pytest starts, the
        # file parses, and its imports resolve before any finding is judged.
        import sys
        test_base = [sys.executable, "-m", "pytest"]
        prof = RepoProfile(
            name=os.path.basename(repo_root.rstrip("/")),
            install_cmd=(_python_sandbox_install(
                             scan_root,
                             posixpath.relpath(
                                 tests_file.replace(os.sep, "/"),
                                 (project_dir or ".").replace(os.sep, "/")))
                         if _docker_backend_selected() else []),
            install_fallback_cmd=(_python_sandbox_install(scan_root)
                                  if _docker_backend_selected() else None),
            test_base=test_base,
            build_cmd=None,
            env={"CI": "true"},
            # The toolchain executes with cwd = project_dir; in a python
            # monorepo the repo-relative tests path would double the prefix
            # (libs/pkg/libs/pkg/...) and collect nothing. Root projects
            # are unchanged: relpath against "." is the identity.
            smoke_cmd=test_base + [
                "--collect-only", "-q",
                posixpath.relpath(tests_file.replace(os.sep, "/"),
                                  (project_dir or ".").replace(os.sep, "/")),
            ],
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
        ws_filter, ws_pkg_dir = (None, None)
        if cfg.filter is None:
            ws_filter, ws_pkg_dir = detect_workspace_filter(scan_root, tests_file)
        prof = RepoProfile.pnpm_vitest(
            os.path.basename(repo_root.rstrip("/")),
            filter=cfg.filter or ws_filter, project=project, build=cfg.build,
            pnpm_version=detect_pnpm_version(scan_root),
            # honor the repo's pinned dependency set when it ships one; the
            # verifier retries unfrozen (with a note) if the pin is stale
            frozen=os.path.isfile(os.path.join(scan_root, "pnpm-lock.yaml")),
            # the package dir the --filter execs inside, so the verifier can
            # make the command's test path package-relative (else it doubles
            # the prefix and vitest scans the whole workspace).
            pkg_dir=ws_pkg_dir or "",
        )
    if kind in ("npm-vitest", "pnpm-vitest") or prof.kind == "vitest":
        # catch 5b: aim the smoke probe at the tests file's own directory,
        # in the suite's own flavor — a root-level probe is disowned by
        # monorepo project includes ("No test files found", exit 1)
        from .verifiers.vitest_verifier import smoke_probe_for
        rel, content = smoke_probe_for(tests_file)
        prof.smoke_probe = (rel, content)
        if prof.smoke_cmd:
            cmd_rel = rel
            ws_dir = locals().get("ws_pkg_dir")
            if ws_dir:
                # --filter execs inside the package dir, so the path handed
                # to vitest must be package-relative; the probe FILE keeps
                # its repo-relative path (write base is the repo root).
                cmd_rel = posixpath.relpath(rel, ws_dir)
            prof.smoke_cmd = prof.smoke_cmd[:-1] + [cmd_rel]
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
    # .edgeverdict.toml, local scratch) doesn't change what HEAD reviews and
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


def working_tree_dirty(repo_root: str) -> bool:
    """True when tracked files have uncommitted changes. prove's mode cue:
    dirty means review the working tree (what the author is looking at),
    clean means review the branch against its fork point."""
    r = subprocess.run(
        ["git", "-C", repo_root, "status", "--porcelain", "--untracked-files=no"],
        capture_output=True, text=True,
    )
    return bool(r.stdout.strip())


_PROVE_SOURCE_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".py")
_PROVE_TEST_DIRS = {"test", "tests", "__tests__"}


def _looks_like_test_file(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    if any(p in _PROVE_TEST_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    stem = name.rsplit(".", 1)[0]
    return (
        ".test." in name
        or ".spec." in name
        or name.startswith("test_")
        or stem.endswith("_test")
        or name == "conftest.py"
    )


def targets_from_diff(
    repo_root: str, base: str, head: str = "", worktree: bool = False,
) -> list[str]:
    """Changed source files with tests and deletions excluded: the targets
    `prove` reviews when the user typed nothing. Worktree mode diffs the
    working tree against base; ref mode diffs base...head (the branch's own
    changes, not what base did since). Sorted, so a run's target list is
    deterministic regardless of git's ordering."""
    cmd = ["git", "-C", repo_root, "diff", "--name-only", "--diff-filter=d"]
    if worktree:
        cmd.append(base)
    else:
        cmd.append(f"{base}...{head}" if head else base)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        rel = line.strip()
        if not rel or not rel.endswith(_PROVE_SOURCE_EXTS):
            continue
        if re.search(r"\.d\.(ts|mts|cts)$", rel):
            # type DECLARATIONS have no runtime behavior to break; a
            # .d.cts target sent the gauntlet's defu run into a dead end
            continue
        if _looks_like_test_file(rel):
            continue
        out.append(rel)
    return sorted(out)


def untracked_source_files(repo_root: str) -> list[str]:
    """New source files git doesn't track yet. Worktree diffs cannot see
    them, so prove names them out loud instead of reviewing half a change
    in silence; `git add -N <file>` makes them visible to the diff."""
    r = subprocess.run(
        ["git", "-C", repo_root, "ls-files", "--others", "--exclude-standard"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        rel = line.strip()
        if rel and rel.endswith(_PROVE_SOURCE_EXTS)                 and not _looks_like_test_file(rel):
            out.append(rel)
    return sorted(out)
