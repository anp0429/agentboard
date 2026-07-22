"""Config must be loadable without ever touching the reviewed repo.

Regression for a real dogfooding papercut: `agentboard init` wrote
.agentboard.toml into a repo being drive-by reviewed (zod), and the
untracked file tripped that repo's pre-push hook. Reviewing a repo you
don't own has to leave its working tree byte-for-byte untouched, so config
can now come from an explicit --config path or from a per-repo file in the
user config dir."""

import os

import pytest

from agentboard.config import CONFIG_NAME, ConfigError, load_config, user_config_path


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_repo_config_wins_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "myrepo"
    _write(str(repo / CONFIG_NAME), 'base = "from-repo"\n')
    _write(user_config_path(str(repo)), 'base = "from-user"\n')
    assert load_config(str(repo)).base == "from-repo"


def test_user_config_fallback_when_repo_has_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "myrepo"
    os.makedirs(repo)
    _write(user_config_path(str(repo)), 'base = "from-user"\nproject = "unit"\n')
    cfg = load_config(str(repo))
    assert cfg.base == "from-user"
    assert cfg.project == "unit"


def test_explicit_config_path_beats_both(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "myrepo"
    _write(str(repo / CONFIG_NAME), 'base = "from-repo"\n')
    explicit = tmp_path / "elsewhere.toml"
    _write(str(explicit), 'base = "from-flag"\n')
    assert load_config(str(repo), str(explicit)).base == "from-flag"


def test_explicit_config_path_missing_is_an_error(tmp_path):
    # ConfigError, not SystemExit: SystemExit is not an Exception subclass,
    # so it sailed through the MCP server's except Exception and killed the
    # whole server over a typo'd --config path.
    repo = tmp_path / "myrepo"
    os.makedirs(repo)
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(repo), str(tmp_path / "nope.toml"))


def test_missing_config_is_a_friendly_exit_at_the_api_boundary(tmp_path):
    # The boundary contract: the same friendly message the SystemExit used
    # to carry, narrated through the log sink, and exit code 1 — the review
    # could not run, but nobody's process dies.
    from agentboard.api import ReviewRequest, run_review

    repo = tmp_path / "myrepo"
    os.makedirs(repo)
    lines = []
    result = run_review(
        ReviewRequest(repo=str(repo), config=str(tmp_path / "nope.toml"),
                      target="a.ts"),
        log=lambda *a, **k: lines.append(" ".join(str(x) for x in a)),
    )
    assert result.exit_code == 1
    assert any("nope.toml: file not found" in line for line in lines)


def test_no_config_anywhere_yields_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "myrepo"
    os.makedirs(repo)
    cfg = load_config(str(repo))
    assert cfg.base == ""
    assert cfg.profile_kind == ""


def test_user_config_path_is_outside_the_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    repo = str(tmp_path / "somerepo")
    path = user_config_path(repo)
    assert not path.startswith(repo)
    assert path.endswith(os.path.join("agentboard", "repos", "somerepo.toml"))


def test_init_user_writes_outside_the_repo(tmp_path, monkeypatch):
    from agentboard.cli import main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "visited"
    os.makedirs(repo)
    rc = main(["init", "--user", "--repo", str(repo)])
    assert rc == 0
    # nothing written into the visited repo
    assert os.listdir(repo) == []
    # config landed in the user dir and loads
    dest = user_config_path(str(repo))
    assert os.path.isfile(dest)
    assert load_config(str(repo)).base == "main"
