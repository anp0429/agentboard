"""Hardened execution backends for EdgeVerdict.

The model-facing process stays on the host. Repository lifecycle commands and
model-generated tests are delegated to an explicit backend. The default is a
locked-down Docker container. Local execution is disabled unless the operator
sets EDGEVERDICT_ALLOW_UNSAFE_LOCAL=1.

This module intentionally returns subprocess.CompletedProcess and raises
subprocess.TimeoutExpired so the existing verifier can keep its deterministic
verdict semantics unchanged.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence


class ExecutionBackend(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]: ...

    def close(self) -> None: ...


class ExecutionConfigurationError(RuntimeError):
    """Raised when a requested execution backend is unavailable or unsafe."""


@dataclass(frozen=True)
class DockerLimits:
    cpus: str = "2"
    memory: str = "2g"
    pids: int = 256
    nofile: int = 1024
    file_size_bytes: int = 64 * 1024 * 1024
    # Install writes legitimately large vendored wheels (e.g.
    # claude-agent-sdk ships an ~85MB binary); the tight per-file cap
    # exists to bound runaway TEST writes, not install downloads, so the
    # install phase gets a generous ceiling while execution stays tight.
    install_file_size_bytes: int = 512 * 1024 * 1024
    # /tmp is a RAM-backed tmpfs, so this bounds MEMORY, not just disk. 512m
    # suits single-package installs. A whole-monorepo pnpm install (many
    # workspaces) writes far more than that into /tmp and hits ENOSPC. For a
    # TRUSTED monorepo you can raise it via EDGEVERDICT_TMPFS_SIZE (e.g. "8g");
    # the value is a docker --tmpfs size string (e.g. "512m", "4g"). The
    # default stays conservative because it caps RAM for untrusted repos.
    tmpfs_size: str = field(
        default_factory=lambda: os.environ.get(
            "EDGEVERDICT_TMPFS_SIZE", "512m"))
    max_output_bytes: int = 2 * 1024 * 1024


_SECRET_NAME = re.compile(
    r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION|PRIVATE)(?:_|$)",
    re.IGNORECASE,
)
_CLOUD_PREFIXES = (
    "AWS_",
    "AZURE_",
    "GOOGLE_",
    "GCP_",
    "GITHUB_",
    "GITLAB_",
    "CI_JOB_JWT",
    "SSH_",
)
_BASE_ENV_ALLOWLIST = {
    "CI",
    "LANG",
    "LC_ALL",
    "TZ",
    "NODE_ENV",
    "PYTHONPATH",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
    "FORCE_COLOR",
    "NO_COLOR",
    "TERM",
    "npm_config_cache",
    "npm_config_store_dir",
    "NPM_CONFIG_CACHE",
    "NPM_CONFIG_STORE_DIR",
    "PIP_CACHE_DIR",
    "PIP_DISABLE_PIP_VERSION_CHECK",
    "PIP_NO_INPUT",
    "PIP_NO_INDEX",
    "PIP_FIND_LINKS",
    "UV_CACHE_DIR",
    # scm fix: without this, filtered_environment strips the pretend
    # version scrubbed_env just set, and VCS-versioned installs fail in
    # the sandbox anyway. Not a secret shape; safe to pass.
    "SETUPTOOLS_SCM_PRETEND_VERSION",
}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _extra_allowed_names() -> set[str]:
    raw = os.environ.get("EDGEVERDICT_SANDBOX_ENV_ALLOWLIST", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return bool(_SECRET_NAME.search(name)) or upper.startswith(_CLOUD_PREFIXES)


def filtered_environment(env: Mapping[str, str]) -> dict[str, str]:
    """Return the minimal environment allowed into untrusted execution.

    Explicit allowlisting never overrides the hard secret-name deny rule. This
    prevents a typo in EDGEVERDICT_SANDBOX_ENV_ALLOWLIST from passing a token.
    A future credential broker should use a scoped proxy, not environment vars.
    """
    allowed = _BASE_ENV_ALLOWLIST | _extra_allowed_names()
    result: dict[str, str] = {}
    for name, value in env.items():
        if _is_secret_name(name):
            continue
        if name in allowed or name.startswith("EDGEVERDICT_SAFE_"):
            result[name] = str(value)
    result.setdefault("CI", "true")
    result.setdefault("HOME", "/tmp/edgeverdict-home")
    result.setdefault("TMPDIR", "/tmp")
    return result


def _warm_root(cwd: str) -> Path:
    current = Path(cwd).resolve()
    for candidate in (current, *current.parents):
        if candidate.name.startswith("edgeverdict_warm_"):
            return candidate
    return current


def _container_path(path: str, mount_root: Path) -> str:
    resolved = Path(path).resolve()
    try:
        rel = resolved.relative_to(mount_root)
    except ValueError as exc:
        raise ExecutionConfigurationError(
            f"execution cwd escapes the sandbox root: {resolved} is not under {mount_root}"
        ) from exc
    return "/edgeverdict" if str(rel) == "." else f"/edgeverdict/{rel.as_posix()}"


def _rewrite_mounted_paths(env: Mapping[str, str], mount_root: Path) -> dict[str, str]:
    """Rewrite host paths under the mount to their container equivalents.

    Both sides must be compared in RESOLVED form. `mount_root` arrives already
    resolved, but an env value is whatever the caller built, and on macOS the
    two differ: /var is a symlink to /private/var, so a warm root under
    /var/folders/... resolves to /private/var/folders/... . A raw string
    comparison then fails, the host path is forwarded to the container
    unchanged, and the command dies trying to create it -- npm reported
    `ENOENT: mkdir '/var/folders'` on a read-only root, which reads like a
    permissions problem and is actually an unrewritten path. Linux never sees
    this because /tmp is not a symlink.
    """
    rewritten: dict[str, str] = {}
    root = str(mount_root)
    for name, value in env.items():
        candidate = value
        if value.startswith(os.sep):
            try:
                candidate = str(Path(value).resolve())
            except OSError:
                candidate = value
        if candidate == root or candidate.startswith(root + os.sep):
            rewritten[name] = _container_path(candidate, mount_root)
        else:
            rewritten[name] = value
    return rewritten


def _map_host_interpreter(args: Sequence[str]) -> list[str]:
    """Map the host Python interpreter to the container's.

    Python profiles build commands with sys.executable, an absolute path
    into the operator's venv. That path lies outside the mount root, so
    path rewriting correctly leaves it alone -- and the container would
    exec an interpreter that does not exist inside it. The sandbox image
    ships `python` on PATH; substitute it for argv[0] only, and only when
    argv[0] is this process's interpreter (compared raw and resolved, for
    venv symlinks). Every other argv[0] -- npm, npx, node -- passes through
    untouched.
    """
    out = list(args)
    if not out:
        return out
    head = out[0]
    try:
        same = head == sys.executable or (
            os.path.isabs(head)
            and Path(head).resolve() == Path(sys.executable).resolve()
        )
    except OSError:
        same = head == sys.executable
    if same:
        out[0] = "python"
    return out


def _rewrite_arg_paths(args: Sequence[str], mount_root: Path) -> list[str]:
    """Rewrite host paths appearing in COMMAND ARGUMENTS, not just in env.

    A path crosses the host/container boundary in three places: the working
    directory, the environment, and the argument list. Rewriting only the first
    two leaves the third pointing at the host. vitest is invoked with
    `--outputFile=<warm root>/edgeverdict-finding-result.json`; unrewritten, it
    writes that JSON to a path that does not exist inside the container, the
    harness then reads nothing back on the host, and every finding reports
    "test run produced no JSON output" -- a silent, total loss of verdicts that
    looks like a reporter problem. The smoke probe hides it, because the smoke
    command carries no --outputFile.

    Handles a bare path token and the `--flag=/path` form. Comparison is done
    in resolved form for the same symlink reason as _rewrite_mounted_paths.
    """
    rewritten: list[str] = []
    for raw in args:
        arg = str(raw)
        prefix, sep, candidate = "", "", arg
        if not arg.startswith(os.sep) and "=" in arg:
            prefix, sep, candidate = arg.partition("=")
        if candidate.startswith(os.sep):
            try:
                resolved = str(Path(candidate).resolve())
            except OSError:
                resolved = candidate
            root = str(mount_root)
            if resolved == root or resolved.startswith(root + os.sep):
                inside = _container_path(resolved, mount_root)
                rewritten.append(f"{prefix}{sep}{inside}" if sep else inside)
                continue
        rewritten.append(arg)
    return rewritten


_PKG_MANAGERS = {"npm", "pnpm", "yarn", "bun", "pip", "pip3", "uv", "poetry"}
_NPX_FLAGS_WITH_VALUE = {"-p", "--package", "-c", "--call"}


def _unwrap_runner(args: Sequence[str]) -> list[str]:
    """Strip an `npx`/`pnpm dlx` wrapper to reveal the real command.

    edgeverdict does not invoke package managers directly. It routes the pinned
    version through npx, so the install command is
    `npx -y pnpm@9 install --frozen-lockfile`, not `pnpm install`. A detector
    that only looks at args[0] therefore misses EVERY pnpm repository -- which
    is most real targets -- and the install silently runs with --network none
    even when the operator asked for `install`. The install then fails and the
    whole run stops on an environment error that looks like a network problem.
    """
    rest = [str(a) for a in args]
    while rest and Path(rest[0]).name.lower() in {"npx", "dlx"}:
        rest = rest[1:]
        while rest and rest[0].startswith("-"):
            flag = rest[0]
            rest = rest[1:]
            if flag in _NPX_FLAGS_WITH_VALUE and rest:
                rest = rest[1:]
    return rest


def _looks_like_install(args: Sequence[str]) -> bool:
    rest = _unwrap_runner(args)
    if not rest:
        return False
    # a pinned manager arrives as `pnpm@9`; the version is not part of the name
    head = Path(rest[0]).name.lower().split("@")[0]
    words = [Path(str(x)).name.lower() for x in rest[:4]]
    joined = " ".join(str(x).lower() for x in rest[:8])
    return (
        head in _PKG_MANAGERS and "install" in joined
    ) or (
        len(words) >= 3 and words[1:3] == ["-m", "pip"] and "install" in joined
    )


class _TailBuffer:
    def __init__(self, limit: int):
        self.limit = max(1024, limit)
        self.parts: deque[bytes] = deque()
        self.size = 0
        self.truncated = False
        self.lock = threading.Lock()

    def append(self, data: bytes) -> None:
        if not data:
            return
        with self.lock:
            self.parts.append(data)
            self.size += len(data)
            while self.size > self.limit and self.parts:
                extra = self.size - self.limit
                first = self.parts[0]
                if len(first) <= extra:
                    self.parts.popleft()
                    self.size -= len(first)
                else:
                    self.parts[0] = first[extra:]
                    self.size -= extra
                self.truncated = True

    def text(self) -> str:
        with self.lock:
            body = b"".join(self.parts).decode("utf-8", errors="replace")
            if self.truncated:
                return "[earlier output truncated by EdgeVerdict sandbox]\n" + body
            return body


def _drain(pipe, buffer: _TailBuffer) -> None:
    try:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            buffer.append(chunk)
    finally:
        pipe.close()


class LocalBackend:
    """Legacy execution path, available only after an explicit unsafe opt-in.

    trusted_fixture=True skips the opt-in check and exists for exactly one
    caller: the demo, whose target ships inside this package and is trusted
    by construction. Reviewing any external repository must never pass it.
    """

    def __init__(self, *, trusted_fixture: bool = False) -> None:
        if not trusted_fixture and not _truthy(
            os.environ.get("EDGEVERDICT_ALLOW_UNSAFE_LOCAL")
        ):
            raise ExecutionConfigurationError(
                "local execution is disabled because repository code would run as your OS user; "
                "set EDGEVERDICT_EXECUTION_BACKEND=docker, or explicitly accept the risk with "
                "EDGEVERDICT_ALLOW_UNSAFE_LOCAL=1"
            )

    def run(self, args, *, cwd, env, timeout):
        return subprocess.run(
            list(args), cwd=cwd, env=dict(env), capture_output=True, text=True, timeout=timeout
        )

    def close(self) -> None:
        return None


class DockerBackend:
    """Run commands in a disposable, restricted Docker container.

    Network policy:
      none    - default; all commands are offline.
      install - network only for recognized package-install commands. This is
                NOT safe for hostile repositories because lifecycle/build hooks
                can exfiltrate during installation.
      all     - network for every command; intended only for trusted repos.
    """

    def __init__(
        self,
        *,
        image: str | None = None,
        network_policy: str | None = None,
        limits: DockerLimits | None = None,
        log: Callable[..., None] = print,
    ) -> None:
        self.image = image or os.environ.get(
            "EDGEVERDICT_SANDBOX_IMAGE", "edgeverdict-sandbox:latest"
        )
        self.network_policy = (
            network_policy or os.environ.get("EDGEVERDICT_SANDBOX_NETWORK", "none")
        ).strip().lower()
        if self.network_policy not in {"none", "install", "all"}:
            raise ExecutionConfigurationError(
                "EDGEVERDICT_SANDBOX_NETWORK must be one of: none, install, all"
            )
        self.limits = limits or DockerLimits(
            cpus=os.environ.get("EDGEVERDICT_SANDBOX_CPUS", "2"),
            memory=os.environ.get("EDGEVERDICT_SANDBOX_MEMORY", "2g"),
            pids=int(os.environ.get("EDGEVERDICT_SANDBOX_PIDS", "256")),
            max_output_bytes=int(
                os.environ.get("EDGEVERDICT_SANDBOX_MAX_OUTPUT", str(2 * 1024 * 1024))
            ),
        )
        self.log = log
        docker = os.environ.get("EDGEVERDICT_DOCKER_BIN", "docker")
        resolved = shutil.which(docker)
        if not resolved:
            raise ExecutionConfigurationError(
                "Docker is required for the default hardened backend. Run `edgeverdict sandbox-build` (the Dockerfile ships inside the package), or from a repo checkout: "
                "`docker build -f docker/Dockerfile.sandbox -t edgeverdict-sandbox:latest .`"
            )
        self.docker = resolved
        probe = subprocess.run(
            [self.docker, "image", "inspect", self.image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if probe.returncode != 0:
            raise ExecutionConfigurationError(
                f"sandbox image {self.image!r} is not present; build it with "
                "`docker build -f docker/Dockerfile.sandbox "
                f"-t {self.image} .`"
            )
        if self.network_policy != "none":
            self.log(
                "  [security warning] sandbox network policy is "
                f"{self.network_policy!r}; use only with repositories you trust"
            )

    def _fsize(self, args: Sequence[str]) -> int:
        """Per-file byte cap. Install commands download vendored wheels
        that legitimately exceed the tight execution cap, so they get the
        higher install ceiling; test runs keep the tight bound that guards
        against runaway writes. Same install/execute split as _network."""
        if _looks_like_install(args):
            return self.limits.install_file_size_bytes
        return self.limits.file_size_bytes

    def _db_url(self) -> str:
        """Mode 2 (bring-your-own-database): a host-run Postgres the sandbox
        may reach for integration tests. Set EDGEVERDICT_DB_URL to the host
        connection string (e.g. postgresql://postgres:postgres@localhost:5432).
        Empty -> DB mode off (default; sandbox stays --network none). When set,
        the test phase is given network + a host.docker.internal mapping and
        DATABASE_URL is injected (localhost rewritten to the host gateway so a
        container-side connection reaches the host's Postgres). OPT-IN and
        trust-scoped: this hands AI-generated test code a live DB + network,
        so use only with a trusted repo against a DISPOSABLE database."""
        return os.environ.get("EDGEVERDICT_DB_URL", "")

    def _container_db_url(self) -> str:
        # rewrite a host-local URL so it resolves from inside the container.
        url = self._db_url()
        return url.replace("localhost", "host.docker.internal").replace(
            "127.0.0.1", "host.docker.internal")

    def _network(self, args: Sequence[str]) -> str:
        if self.network_policy == "all":
            return "bridge"
        if self.network_policy == "install" and _looks_like_install(args):
            return "bridge"
        # DB mode: the TEST phase needs to reach the host Postgres. Install is
        # handled by the policy above; a non-install command under DB mode
        # gets bridge network so the connection to host.docker.internal works.
        if self._db_url() and not _looks_like_install(args):
            return "bridge"
        return "none"

    def _docker_command(
        self,
        args: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        name: str,
    ) -> list[str]:
        mount_root = _warm_root(cwd)
        container_cwd = _container_path(cwd, mount_root)
        safe_env = _rewrite_mounted_paths(filtered_environment(env), mount_root)
        uid = str(os.getuid()) if hasattr(os, "getuid") else "65532"
        gid = str(os.getgid()) if hasattr(os, "getgid") else "65532"
        command = [
            self.docker,
            "run",
            "--rm",
            "--init",
            "--pull=never",
            "--name",
            name,
            "--network",
            self._network(args),
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit",
            str(self.limits.pids),
            "--memory",
            self.limits.memory,
            "--cpus",
            self.limits.cpus,
            "--ulimit",
            f"nofile={self.limits.nofile}:{self.limits.nofile}",
            "--ulimit",
            f"fsize={self._fsize(args)}:{self._fsize(args)}",
            "--user",
            f"{uid}:{gid}",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,noexec,size={self.limits.tmpfs_size}",
            "--mount",
            # `rw` is not a valid --mount field (docker rejects it with exit 125,
            # "invalid field 'rw' must be a key=value pair"). Bind mounts are
            # read-write unless readonly is set, so state it explicitly.
            f"type=bind,src={mount_root},dst=/edgeverdict,readonly=false",
            "--workdir",
            container_cwd,
        ]
        # Mode 2 DB access: when EDGEVERDICT_DB_URL is set and this is a test
        # (network-bearing) command, map host.docker.internal to the host and
        # inject DATABASE_URL so pg-meta-style tests can CREATE their
        # disposable test databases against the host Postgres. Install
        # commands don't get the DB (they don't need it); the host mapping is
        # harmless there but we scope it to the DB-bearing command for clarity.
        if self._db_url() and self._network(args) == "bridge" and \
                not _looks_like_install(args):
            command.extend([
                "--add-host", "host.docker.internal:host-gateway",
            ])
            safe_env.setdefault("DATABASE_URL", self._container_db_url())
        # Python user-site inside the mount: the only writable+executable
        # surface in the container. Every command gets it so the install
        # and the test runs resolve the same site; harmless for JS lanes.
        safe_env.setdefault(
            "PYTHONUSERBASE", "/edgeverdict/.edgeverdict-pyuser"
        )
        for key in sorted(safe_env):
            command.extend(["--env", f"{key}={safe_env[key]}"])
        command.append(self.image)
        command.extend(_rewrite_arg_paths(_map_host_interpreter(args), mount_root))
        return command

    def run(self, args, *, cwd, env, timeout):
        name = f"edgeverdict-{uuid.uuid4().hex[:12]}"
        command = self._docker_command(args, cwd=cwd, env=env, name=name)
        stdout_buffer = _TailBuffer(self.limits.max_output_bytes)
        stderr_buffer = _TailBuffer(self.limits.max_output_bytes)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        assert process.stdout is not None and process.stderr is not None
        threads = [
            threading.Thread(target=_drain, args=(process.stdout, stdout_buffer), daemon=True),
            threading.Thread(target=_drain, args=(process.stderr, stderr_buffer), daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            subprocess.run(
                [self.docker, "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            for thread in threads:
                thread.join(timeout=2)
            raise subprocess.TimeoutExpired(
                cmd=list(args),
                timeout=timeout,
                output=stdout_buffer.text(),
                stderr=stderr_buffer.text(),
            ) from exc
        for thread in threads:
            thread.join(timeout=5)
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=returncode,
            stdout=stdout_buffer.text(),
            stderr=stderr_buffer.text(),
        )

    def close(self) -> None:
        return None


def backend_from_env(*, log: Callable[..., None] = print) -> ExecutionBackend:
    mode = os.environ.get("EDGEVERDICT_EXECUTION_BACKEND", "docker").strip().lower()
    if mode == "docker":
        return DockerBackend(log=log)
    if mode == "local":
        return LocalBackend()
    raise ExecutionConfigurationError(
        "EDGEVERDICT_EXECUTION_BACKEND must be 'docker' or 'local'"
    )
