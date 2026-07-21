"""The vitest facts of the review path: repo profiles and execution helpers.

RepoProfile exists because a Python repo runs with one command (`pytest`)
but a JS repo does not: the package manager, whether sibling packages must
be built first, the project to scope to, and required env all vary per
repo. Pretending those are universal would make the gate *lie* on the next
repo. So every such fact is named explicitly in a RepoProfile. Supabase is
just one profile; a flat single-package repo is another. The harness core
never changes.

Alongside the profiles live the execution helpers every runner shares:
credential scrubbing (scrubbed_env), the frozen-install fallback
(unfrozen_install), vitest JSON parsing, and error-tail formatting.

Gotchas encoded (each found by actually running it against supabase/mcp):
  - `CI=true` (vitest.setup.ts stats .env.local otherwise; whole suite fails).
  - build workspace deps BEFORE tests (tests import built sibling packages).
  - `vitest run`, never bare `vitest` (bare = watch mode = hangs forever).
  - scope to a hermetic project; e2e/integration need live creds.
  - parse the JSON reporter, never stdout.

The legacy loop-protocol verifier that drove these facts (VitestVerifier,
with baseline-delta acceptance) lives in
``agentboard.experimental.verifiers.vitest_verifier``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


# --- credential scrubbing ----------------------------------------------------

# Model-provider credentials the gate must never hand to executed code. The
# gate is deterministic by design — no LLM in the pass/fail path — so nothing
# it spawns (install, build, smoke, or the tests themselves, which include
# model-written code) has any legitimate use for these. Scrubbing here
# enforces that design mechanically: even in CI, where the review step holds
# OPENAI_API_KEY, the code under test runs without it. Deliberately narrow —
# only the model providers' keys — because install steps may legitimately
# need registry tokens.
_PROVIDER_CREDENTIALS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def scrubbed_env(profile_env: dict[str, str],
                 cache_root: str | None = None) -> dict[str, str]:
    """The subprocess environment for everything the gate executes:
    os.environ + the profile's env, minus model-provider credentials.

    cache_root, when given, points the package managers' caches inside the
    run's own temp area: npm reads npm_config_cache, pnpm reads its
    store-dir from the same npm-style env (npm_config_store_dir). The
    sandbox runs model-written install scripts and tests; those must not
    read or poison the user's shared ~/.npm cache and pnpm store, which
    every later run (and the user's own shell) trusts. A cold cache per
    run is the price of that isolation."""
    env = {**os.environ, **profile_env}
    for key in _PROVIDER_CREDENTIALS:
        env.pop(key, None)
    if cache_root:
        env["npm_config_cache"] = os.path.join(cache_root, "npm-cache")
        env["npm_config_store_dir"] = os.path.join(cache_root, "pnpm-store")
    return env


def unfrozen_install(install_cmd: list[str]) -> list[str] | None:
    """The retry command for a failed frozen install: same command with
    --frozen-lockfile swapped for --no-frozen-lockfile. None when the
    install was not frozen (nothing to fall back to). The fallback exists
    because a frozen install fails on a merely stale lockfile, and a stale
    lockfile must degrade the run (with a printed note), not kill it."""
    if "--frozen-lockfile" not in install_cmd:
        return None
    return ["--no-frozen-lockfile" if a == "--frozen-lockfile" else a
            for a in install_cmd]


# --- the explicit, per-repo facts the verifier cannot guess -----------------

@dataclass
class RepoProfile:
    """Everything repo-specific, named instead of assumed.

    ``test_base`` is the vitest invocation up to (and including) ``run`` plus any
    filters/projects. The verifier appends ``--reporter=json --outputFile=...``
    itself, so JSON output is centralised and cannot drift per profile.
    """

    name: str
    install_cmd: list[str]
    test_base: list[str]
    build_cmd: list[str] | None = None          # None => no build step
    env: dict[str, str] = field(default_factory=lambda: {"CI": "true"})
    # Repo-specific rules the reviewer must follow when WRITING tests (harness
    # setup sequences, required helpers, gotchas). Injected into the reviewer
    # prompt as data — the prompt itself stays repo-agnostic.
    harness_notes: str = ""
    # Cheap functional probe run once after install/build: proves the test
    # runner actually starts in the warm sandbox (binary present, config
    # loads, transforms work) BEFORE any finding is judged. Exit codes can be
    # version-flaky (a pnpm notice once benched an entire run as "install
    # failed"); a probe that RUNS the runner cannot be fooled by log noise.
    # None => skip.
    smoke_cmd: list[str] | None = None
    # Which framework harness the gate should drive this repo with
    # ("vitest" | "pytest"). A profile is repo facts, but the commands it
    # names imply a framework, and the gate needs that named too — see
    # harness.harness_for_profile. Default keeps every existing profile
    # (and every hand-built one in tests) on the vitest path.
    kind: str = "vitest"

    # ---- presets for the common cases --------------------------------------

    @classmethod
    def pnpm_vitest(
        cls,
        name: str,
        *,
        filter: str | None = None,
        project: str | None = None,
        build: bool = True,
        extra_test_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        pnpm_version: str = "9",
        frozen: bool = False,
    ) -> "RepoProfile":
        # Every pnpm invocation goes through an explicitly versioned pnpm via
        # npx — never the ambient corepack shim. The version is the repo's own
        # packageManager pin when it is modern (config.detect_pnpm_version),
        # else 9. History of this line, because it has broken in BOTH
        # directions: old pnpm (7.x) cannot run on Node 22 (ERR_INVALID_THIS),
        # which forced the pin to 9 — and then pnpm 10/11 repos (pathe) began
        # using pnpm-workspace.yaml as a plain config file with no `packages`
        # field, which pnpm 9 rejects as a broken workspace ("packages field
        # missing or empty"). The repo's own modern pin is the only version
        # its config is guaranteed to parse under.
        pnpm = ["npx", "-y", f"pnpm@{pnpm_version}"]
        test = list(pnpm)
        if filter:
            test += ["--filter", filter]
        test += ["exec", "vitest", "run"]
        if project:
            test += ["--project", project]
        test += extra_test_args or []
        # frozen=True (set by build_profile when pnpm-lock.yaml exists): the
        # sandbox installs exactly the dependency set the repo pins, instead
        # of --no-frozen-lockfile silently resolving whatever the registry
        # serves that day — verdicts must not drift with upstream releases.
        # A stale lockfile is not a dead end: the verifiers retry unfrozen
        # with a printed note (see unfrozen_install). No lockfile keeps the
        # permissive install, the only one that can work.
        return cls(
            name=name,
            install_cmd=pnpm + ["install",
                                "--frozen-lockfile" if frozen
                                else "--no-frozen-lockfile"],
            build_cmd=pnpm + ["-r", "build"] if build else None,
            test_base=test,
            env={"CI": "true", **(env or {})},
            smoke_cmd=test + ["--passWithNoTests", "-t", "___agentboard_env_probe___"],
        )

    @classmethod
    def npm_vitest(
        cls,
        name: str,
        *,
        project: str | None = None,
        build: bool = False,
        extra_test_args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> "RepoProfile":
        test = ["npx", "vitest", "run"]
        if project:
            test += ["--project", project]
        test += extra_test_args or []
        return cls(
            name=name,
            install_cmd=["npm", "ci"],
            build_cmd=["npm", "run", "build"] if build else None,
            test_base=test,
            env={"CI": "true", **(env or {})},
            smoke_cmd=test + ["--passWithNoTests", "-t", "___agentboard_env_probe___"],
        )


# the reference profile — supabase/mcp, derived empirically
SUPABASE_MCP = RepoProfile.pnpm_vitest(
    "supabase-mcp",
    filter="@supabase/mcp-server-supabase",
    project="unit",
    build=True,
)
# Harness rules the reviewer must follow when writing tests for THIS repo.
# These were previously hardcoded in the reviewer prompt; they are repo
# knowledge, so they live here and get injected as data.
SUPABASE_MCP.harness_notes = (
    "MANDATORY SETUP: the tool operates on a project that must be created "
    "first. Every test MUST reproduce, in order, the exact setup sequence used "
    "by the existing tests before calling the tool: create the organization, "
    "create the project, set the project status to active, then load schema "
    "via the project's db exec. Never call the tool with a project_id you did "
    "not create this way — doing so fails with \"Project not found\". Copy this "
    "setup verbatim from the existing tests and change only the schema you "
    "load and the assertions you make. Use the same helpers the existing tests "
    "use (setup/createOrganization/createProject/callTool)."
)


# --- helpers -----------------------------------------------------------------

def _tail(s: str, n: int = 300, head: int = 200) -> str:
    """First `head` chars + last `n` chars. Error messages lead with the
    cause (Failed to load url X) and end with the stack; keeping only the
    tail once cost a debugging session by hiding the cause mid-stack."""
    s = (s or "").strip()
    if len(s) <= head + n:
        return s
    return s[:head] + "\n  ...\n" + s[-n:]


def _parse_vitest_json(path: str) -> tuple[set[str], dict[str, str], str]:
    """vitest's json reporter is Jest-shaped:
        { testResults: [ { assertionResults: [ {ancestorTitles,title,status,failureMessages} ] } ] }
    Returns (failing_ids, id->message, infra_error).
    """
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except Exception as e:  # noqa: BLE001
        return set(), {}, f"could not parse test JSON: {e}"
    failing: set[str] = set()
    messages: dict[str, str] = {}
    for suite in data.get("testResults", []):
        for t in suite.get("assertionResults", []):
            test_id = " > ".join(t.get("ancestorTitles", []) + [t.get("title", "")])
            if t.get("status") == "failed":
                failing.add(test_id)
                msgs = t.get("failureMessages") or []
                messages[test_id] = (msgs[0] if msgs else "").strip()
    return failing, messages, ""

