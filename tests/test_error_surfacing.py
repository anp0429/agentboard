"""Environment errors must surface BOTH output streams.

Found on supabase/mcp (PR #324 gating run): the smoke probe failed, and the
banner showed only npm's harmless stderr warning ('Unknown env config
"store-dir"') because the report used `stderr or stdout` — stderr was
truthy, so the real failure in stdout was never printed. The operator
debugged a warning. These tests pin the rule: a failed subprocess is
reported with every non-empty stream, labeled, and `X or Y` stream
selection is banned from error paths.
"""

import subprocess

from agentboard.verifiers.vitest_verifier import _proc_tail


def _proc(stdout="", stderr="", code=1):
    return subprocess.CompletedProcess(
        args=["x"], returncode=code, stdout=stdout, stderr=stderr
    )


def test_warning_on_stderr_does_not_mask_stdout():
    p = _proc(
        stdout="ERR_PNPM_BAD_WORKSPACE  ignoredBuiltDependencies is not supported",
        stderr='npm warn Unknown env config "store-dir".',
    )
    out = _proc_tail(p)
    assert "ignoredBuiltDependencies" in out          # the actual cause
    assert "store-dir" in out                          # the noise, still visible
    assert "stdout:" in out and "stderr:" in out       # labeled, not merged


def test_stderr_only_failure_is_unchanged():
    out = _proc_tail(_proc(stderr="Error: Cannot find module 'vitest'"))
    assert "Cannot find module" in out
    assert "stdout:" not in out


def test_stdout_only_failure_is_unchanged():
    out = _proc_tail(_proc(stdout="FAIL config could not be loaded"))
    assert "could not be loaded" in out
    assert "stderr:" not in out


def test_silent_failure_says_so():
    assert "no output" in _proc_tail(_proc())
