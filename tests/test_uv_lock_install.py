# EDGEVERDICT_UV_LOCK_INSTALL_TESTS_V1
"""uv-locked repos (sig EDGEVERDICT_UV_LOCK_INSTALL_V1).

posthog/posthog: the flat pip command built from pyproject could not
resolve workspace members (posthog-owners, hogli are path sources) or
git-pinned sources, so every install died at resolution. The lock knows
all of it: export it frozen, pip-install the export into the user-site,
install workspace members editable with no deps.
"""
from __future__ import annotations

import os
import subprocess

from edgeverdict.config import (
    _python_sandbox_install,
    _uv_lock_install,
    _uv_workspace_members,
)
from edgeverdict.execution import _looks_like_install
from edgeverdict.verifiers.finding_verifier import (
    _install_failure_class,
    _repo_requires_python,
)

_PYPROJECT = """
[project]
name = "bigapp"
requires-python = "==3.13.13"
dependencies = ["django~=5.2.0", "posthog-owners", "hogli"]

[dependency-groups]
dev = ["pytest~=8.4"]

[tool.uv.workspace]
members = ["tools/*"]

[tool.uv.sources]
hogli = { workspace = true }
posthog-owners = { workspace = true }
"""

_MEMBER = """
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[project]
name = "%s"
version = "0.1.0"
"""


def _uv_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text(_PYPROJECT)
    (root / "uv.lock").write_text("version = 1\n")
    for m in ("hogli", "owners"):
        d = root / "tools" / m
        d.mkdir(parents=True)
        (d / "pyproject.toml").write_text(_MEMBER % m)
    # a member without a build backend is not editable-installable: skipped
    bare = root / "tools" / "bare"
    bare.mkdir()
    (bare / "pyproject.toml").write_text('[project]\nname = "bare"\nversion = "0"\n')
    return str(root)


def test_workspace_members_expand_globs_and_require_build_backend(tmp_path):
    root = _uv_repo(tmp_path)
    assert _uv_workspace_members(root) == ["tools/hogli", "tools/owners"]


def test_uv_lock_repo_installs_via_export(tmp_path, monkeypatch):
    root = _uv_repo(tmp_path)
    monkeypatch.setenv("EDGEVERDICT_EXECUTION_BACKEND", "docker")
    cmd = _python_sandbox_install(root, "tests/test_x.py")
    assert cmd[:2] == ["sh", "-c"]
    script = cmd[2].splitlines()
    assert script[0] == "set -e"
    assert script[1].endswith("pip install --quiet --user --no-cache-dir uv")
    assert "uv export --frozen --no-hashes --no-emit-workspace --no-emit-project" in script[2]
    assert "--group dev" in script[2]
    assert script[3].startswith("env -u SETUPTOOLS_SCM_PRETEND_VERSION python -m pip install")
    assert script[3].endswith("--ignore-requires-python -r .edgeverdict-uv-reqs.txt")
    assert script[4].endswith("--no-deps -e tools/hogli -e tools/owners")
    # no build backend at the root -> deps only, no `-e .`
    assert not any(line.endswith("-e .") for line in script)


def test_uv_lock_extra_becomes_export_extra(tmp_path):
    root = _uv_repo(tmp_path)
    cmd = _uv_lock_install(root, ".[test]", [])
    assert "--extra test" in cmd[2]


def test_root_with_build_backend_is_installed_editable_last(tmp_path):
    root = _uv_repo(tmp_path)
    with open(os.path.join(root, "pyproject.toml"), "a") as fh:
        fh.write('\n[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n')
    cmd = _uv_lock_install(root, ".", [])
    assert cmd[2].splitlines()[-1].endswith("--no-deps -e .")


def test_no_uv_lock_keeps_the_pip_path(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = ["attrs"]\n')
    monkeypatch.setenv("EDGEVERDICT_EXECUTION_BACKEND", "docker")
    cmd = _python_sandbox_install(str(root), "")
    assert cmd[:3] == ["python", "-m", "pip"]


def test_no_binary_env_is_threaded_into_the_export_install(tmp_path, monkeypatch):
    root = _uv_repo(tmp_path)
    monkeypatch.setenv("EDGEVERDICT_PIP_NO_BINARY", "lxml,xmlsec")
    cmd = _uv_lock_install(root, ".", [])
    assert "--ignore-requires-python --no-binary lxml,xmlsec -r .edgeverdict-uv-reqs.txt" in cmd[2]
    monkeypatch.delenv("EDGEVERDICT_PIP_NO_BINARY")
    assert "--no-binary" not in _uv_lock_install(root, ".", [])[2]


def test_sh_script_install_is_recognized_as_install():
    script = "set -e\npython -m pip install --user uv\npython -m uv export --frozen"
    assert _looks_like_install(["sh", "-c", script]) is True
    assert _looks_like_install(["sh", "-c", "echo hi"]) is False
    assert _looks_like_install(["bash", "-c", "python -m uv sync --frozen"]) is True


def test_python_version_mismatch_is_a_named_failure_class(tmp_path):
    proc = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="",
        stderr="ERROR: Ignored the following versions that require a different "
               "python version: 8.0.0 Requires-Python >=3.13\n"
               "ERROR: Could not find a version that satisfies the requirement "
               "pyyaml-ft==8.0.0 (from versions: none)\n")
    assert _install_failure_class(proc) == "python"
    root = _uv_repo(tmp_path)
    assert _repo_requires_python(root) == "==3.13.13"
    assert _repo_requires_python(str(tmp_path)) == ""
