"""Git helpers for the target repository: base snapshot, clean diffs, reversible patch application."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Harness scratch files live inside the repo but are git-excluded so they never leak into the patch.
SCRATCH_DIR = ".veriswe"
REPRO_FILE = "reproduce_issue.py"
EXCLUDES = [
    f"{SCRATCH_DIR}/",
    REPRO_FILE,
    "*.veriswe.bak",
    # build/runtime debris the agent may create while verifying (only affects *untracked* files)
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    "*.egg-info/",
    ".venv/",
    "venv/",
    ".mypy_cache/",
    ".tox/",
    "node_modules/",
]


def git(repo: Path, *args: str, env: dict | None = None, check: bool = True, input: str | None = None) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", **(env or {})},
        input=input,
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


@dataclass
class Workspace:
    repo: Path
    base_commit: str

    @classmethod
    def prepare(cls, repo: Path) -> Workspace:
        """Make sure `repo` is a git repo, install excludes, and snapshot the starting state."""
        repo = repo.resolve()
        if not (repo / ".git").exists():
            git(repo, "init", "-q")
            git(repo, "add", "-A")
            git(
                repo,
                "-c", "user.email=veriswe@localhost", "-c", "user.name=veriswe",
                "commit", "-q", "--allow-empty", "-m", "veriswe baseline",
            )  # fmt: skip
        ws = cls(repo=repo, base_commit="")
        ws._install_excludes()
        # If the tree is dirty, snapshot it (tracked + untracked) so the diff only shows *our* changes.
        ws.base_commit = ws.snapshot() or git(repo, "rev-parse", "HEAD").strip()
        (repo / SCRATCH_DIR).mkdir(exist_ok=True)
        return ws

    def _install_excludes(self) -> None:
        info = Path(git(self.repo, "rev-parse", "--git-path", "info/exclude").strip())
        info = info if info.is_absolute() else self.repo / info
        info.parent.mkdir(parents=True, exist_ok=True)
        current = info.read_text() if info.exists() else ""
        missing = [e for e in EXCLUDES if e not in current.splitlines()]
        if missing:
            info.write_text(current.rstrip("\n") + ("\n" if current else "") + "\n".join(missing) + "\n")

    def _tree_of_worktree(self) -> str:
        """Tree object of the full working tree (tracked + untracked, minus ignored), without touching the index."""
        with tempfile.TemporaryDirectory() as td:
            tmp_index = Path(td) / "index"
            env = {"GIT_INDEX_FILE": str(tmp_index)}
            real_index = Path(git(self.repo, "rev-parse", "--git-path", "index").strip())
            real_index = real_index if real_index.is_absolute() else self.repo / real_index
            if real_index.is_file():
                shutil.copyfile(real_index, tmp_index)  # keeps the stat cache -> fast on big repos
            else:
                git(self.repo, "read-tree", "HEAD", env=env, check=False)
            git(self.repo, "add", "-A", env=env)
            return git(self.repo, "write-tree", env=env).strip()

    def snapshot(self) -> str:
        """Commit object capturing the current working tree, or '' if identical to HEAD."""
        tree = self._tree_of_worktree()
        head_tree = git(self.repo, "rev-parse", "HEAD^{tree}", check=False).strip()
        if tree == head_tree:
            return ""
        return git(
            self.repo,
            "-c", "user.email=veriswe@localhost", "-c", "user.name=veriswe",
            "commit-tree", tree, "-p", "HEAD", "-m", "veriswe snapshot",
        ).strip()  # fmt: skip

    def diff(self) -> str:
        """Full patch of our changes relative to the base (includes new files, excludes harness scratch)."""
        tree = self._tree_of_worktree()
        return git(self.repo, "diff", "--binary", self.base_commit, tree)

    def changed_files(self) -> list[str]:
        tree = self._tree_of_worktree()
        out = git(self.repo, "diff", "--name-only", self.base_commit, tree)
        return [line for line in out.splitlines() if line.strip()]

    def reverse(self, patch: str) -> None:
        """Temporarily undo `patch` in the working tree (used to run checks on the original code)."""
        if patch.strip():
            git(self.repo, "apply", "--binary", "-R", "--whitespace=nowarn", input=patch)

    def apply(self, patch: str) -> None:
        if patch.strip():
            git(self.repo, "apply", "--binary", "--whitespace=nowarn", input=patch)

    def restore(self, patch: str) -> None:
        """Reset the working tree to base + patch (used to roll back to the best verified checkpoint)."""
        current = self.diff()
        if current == patch:
            return
        self.reverse(current)
        self.apply(patch)

    @property
    def scratch(self) -> Path:
        return self.repo / SCRATCH_DIR

    @property
    def repro_path(self) -> Path:
        return self.repo / REPRO_FILE
