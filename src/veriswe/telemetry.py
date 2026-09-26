"""Durable run telemetry: every harness event -> runs/<run>/telemetry.jsonl, plus telemetry_summary.json.

Makes autonomy, recovery, verification and context efficiency observable after the run.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

# Events that mean the harness detected a problem and kept the run going.
RECOVERY_EVENTS = ("api_retry", "compact", "mode_switch", "restore", "salvaged", "format_error", "loop_nudge", "blocked")
MAX_STR = 2000


def _secrets() -> list[str]:
    from veriswe.agent import _secrets as agent_secrets

    return agent_secrets()


def _clean(value: Any) -> Any:
    """JSON-safe, size-bounded copy of an event payload."""
    if isinstance(value, str):
        return value if len(value) <= MAX_STR else value[: MAX_STR // 2] + " […] " + value[-MAX_STR // 2 :]
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value][:200]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clean(str(value))


def _event_payload(kind: str, data: dict) -> dict:
    if kind == "verify":
        r = data["result"]
        return {
            "round": r.round,
            "passed": r.passed,
            "seconds": round(r.seconds, 2),
            "checks": [{"name": c.name, "status": c.status, "summary": c.summary} for c in r.checks],
        }
    if kind == "final":
        return {"status": data.get("status")}
    if kind == "done":
        res = data["result"]
        return {"status": res.status, "verified": res.verified}
    if kind == "patch":
        return {"files": data.get("files", []), "diff_chars": len(data.get("diff") or "")}
    if kind == "observation":
        return {**data, "output": data.get("output", "")}
    return data


class Telemetry:
    def __init__(self) -> None:
        self.t0 = time.time()
        self.counts: Counter[str] = Counter()
        self.command_failures = 0
        self.verification: list[dict] = []
        self.context = {"chars_before": 0, "chars_sent": 0}
        self.tokens: dict = {}
        self.files: list[str] = []
        self.task_type = ""
        self._buffer: list[str] = []
        self._path: Path | None = None
        self._secrets = _secrets()

    # ------------------------------------------------------------------ recording
    def wrap(self, downstream):
        def on_event(kind: str, data: dict) -> None:
            try:
                self.record(kind, data)
            except Exception:
                pass  # telemetry must never break a run
            downstream(kind, data)

        return on_event

    def record(self, kind: str, data: dict) -> None:
        self.counts[kind] += 1
        if kind == "observation" and data.get("returncode") not in (0, None):
            self.command_failures += 1
        elif kind == "model":
            ctx = data.get("context") or {}
            self.context["chars_before"] += ctx.get("chars_before", 0)
            self.context["chars_sent"] += ctx.get("chars_sent", 0)
            self.tokens = data.get("tokens") or self.tokens
        elif kind == "verify":
            r = data["result"]
            self.verification.append({"round": r.round, "passed": r.passed})
        elif kind == "patch":
            self.files = data.get("files") or self.files
        elif kind == "task_type":
            self.task_type = data.get("task_type", "")
        line = json.dumps({"t": round(time.time() - self.t0, 3), "event": kind, **_clean(_event_payload(kind, data))}, default=str)
        for secret in self._secrets:
            line = line.replace(secret, "***REDACTED***")
        if self._path:
            with self._path.open("a") as f:
                f.write(line + "\n")
        else:
            self._buffer.append(line)

    def attach(self, run_dir: Path) -> None:
        """Start writing to runs/<run>/telemetry.jsonl (events seen before the run dir existed are flushed)."""
        self._path = run_dir / "telemetry.jsonl"
        with self._path.open("a") as f:
            f.writelines(line + "\n" for line in self._buffer)
        self._buffer.clear()

    # ------------------------------------------------------------------ summary
    def summary(self, *, status: str = "", verified: bool = False, files: list[str] | None = None) -> dict:
        rounds = len(self.verification)
        rejected = sum(1 for v in self.verification if not v["passed"])
        # A rejected verification round that the agent recovered from (it kept working) is a recovery event.
        recovered_rejections = rejected - (1 if self.verification and not self.verification[-1]["passed"] else 0)
        recoveries = {k: self.counts[k] for k in RECOVERY_EVENTS if self.counts[k]}
        if recovered_rejections:
            recoveries["verification_rejection_recovered"] = recovered_rejections
        before, sent = self.context["chars_before"], self.context["chars_sent"]
        t = self.tokens or {}
        return {
            "final_status": status,
            "verified": verified,
            "task_type": self.task_type or "unspecified",
            "model_calls": self.counts["model"],
            "tool_calls": self.counts["action"],
            "files_changed": len(files if files is not None else self.files),
            "failures": self.command_failures + self.counts["model_error"] + self.counts["format_error"] + self.counts["verify_error"],
            "command_failures": self.command_failures,
            "recovery_events": sum(recoveries.values()),
            "recovery_breakdown": recoveries,
            "verification_rounds": rounds,
            "verification_failures": rejected,
            "context_chars_before": before,
            "context_chars_sent": sent,
            "context_chars_reduced": max(0, before - sent),
            "context_reduction_pct": round(100 * (before - sent) / before, 1) if before else 0.0,
            "tokens": {"prompt": t.get("prompt", 0), "completion": t.get("completion", 0), "cached": t.get("cached", 0),
                       "total": t.get("prompt", 0) + t.get("completion", 0)},  # fmt: skip
            "wall_seconds": round(time.time() - self.t0, 1),
        }

    def write_summary(self, run_dir: Path, **kw) -> dict:
        summary = self.summary(**kw)
        (run_dir / "telemetry_summary.json").write_text(json.dumps(summary, indent=2))
        return summary


def render_box(s: dict) -> str:
    """The 'AGENT EXECUTION' box shown at the end of a run (CLI + report)."""

    def k(n: int) -> str:
        return f"{n / 1000:.1f}K" if n >= 1000 else str(n)

    rows = [
        ("Task type", s["task_type"]),
        ("Model calls", s["model_calls"]),
        ("Tool calls", s["tool_calls"]),
        ("Files changed", s["files_changed"]),
        ("Failures", s["failures"]),
        ("Recovery events", s["recovery_events"]),
        ("Verification rounds", s["verification_rounds"]),
        ("Context reduced", f"{k(s['context_chars_reduced'])} chars ({s['context_reduction_pct']}%)"),
        ("Tokens", k(s["tokens"]["total"])),
        ("Final status", "VERIFIED" if s["verified"] else "UNVERIFIED"),
    ]
    width = 42
    out = ["┌" + "─" * width + "┐", "│" + "AGENT EXECUTION".center(width) + "│", "├" + "─" * width + "┤"]
    for label, value in rows:
        text = f" {label:<22}{str(value):>18} "
        out.append("│" + text[:width].ljust(width) + "│")
    out.append("└" + "─" * width + "┘")
    return "\n".join(out)
