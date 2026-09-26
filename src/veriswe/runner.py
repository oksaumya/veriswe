"""Glue: intake -> model -> workspace -> agent -> artifacts (patch, report, trajectory)."""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from veriswe import config_dir, repo_root
from veriswe.agent import EventHandler, VeriAgent
from veriswe.environment import VeriEnvironment
from veriswe.intake import Issue, read_issue, resolve_repo
from veriswe.model_config import ResolvedModel, probe_action_mode, resolve_model
from veriswe.telemetry import Telemetry, render_box
from veriswe.workspace import Workspace

RUNS_DIR = repo_root / "runs"
WORKSPACE_DIR = repo_root / "workspace"


@dataclass
class RunResult:
    status: str
    verified: bool
    patch: str
    run_dir: Path
    report_path: Path
    stats: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def load_agent_yaml(path: Path | None = None) -> dict:
    return yaml.safe_load((path or config_dir / "agent.yaml").read_text())


def build_model(resolved: ResolvedModel, mode: str, agent_cfg: dict):
    from minisweagent.models import get_model

    a = agent_cfg["agent"]
    cfg = {
        "model_name": resolved.model_name,
        "model_class": "veriswe.models.VeriTextModel" if mode == "text" else "veriswe.models.VeriToolcallModel",
        "model_kwargs": resolved.model_kwargs,
        "observation_template": a["observation_template"],
        "format_error_template": a["format_error_template_text" if mode == "text" else "format_error_template_toolcall"],
        "cost_tracking": "ignore_errors",
    }
    if resolved.provider == "anthropic" or "claude" in resolved.model_name:
        cfg["set_cache_control"] = "default_end"
    return get_model(config=cfg)


def prepare_model(model_override: str | None = None, on_event: EventHandler | None = None):
    """Resolve + probe the model. Returns (resolved, mode, note)."""
    resolved = resolve_model(model_override)
    mode = resolved.action_mode
    note = f"action mode forced to {mode}"
    if mode == "auto":
        mode, note = probe_action_mode(resolved)
    if on_event:
        on_event("model_ready", {"model": resolved.display, "mode": mode, "note": note})
    return resolved, mode, note


def run_task(
    issue_spec: str,
    repo_spec: str | None = None,
    *,
    model_override: str | None = None,
    on_event: EventHandler | None = None,
    model=None,
    mode: str | None = None,
    model_display: str = "",
    agent_overrides: dict | None = None,
) -> RunResult:
    """Solve one issue end-to-end. `model`/`mode` can be injected (tests); otherwise resolved from env/config."""
    telemetry = Telemetry()
    on_event = telemetry.wrap(on_event or (lambda k, d: None))
    t0 = time.time()
    agent_cfg = load_agent_yaml()

    issue: Issue = read_issue(issue_spec)
    on_event("issue", {"title": issue.title, "url": issue.url, "body": issue.body[:2000]})
    repo = resolve_repo(repo_spec, issue, WORKSPACE_DIR)
    ws = Workspace.prepare(repo)
    on_event("workspace", {"repo": str(repo), "base": ws.base_commit})

    text_model_factory = None
    if model is None:
        resolved, mode, _ = prepare_model(model_override, on_event)
        model = build_model(resolved, mode, agent_cfg)
        model_display = resolved.display
        text_model_factory = lambda: build_model(resolved, "text", agent_cfg)  # noqa: E731
    else:
        on_event("model_ready", {"model": model_display or "provided model", "mode": mode or "toolcall", "note": "model supplied by caller"})
    mode = mode or "toolcall"

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_DIR / f"{stamp}-{repo.name}"
    run_dir.mkdir(parents=True, exist_ok=True)
    telemetry.attach(run_dir)

    env_cfg = agent_cfg.get("environment", {})
    env = VeriEnvironment(
        cwd=str(repo),
        timeout=env_cfg.get("timeout", 180),
        env={
            **env_cfg.get("env", {}),
            "VERISWE_SCRATCH": str(ws.scratch),
            "VERISWE_REPO": str(ws.repo),
            "VERISWE_BASE_COMMIT": ws.base_commit,
        },
    )
    a = dict(agent_cfg["agent"])
    for k in ("format_error_template_toolcall", "format_error_template_text", "observation_template"):
        a.pop(k, None)
    a.update(agent_overrides or {})
    a["action_mode"] = mode
    a["output_path"] = run_dir / "trajectory.json"
    agent = VeriAgent(model, env, ws, on_event=on_event, text_model_factory=text_model_factory, **a)
    on_event(
        "run_config",
        {
            "step_limit": a.get("step_limit", 0),
            "max_verification_rounds": a.get("max_verification_rounds", 0),
            "run_dir": str(run_dir),
        },
    )

    error = ""
    retry_logger = logging.getLogger("litellm_model")
    handler = _RetryEventHandler(on_event)
    retry_logger.addHandler(handler)
    retry_logger.propagate = False
    try:
        agent.run(issue.as_task())
    except KeyboardInterrupt:
        error = "Interrupted by user"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        (run_dir / "error.txt").write_text(traceback.format_exc())

    finally:
        retry_logger.removeHandler(handler)
        retry_logger.propagate = True
    patch = ws.diff()
    final = agent.final
    last_exit = (agent.messages[-1].get("extra") or {}).get("exit_status", "") if agent.messages else ""
    status = agent.final_status or (f"Error: {error}" if error else f"UNVERIFIED: agent stopped ({last_exit or 'no submission'})")
    verified = bool(final and final.passed and final.diff == patch)
    try:
        files = ws.changed_files()
    except Exception:
        files = []
    stats = {
        "model": model_display or getattr(getattr(model, "config", None), "model_name", "?"),
        "action_mode": agent.config.action_mode + (" (switched from toolcall)" if agent.config.action_mode != mode else ""),
        "steps": agent.n_calls,
        "cost_usd": round(agent.cost, 4),
        "tokens": agent.tokens,
        "wall_seconds": round(time.time() - t0, 1),
        "verification_rounds": len(agent.attempts),
    }
    stats["telemetry"] = telemetry.write_summary(run_dir, status=status, verified=verified, files=files)
    _collect_artifacts(ws, run_dir)
    (run_dir / "patch.diff").write_text(patch)
    report = render_report(issue, ws, status, verified, patch, agent, stats, error)
    report_path = run_dir / "report.md"
    report_path.write_text(report)
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "status": status,
                "verified": verified,
                "repo": str(repo),
                "base_commit": ws.base_commit,
                "issue_url": issue.url,
                "stats": stats,
                "error": error,
                "final_checks": [c.__dict__ for c in (final.checks if final else [])],
            },
            indent=2,
        )
    )
    result = RunResult(status, verified, patch, run_dir, report_path, stats, error)
    on_event("done", {"result": result})
    return result


class _RetryEventHandler(logging.Handler):
    """Turn tenacity's noisy retry warnings into compact UI events (and keep them off the TUI's screen)."""

    def __init__(self, on_event: EventHandler):
        super().__init__(level=logging.WARNING)
        self.on_event = on_event

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        m = re.search(r"in ([\d.]+) seconds as it raised (\w+)", msg)
        detail = f"{m.group(2)}, retrying in {float(m.group(1)):.0f}s" if m else msg[:200]
        self.on_event("api_retry", {"detail": detail})


def _collect_artifacts(ws: Workspace, run_dir: Path) -> None:
    """Move harness scratch (repro script, junit xml) out of the target repo into the run directory."""
    evidence = run_dir / "evidence"
    evidence.mkdir(exist_ok=True)
    if ws.repro_path.exists():
        shutil.copy2(ws.repro_path, evidence / ws.repro_path.name)
        ws.repro_path.unlink()
    if ws.scratch.exists():
        for f in ws.scratch.glob("junit_*.xml"):
            shutil.copy2(f, evidence / f.name)
        shutil.rmtree(ws.scratch, ignore_errors=True)


ICON = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}


def render_report(issue: Issue, ws: Workspace, status, verified, patch, agent: VeriAgent, stats, error) -> str:
    t = stats["tokens"]
    lines = [
        "# VeriSWE run report",
        "",
        f"- **Status:** {status}",
        f"- **Verified by harness:** {'yes' if verified else 'no'}",
        f"- **Issue:** {issue.title or (issue.body.strip().splitlines() or ['(empty)'])[0].lstrip('# ')[:120]} {issue.url}".rstrip(),
        f"- **Repository:** `{ws.repo}` @ `{ws.base_commit[:12]}`",
        f"- **Model:** `{stats['model']}` (actions: {stats['action_mode']})",
        f"- **Effort:** {stats['steps']} model calls, {t['prompt']:,} prompt tokens "
        f"({t['cached']:,} cached), {t['completion']:,} completion tokens, ${stats['cost_usd']}, "
        f"{stats['wall_seconds']}s wall",
    ]
    if not verified and patch.strip():
        lines += [
            "",
            "> **UNVERIFIED** - the harness could not obtain objective evidence that this patch achieves the task. "
            "It is preserved for inspection only and must not be treated as a successful submission.",
        ]
    if error:
        lines += ["", f"**Error:** `{error}`"]
    if tel := stats.get("telemetry"):
        lines += ["", "## Agent execution (telemetry)", "", "```", render_box(tel), "```",
                  "", "Full event log: `telemetry.jsonl` - summary: `telemetry_summary.json`."]  # fmt: skip
    final = agent.final
    if final:
        title = "Verification evidence (accepted patch)" if final.passed else "Verification checks (best UNVERIFIED attempt)"
        lines += ["", f"## {title}", "", "| Check | Result | Details |", "|---|---|---|"]
        for c in final.checks:
            lines.append(f"| {c.name} | {ICON.get(c.status, c.status)} | {c.summary.replace('|', '/')} |")
        for c in final.checks:
            if c.log:
                lines += ["", f"<details><summary>{c.name} log</summary>", "", "```", c.log, "```", "</details>"]
    if len(agent.attempts) > 1:
        lines += ["", "## Verification history", ""]
        for r in agent.attempts:
            fails = [c.name for c in r.checks if c.status == "fail"]
            lines.append(
                f"- Round {r.round}: {'passed' if r.passed else 'rejected'}"
                + (f" (failed: {', '.join(fails)})" if fails else "")
                + f", {r.seconds:.1f}s"
            )
    lines += ["", "## Patch", "", "```diff", patch.rstrip() or "(empty)", "```", ""]
    return "\n".join(lines)
