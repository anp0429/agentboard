"""Sandbox trustworthiness tests.

Two properties the warm sandbox must have before any verdict is issued:

  1. FIDELITY — the copy IS the repo (modulo declared ignores). A silently
     dropped file makes verdicts depend on copy luck.
  2. VIABILITY — the test runner actually starts. Exit codes proved flaky
     across toolchain versions (a pnpm notice once benched an entire run);
     a functional probe that launches the runner cannot be fooled by logs.

Both failure modes must surface as a single loud prep error, never as
per-finding noise and never as a crash.
"""

from __future__ import annotations

import os

from agentboard.verifiers.finding_verifier import (
    FindingVerifier,
    _copy_discrepancies,
)
from agentboard.verifiers.vitest_verifier import RepoProfile


# ---------------------------------------------------------------------------
# fidelity: the pure checker
# ---------------------------------------------------------------------------

def _make_tree(root, files):
    for rel, content in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)


def test_fidelity_identical_trees_pass(tmp_path):
    files = {
        "pnpm-workspace.yaml": "packages:\n  - packages/*\n",
        "packages/a/src/x.ts": "export const x = 1\n",
    }
    src, dst = str(tmp_path / "src"), str(tmp_path / "dst")
    _make_tree(src, files)
    _make_tree(dst, files)
    assert _copy_discrepancies(src, dst) == []


def test_fidelity_ignored_dirs_do_not_count(tmp_path):
    src, dst = str(tmp_path / "src"), str(tmp_path / "dst")
    _make_tree(src, {"a.py": "x", "node_modules/dep/index.js": "junk",
                     ".git/HEAD": "ref"})
    _make_tree(dst, {"a.py": "x"})  # sandbox legitimately lacks ignored dirs
    assert _copy_discrepancies(src, dst) == []


def test_fidelity_catches_a_dropped_config_file(tmp_path):
    """The exact bug class from the supabase run: a config file the sandbox
    never saw. This must be one loud discrepancy, named."""
    src, dst = str(tmp_path / "src"), str(tmp_path / "dst")
    _make_tree(src, {"a.py": "x", "pnpm-workspace.yaml": "allowBuilds: ..."})
    _make_tree(dst, {"a.py": "x"})
    diffs = _copy_discrepancies(src, dst)
    assert diffs == ["missing from sandbox: pnpm-workspace.yaml"]


def test_fidelity_catches_truncated_file(tmp_path):
    src, dst = str(tmp_path / "src"), str(tmp_path / "dst")
    _make_tree(src, {"a.py": "full content here"})
    _make_tree(dst, {"a.py": "full"})
    diffs = _copy_discrepancies(src, dst)
    assert len(diffs) == 1 and diffs[0].startswith("size mismatch: a.py")


# ---------------------------------------------------------------------------
# smoke probe: wired into the warm base, both outcomes
# ---------------------------------------------------------------------------

def _profile(smoke_cmd):
    return RepoProfile(
        name="fixture",
        install_cmd=["true"],          # succeeds, does nothing
        test_base=["true"],
        build_cmd=None,
        env={},
        smoke_cmd=smoke_cmd,
    )


def _repo_with_tests_file(tmp_path):
    repo = str(tmp_path / "repo")
    _make_tree(repo, {"tests/suite.test.ts": "describe('d', () => {\n});\n"})
    return repo


def test_smoke_probe_failure_is_one_loud_prep_error(tmp_path):
    repo = _repo_with_tests_file(tmp_path)
    v = FindingVerifier(repo, _profile(["false"]), "tests/suite.test.ts")
    try:
        v._ensure_warm()
        assert v._prep_error.startswith("environment smoke probe failed")
    finally:
        v.close()


def test_smoke_probe_success_leaves_env_prepped(tmp_path):
    repo = _repo_with_tests_file(tmp_path)
    v = FindingVerifier(repo, _profile(["true"]), "tests/suite.test.ts")
    try:
        v._ensure_warm()
        assert v._prep_error == ""
        assert v._pristine_tests is not None
    finally:
        v.close()


def test_no_smoke_cmd_means_no_probe(tmp_path):
    repo = _repo_with_tests_file(tmp_path)
    v = FindingVerifier(repo, _profile(None), "tests/suite.test.ts")
    try:
        v._ensure_warm()
        assert v._prep_error == ""
    finally:
        v.close()


def test_presets_declare_a_probe():
    """The common-case presets should ship with the probe on by default:
    write a real probe test, run it, exit 0 — the filter trick
    ("match nothing, pass on empty") died in gauntlet catch 5: vitest 4
    exits 1 when a -t filter skips everything."""
    p = RepoProfile.pnpm_vitest("x", build=False)
    assert p.smoke_cmd is not None
    assert p.smoke_probe is not None
    assert p.smoke_cmd[-1] == p.smoke_probe[0]
    assert "-t" not in p.smoke_cmd


# ---------------------------------------------------------------------------
# run-level banner: one loud failure, never per-finding noise
# ---------------------------------------------------------------------------

def test_env_failure_is_a_run_level_banner(tmp_path):
    from agentboard.review import ReviewFinding, ReviewRun, render_review_html

    repo = _repo_with_tests_file(tmp_path)
    v = FindingVerifier(repo, _profile(["false"]), "tests/suite.test.ts")
    run = ReviewRun(
        intent="i", target="t",
        findings=[ReviewFinding(behavior="a", test_code="test('a', ()=>{})"),
                  ReviewFinding(behavior="b", test_code="test('b', ()=>{})")],
    )
    v.run(run)
    assert run.env_error.startswith("environment smoke probe failed")
    board = str(tmp_path / "board.html")
    render_review_html(run, board)
    html_out = open(board, encoding="utf-8").read()
    assert "Environment preparation failed" in html_out
    assert html_out.count("not executed: environment failure") == 2


# ---------------------------------------------------------------------------
# injection placement: describe-wrapped vs top-level files (the zod lesson)
# ---------------------------------------------------------------------------

def test_inject_into_describe_file_goes_inside_the_block():
    from agentboard.verifiers.finding_verifier import _inject
    pristine = 'import x\n\ndescribe("d", () => {\n  test("a", () => {});\n});\n'
    out, err = _inject(pristine, 'test("new", () => {});')
    assert err == ""
    assert out.index('test("new"') < out.rindex("});")


def test_inject_into_toplevel_file_appends_at_eof_never_nests():
    from agentboard.verifiers.finding_verifier import _inject
    pristine = (
        'import x\n\n'
        'test("a", () => {\n  expect(1).toBe(1);\n});\n\n'
        'test("b", () => {\n  expect(2).toBe(2);\n});\n'
    )
    out, err = _inject(pristine, 'test("new", () => {});')
    assert err == ""
    # the new test must come AFTER test b's close — top level, not nested
    assert out.rstrip().endswith('test("new", () => {});')


def test_inject_strips_proposal_imports():
    """Proposals ride inside a host file; their own imports are illegal there
    and killed three zod findings via whole-file transform failure."""
    from agentboard.verifiers.finding_verifier import _inject
    pristine = 'import x\n\ntest("a", () => {});\n'
    proposal = (
        'import { expect, test } from "vitest";\n'
        'import * as z from "zod/v4";\n'
        'test("new", () => {\n  expect(1).toBe(1);\n});'
    )
    out, err = _inject(pristine, proposal)
    assert err == ""
    assert 'from "vitest"' not in out.replace('import x', '')
    assert 'zod/v4' not in out
    assert 'test("new"' in out


def test_inject_strips_multiline_imports():
    from agentboard.verifiers.finding_verifier import _inject
    pristine = 'test("a", () => {});\n'
    proposal = (
        'import {\n  expect,\n  test,\n} from "vitest";\n'
        'test("new", () => {});'
    )
    out, err = _inject(pristine, proposal)
    assert err == ""
    assert "vitest" not in out
    assert 'test("new"' in out


def test_inject_into_semicolonless_describe_file():
    """prettier semi:false repos close describe with `})` not `});` —
    zustand was the repo that surfaced this. Injection must land inside
    the block, exactly as it does for the semicolon style."""
    from agentboard.verifiers.finding_verifier import _inject

    pristine = (
        "import { it } from 'vitest'\n"
        "describe('d', () => {\n"
        "  it('a', () => {\n"
        "  })\n"
        "})\n"
    )
    out, err = _inject(pristine, "test('new', () => {})")
    assert err == ""
    assert out is not None
    assert out.index("test('new'") < out.rindex("\n})")


# ---------------------------------------------------------------------------
# timeouts during prep: same loud error path as a failed install/build
# ---------------------------------------------------------------------------

def test_install_timeout_is_one_loud_prep_error(tmp_path, monkeypatch):
    """A hung install must land in the prep-error path, exactly like a
    failed one. The smoke probe always caught TimeoutExpired; install and
    build let it escape as a traceback through the whole run."""
    import subprocess

    repo = _repo_with_tests_file(tmp_path)

    def hang(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", hang)
    v = FindingVerifier(repo, _profile(None), "tests/suite.test.ts", timeout=7)
    try:
        v._ensure_warm()  # must not raise
        assert v._prep_error == "install did not finish within 7s"
    finally:
        v.close()


def test_install_timeout_is_a_run_level_banner(tmp_path, monkeypatch):
    """And at run() level it is the one env_error banner, never a crash and
    never per-finding noise."""
    import subprocess

    from agentboard.review import ReviewFinding, ReviewRun

    repo = _repo_with_tests_file(tmp_path)

    def hang(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", hang)
    v = FindingVerifier(repo, _profile(None), "tests/suite.test.ts", timeout=7)
    run = ReviewRun(
        intent="i", target="t",
        findings=[ReviewFinding(behavior="a", test_code="test('a', ()=>{})")],
    )
    v.run(run)
    assert run.env_error == "install did not finish within 7s"
    assert run.findings[0].status == "broken_test"


def test_smoke_noise_strip_peels_the_label_before_filtering():
    # the prefix bug: "stderr: npm warn ..." doesn't START with "npm warn",
    # so the first filter kept everything and the real cause stayed buried
    from agentboard.verifiers.finding_verifier import _strip_pm_noise
    tail = ("stderr: npm warn Unknown env config \"store-dir\".\n"
            "npm notice something\n"
            "Error: Cannot find module 'vitest'")
    out = _strip_pm_noise(tail)
    assert out.startswith("stderr: Error: Cannot find module")
    assert "npm warn" not in out
    # all-noise tails fall back to the raw tail rather than emptiness
    assert _strip_pm_noise("stderr: npm warn only") == "stderr: npm warn only"
