"""`veriswe` command line entry point (what `make run` launches)."""

from __future__ import annotations

import os
import subprocess
import sys

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

import veriswe  # noqa: F401  (sets env defaults before minisweagent import)
from veriswe import __version__, repo_root

app = typer.Typer(add_completion=False, rich_markup_mode="rich")
console = Console(highlight=False)


class HeadlessPrinter:
    """Streams agent events to the terminal (used when there is no TUI)."""

    def __init__(self, verbose: bool = True):
        self.verbose = verbose

    def __call__(self, kind: str, d: dict) -> None:
        if kind == "model_ready":
            console.print(f"[bold green]Model:[/] {escape(d['model'])}  [dim]({escape(d['mode'])}: {escape(d['note'])})[/]")
        elif kind == "issue":
            console.print(Panel(Text(d["body"][:1500] or "(empty)"), title=escape(d.get("title") or "Issue"), border_style="cyan"))
        elif kind == "workspace":
            console.print(f"[bold green]Repository:[/] {escape(d['repo'])}  [dim]base {d['base'][:12]}[/]")
        elif kind == "model":
            if d.get("content") and self.verbose:
                text = d["content"].strip()
                console.print(f"\n[bold magenta]Step {d['step']}[/] [dim]{escape(text[:600])}{'...' if len(text) > 600 else ''}[/]")
            else:
                console.print(f"\n[bold magenta]Step {d['step']}[/]")
        elif kind == "action":
            console.print(Syntax(d["command"][:1500], "bash", theme="ansi_dark", word_wrap=True))
        elif kind == "observation":
            out = (d.get("output") or "").rstrip()
            rc = d.get("returncode")
            style = "green" if rc == 0 else "red"
            snippet = out if len(out) < 800 else out[:400] + "\n[...]\n" + out[-300:]
            console.print(f"[{style}]rc={rc}[/] [dim]{escape(snippet)}[/]" if snippet else f"[{style}]rc={rc}[/]")
        elif kind == "verify_start":
            console.print(f"\n[bold yellow]Harness verification round {d['round']}...[/]")
        elif kind == "verify":
            r = d["result"]
            for c in r.checks:
                color = {"pass": "green", "fail": "red", "warn": "yellow", "skip": "dim"}.get(c.status, "white")
                console.print(f"  [{color}]{c.status.upper():4}[/] {escape(c.name)}: {escape(c.summary)}")
            console.print(f"  => {'[bold green]ACCEPTED' if r.passed else '[bold red]REJECTED'}[/] ({r.seconds:.1f}s)")
        elif kind == "task_type":
            console.print(f"[bold cyan]Task type:[/] {escape(d['task_type'])}")
        elif kind == "verify_error":
            console.print(f"[bold red]VERIFICATION ERROR:[/] {escape(d['error'][:300])} (patch NOT accepted)")
        elif kind == "api_retry":
            console.print(f"[dim yellow]  API: {escape(d['detail'])}[/]")
        elif kind == "compact":
            console.print(f"[yellow]Request too large ({escape(d['reason'])}): compacting context (level {d['level']}).[/]")
        elif kind == "mode_switch":
            console.print("[bold yellow]Tool calls keep failing at the API: switched to text actions.[/]")
        elif kind == "model_error":
            console.print(f"[bold red]Model API error:[/] {escape(d['error'][:300])}")
        elif kind == "restore":
            console.print(f"[yellow]Restored best checkpoint from verification round {d['round']}[/]")
        elif kind == "final":
            console.print(f"\n[bold]{escape(d['status'])}[/]")


def _prompt_multiline(label: str) -> str:
    console.print(f"[bold cyan]{label}[/] [dim](finish with an empty line, or Ctrl-D)[/]")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip() and lines:
            break
        lines.append(line)
    return "\n".join(lines).strip()


@app.command()
def main(
    issue: str = typer.Option(None, "--issue", "-i", envvar="ISSUE", help="GitHub issue URL, owner/repo#N, issue text, @file, or '-' for stdin."),
    task: str = typer.Option(None, "--task", "-t", envvar="TASK", help="A general software-engineering task in plain text (feature, test fix, refactor, ...). Needs --repo."),
    repo: str = typer.Option(None, "--repo", "-r", envvar="REPO", help="Path or git URL of the target repository."),
    model: str = typer.Option(None, "--model", "-m", envvar="AI_MODEL", help="Override model (else config/model.yaml)."),
    headless: bool = typer.Option(False, "--headless", envvar="HEADLESS", help="Plain streaming output instead of the TUI."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Less output in headless mode."),
    step_limit: int = typer.Option(None, "--step-limit", envvar="STEP_LIMIT", help="Max model calls."),
    demo_replay: bool = typer.Option(False, "--demo-replay", help="Offline recovery demo: scripted model decisions, real harness (no API key)."),
) -> None:
    """VeriSWE - autonomous, verification-first coding agent."""
    if not os.getenv("AI_API_KEY", "").strip():
        os.environ.pop("AI_API_KEY", None)  # an empty value must not block the local .env
    load_dotenv(repo_root / ".env", override=False)  # optional local convenience; never committed
    overrides = {"step_limit": step_limit} if step_limit else None
    issue = issue or task
    replay = None
    if demo_replay:
        from veriswe import demo
        from veriswe.runner import WORKSPACE_DIR

        repo = str(demo.prepare_repo(WORKSPACE_DIR))
        issue = demo.issue_spec()
        replay = demo.run_kwargs()

    use_tui = not headless and sys.stdin.isatty() and sys.stdout.isatty() and os.getenv("TERM", "") != "dumb"
    if use_tui:
        from veriswe.tui import VeriApp

        VeriApp(issue=issue, repo=repo, model=model, agent_overrides=overrides, replay=replay).run()
        return

    from veriswe.intake import issue_names_repo
    from veriswe.model_config import ModelConfigError
    from veriswe.runner import run_task
    from veriswe.telemetry import render_box

    console.print(f"[bold]VeriSWE[/] v{__version__} - verification-first coding agent (built on mini-swe-agent)")
    if not issue:
        if sys.stdin.isatty():
            issue = _prompt_multiline("Paste the GitHub issue URL or the issue text:")
        else:
            issue = sys.stdin.read().strip()
            if not issue:
                console.print("[bold red]No issue given.[/] Pass ISSUE=<github-issue-url | text | @file>, or pipe it on stdin.")
                raise typer.Exit(2)
    if not repo and not issue_names_repo(issue or ""):
        if not sys.stdin.isatty():
            console.print("[bold red]No repository given.[/] Pass REPO=<path-or-git-url>, or use a GitHub issue URL as ISSUE.")
            raise typer.Exit(2)
        while not repo:
            repo = console.input("[bold cyan]Repository to fix[/] [dim](local path or git URL)[/]: ").strip()
    try:
        result = run_task(issue, repo, model_override=model, on_event=HeadlessPrinter(verbose=not quiet), agent_overrides=overrides, **(replay or {}))
    except ModelConfigError as e:
        console.print(f"[bold red]Model configuration error:[/] {escape(str(e))}")
        raise typer.Exit(2)
    except (ValueError, FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as e:
        console.print(f"[bold red]Input error:[/] {escape(str(e))}")
        raise typer.Exit(2)
    console.print(Panel(Markdown(result.report_path.read_text().split("## Patch")[0]), title="Result", border_style="green" if result.verified else "yellow"))
    if tel := result.stats.get("telemetry"):
        console.print(render_box(tel), markup=False, style="bold green" if result.verified else "bold yellow", highlight=False)
    console.print(f"Patch: [bold]{result.run_dir / 'patch.diff'}[/]   Report: [bold]{result.report_path}[/]")
    console.print(f"Telemetry: [bold]{result.run_dir / 'telemetry.jsonl'}[/] (+ telemetry_summary.json)")
    raise typer.Exit(0 if result.patch.strip() else 1)


if __name__ == "__main__":
    app()
