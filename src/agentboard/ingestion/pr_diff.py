"""PR diff ingestion — the SECOND input the agents review (intent is the first).

A review has two givens: the INTENT (what #277 asked) and the CHANGE (what #278
did). Until now the pipeline read the whole target file; that is not the change,
it is the file the change lives in. The agents should review the DIFF: the lines
#278 actually added/removed, against the intent.

Deterministic, dependency-free (git via subprocess). "before" is the true branch
point (`git merge-base head base`), not current main, which may have drifted.
No LLM here — this is ground truth about what changed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field


@dataclass
class ChangedFile:
    path: str
    added: str = ""  # added lines (the new behavior to review)
    removed: str = ""  # removed lines (what it replaced)
    hunks: list[str] = field(default_factory=list)


@dataclass
class PRDiff:
    base: str  # resolved merge-base sha
    head: str  # PR head ref/sha
    files: list[ChangedFile] = field(default_factory=list)

    def file(self, path: str) -> ChangedFile | None:
        return next((f for f in self.files if f.path == path), None)


def _git(repo: str, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, timeout=120
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:200]}")
    return r.stdout


def load_pr_diff(repo: str, head: str, base: str = "main") -> PRDiff:
    """Diff the PR head against the true branch point (merge-base with base).

    `head` is the PR branch/sha; `base` is the branch it targets (usually main).
    We diff against the merge-base so a drifted main does not add unrelated files.
    """
    merge_base = _git(repo, "merge-base", head, base).strip()
    name_status = _git(repo, "diff", "--name-only", f"{merge_base}..{head}")
    files: list[ChangedFile] = []
    for path in (p for p in name_status.splitlines() if p.strip()):
        patch = _git(repo, "diff", f"{merge_base}..{head}", "--", path)
        added, removed, hunks = _split_patch(patch)
        files.append(ChangedFile(path=path, added=added, removed=removed, hunks=hunks))
    return PRDiff(base=merge_base, head=head, files=files)


def load_worktree_diff(repo: str, base: str = "HEAD") -> PRDiff:
    """Diff the WORKING TREE (staged + unstaged edits) against a ref.

    This is the agent-session mode: an agent has edited files on disk and wants
    them gated before committing. The gate's sandbox copies the working tree,
    so this diff and the executed code are the same facts — unlike
    `load_pr_diff`, which describes refs and would silently disagree with what
    the sandbox runs when the tree is dirty.

    Untracked files do not appear in `git diff` and are therefore invisible
    here (they still execute in the sandbox). Reviewing a brand-new file means
    `git add`-ing it first, which is also what makes it part of the change.
    """
    base_sha = _git(repo, "rev-parse", base).strip()
    name_only = _git(repo, "diff", "--name-only", base_sha)
    files: list[ChangedFile] = []
    for path in (p for p in name_only.splitlines() if p.strip()):
        patch = _git(repo, "diff", base_sha, "--", path)
        added, removed, hunks = _split_patch(patch)
        files.append(ChangedFile(path=path, added=added, removed=removed, hunks=hunks))
    return PRDiff(base=base_sha, head="WORKTREE", files=files)


def _split_patch(patch: str) -> tuple[str, str, list[str]]:
    added: list[str] = []
    removed: list[str] = []
    hunks: list[str] = []
    cur: list[str] = []
    for line in patch.splitlines():
        if line.startswith("@@"):
            if cur:
                hunks.append("\n".join(cur))
            cur = [line]
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
            cur.append(line)
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
            cur.append(line)
        elif cur:
            cur.append(line)
    if cur:
        hunks.append("\n".join(cur))
    return "\n".join(added), "\n".join(removed), hunks


def diff_blob(diff: PRDiff, max_chars: int | None = None) -> str:
    """A compact textual view of the change for an agent prompt: EVERY changed
    file's added lines. This is what an agent reviews, not the whole repo.

    GENERIC BY DEFAULT: no target, no human-chosen file, no cap. The agent sees
    the whole change and has to notice what matters (foreign keys, etc.) itself —
    that is the review, not a lookup. An earlier version prioritized a hand-picked
    TARGET file; that smuggled the human's guess about where the bug is into the
    ranking and broke genericness. Removed.

    `max_chars` is OPTIONAL and defaults to None = no cap (feed everything). If a
    caller ever needs a bound (a tiny context window), it is spent FAIRLY: an
    equal per-file share, so no file is dropped entirely — never git-order or a
    human's pick deciding who falls off the end.
    """
    header = f"PR head {diff.head} vs base {diff.base[:12]}"
    blocks = [f"\n--- {f.path} ---\n{f.added}" for f in diff.files]

    if max_chars is None:  # default: feed everything
        return header + "".join(blocks)

    # bounded mode: equal share per file, fairness not git order
    if not blocks:
        return header
    body_budget = max(0, max_chars - len(header))
    per_file = body_budget // len(blocks)
    out = [header]
    for b in blocks:
        out.append(b if len(b) <= per_file else b[:per_file] + "\n… [truncated]")
    return "".join(out)
