"""Command guards and loop detection."""

from __future__ import annotations

import re
from collections import deque

_START = r"(?:^|[;&|(`]\s*|\$\(\s*|\n\s*|\bsudo\s+|\bxargs\s+)"

BLOCK_RULES: list[tuple[re.Pattern, str]] = [
    (
        re.compile(_START + r"(?:vi|vim|nvim|nano|emacs|pico|less|more|top|htop|watch|man)\b(?!\s*=)"),
        "Interactive programs are not available (no terminal). Use `view`, `cat`, `head`, `sed -n` or `str_replace`.",
    ),
    (
        re.compile(r"\bgit\s+(?:stash|clean|rebase|merge|cherry-pick|switch|reset\s+--hard|fetch|pull|reflog|worktree)\b"),
        "This git command is blocked: it can destroy your work or reveal future history. "
        "Use `undo_edit FILE` or `git checkout -- FILE` to revert files, and `git diff` to review changes.",
    ),
    (
        re.compile(r"\bgit\s+checkout\s+(?!--\s)(?!-- )\S"),
        "Switching commits/branches is blocked. To revert a file use `git checkout -- FILE` or `undo_edit FILE`.",
    ),
    (
        re.compile(r"\bgit\s+(?:log|show|diff)\b[^\n;&|]*(?:--all\b|--branches\b|--remotes\b|\borigin/|\bupstream/)"),
        "Inspecting other branches or remote history is not allowed; solve the issue from the current code.",
    ),
    (
        re.compile(r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+(?:/|~|\.git\b|\.\s*$|\*\s*$|\.\.)"),
        "Refusing a destructive recursive delete. Delete specific files instead.",
    ),
]


def check_command(command: str) -> str | None:
    """Return a refusal message if the command is blocked, else None."""
    for pattern, message in BLOCK_RULES:
        if pattern.search(command):
            return f"BLOCKED by harness: {message}"
    return None


def _norm(cmd: str) -> str:
    return re.sub(r"\s+", " ", cmd.strip())


class LoopDetector:
    """Detects the agent repeating itself (same command + same result) and injects escalating nudges."""

    def __init__(self, window: int = 12):
        self.history: deque[tuple[str, str]] = deque(maxlen=window)
        self.failed_edits: dict[str, int] = {}

    def record(self, command: str, output: str, returncode: int | None) -> str | None:
        key = (_norm(command), (output or "")[-2000:])
        self.history.append(key)
        repeats = 0
        for item in reversed(self.history):
            if item == key:
                repeats += 1
            else:
                break
        cmd_only = sum(1 for c, _ in self.history if c == key[0])
        nudge = None
        if repeats >= 5 or cmd_only >= 6:
            nudge = (
                "HARNESS WARNING: you have run essentially the same command many times without progress. STOP and "
                "re-plan: summarise what you know, list 2-3 different hypotheses, and try a genuinely different approach."
            )
        elif repeats >= 3:
            nudge = "HARNESS NOTE: this is the same command with the same result as before. Try something different."
        # Repeated failing edits on the same file
        m = re.match(r"\s*str_replace\s+(\S+)", command)
        if m and returncode not in (0, None):
            f = m.group(1)
            self.failed_edits[f] = self.failed_edits.get(f, 0) + 1
            if self.failed_edits[f] >= 3:
                nudge = (
                    f"HARNESS NOTE: {self.failed_edits[f]} edits to {f} have failed. Run `view {f} START END` on the "
                    "exact region first and copy the SEARCH text verbatim (including indentation)."
                )
        elif m:
            self.failed_edits[m.group(1)] = 0
        return nudge
