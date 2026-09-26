"""Textual TUI: intake form -> live run view (steps, commands, outputs, verification, stats) -> result."""

from __future__ import annotations

import time

from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Static, TextArea

from veriswe import __version__
from veriswe.intake import issue_names_repo

STATUS_STYLE = {"pass": "green", "fail": "red", "warn": "yellow", "skip": "grey50"}


class IntakeScreen(Screen):
    BINDINGS = [Binding("ctrl+s", "start", "Start"), Binding("ctrl+q", "app.quit", "Quit")]

    def __init__(self, issue: str | None, repo: str | None):
        super().__init__()
        self._issue = issue or ""
        self._repo = repo or ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="intake"):
            yield Label("[b]VeriSWE[/b] - verification-first autonomous coding agent", id="title")
            yield Label("Issue: GitHub issue URL, or paste the full issue text")
            yield TextArea(self._issue, id="issue")
            yield Label("Repository: local path or git URL (optional when the issue URL is on GitHub)")
            yield Input(value=self._repo, placeholder="/path/to/repo  or  https://github.com/owner/repo", id="repo")
            with Horizontal(id="buttons"):
                yield Button("Solve issue  (Ctrl+S)", variant="success", id="start")
            yield Label("", id="error")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start":
            self.action_start()

    def action_start(self) -> None:
        issue = self.query_one("#issue", TextArea).text.strip()
        repo = self.query_one("#repo", Input).value.strip()
        if not issue:
            self.query_one("#error", Label).update("[red]Please provide an issue URL or text.[/red]")
            return
        if not repo and not issue_names_repo(issue):
            self.query_one("#error", Label).update(
                "[red]Please enter the repository (local path or git URL), or give a GitHub issue URL.[/red]"
            )
            return
        self.app.start_run(issue, repo or None)


class RunScreen(Screen):
    BINDINGS = [
        Binding("ctrl+q", "app.quit", "Quit"),
        Binding("f", "toggle_follow", "Follow on/off"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            yield RichLog(id="log", wrap=True, markup=True, highlight=False, auto_scroll=True)
            with VerticalScroll(id="side"):
                yield Static(id="stats")
                yield Static(id="verify")
        yield Footer()

    def action_toggle_follow(self) -> None:
        log = self.query_one("#log", RichLog)
        log.auto_scroll = not log.auto_scroll


class VeriApp(App):
    TITLE = f"VeriSWE v{__version__}"
    CSS = """
    #intake { padding: 1 2; }
    #title { margin-bottom: 1; }
    #issue { height: 1fr; min-height: 8; margin-bottom: 1; }
    #repo { margin-bottom: 1; }
    #buttons { height: auto; }
    #error { margin-top: 1; }
    #log { width: 3fr; border: round $primary; }
    #side { width: 1fr; min-width: 36; border: round $secondary; padding: 0 1; }
    #verify { margin-top: 1; }
    """

    def __init__(self, issue=None, repo=None, model=None, agent_overrides=None):
        super().__init__()
        self._issue, self._repo, self._model = issue, repo, model
        self._overrides = agent_overrides
        self.stats = {"model": "resolving...", "mode": "", "step": 0, "tokens": {}, "cost": 0.0, "status": "starting"}
        self._t0 = time.time()
        self._verify_lines: list[str] = []

    def on_mount(self) -> None:
        if self._issue:
            self.start_run(self._issue, self._repo)
        else:
            self.push_screen(IntakeScreen(self._issue, self._repo))

    # ------------------------------------------------------------------ run control
    def start_run(self, issue: str, repo: str | None) -> None:
        self.push_screen(RunScreen())
        self._t0 = time.time()
        self.run_worker(lambda: self._worker(issue, repo), thread=True, exclusive=True)
        self.set_interval(1.0, self._refresh_stats)

    def _worker(self, issue: str, repo: str | None) -> None:
        from veriswe.runner import run_task

        try:
            run_task(issue, repo, model_override=self._model, on_event=self._on_event_thread, agent_overrides=self._overrides)
        except Exception as e:  # show config / intake errors in the UI
            self.call_from_thread(self._log, Panel(escape(f"{type(e).__name__}: {e}"), title="Error", border_style="red"))
            self.stats["status"] = "error"

    def _on_event_thread(self, kind: str, data: dict) -> None:
        self.call_from_thread(self._handle, kind, data)

    # ------------------------------------------------------------------ rendering
    def _log(self, renderable) -> None:
        try:
            self.screen.query_one("#log", RichLog).write(renderable)
        except Exception:
            pass

    def _handle(self, kind: str, d: dict) -> None:
        s = self.stats
        if kind == "model_ready":
            s["model"], s["mode"] = d["model"], d["mode"]
            self._log(f"[green]Model ready:[/] {escape(d['model'])} [dim]({escape(d['note'])})[/]")
        elif kind == "issue":
            self._log(Panel(escape(d["body"][:1500] or "(empty)"), title=escape(d.get("title") or "Issue"), border_style="cyan"))
        elif kind == "workspace":
            s["repo"] = d["repo"]
            self._log(f"[green]Repository:[/] {escape(d['repo'])} [dim]@ {d['base'][:12]}[/]")
            s["status"] = "working"
        elif kind == "thinking":
            s["status"] = "thinking"
        elif kind == "model":
            s["step"], s["tokens"], s["cost"] = d["step"], d.get("tokens", {}), d.get("cost", 0.0)
            text = (d.get("content") or "").strip()
            self._log(Text.from_markup(f"\n[b magenta]Step {d['step']}[/]"))
            if text:
                self._log(Text(text[:1200] + ("..." if len(text) > 1200 else ""), style="italic grey70"))
            s["status"] = "running command"
        elif kind == "action":
            self._log(Syntax(d["command"][:2000], "bash", theme="monokai", word_wrap=True))
        elif kind == "observation":
            out = (d.get("output") or "").rstrip()
            rc = d.get("returncode")
            snippet = out if len(out) < 1500 else out[:800] + "\n[...]\n" + out[-500:]
            self._log(Text(f"rc={rc} ", style="green" if rc == 0 else "red") + Text(snippet, style="grey62"))
        elif kind == "verify_start":
            s["status"] = f"verifying (round {d['round']})"
            self._log(Text.from_markup(f"\n[b yellow]Harness verification round {d['round']}...[/]"))
        elif kind == "verify":
            r = d["result"]
            lines = [f"[b]Round {r.round}: {'[green]ACCEPTED' if r.passed else '[red]REJECTED'}[/][/b] ({r.seconds:.0f}s)"]
            for c in r.checks:
                lines.append(f" [{STATUS_STYLE.get(c.status, 'white')}]{c.status.upper():4}[/] {c.name}: {escape(c.summary)}")
            self._verify_lines = lines
            self._log("\n".join(lines))
        elif kind == "api_retry":
            s["status"] = f"API: {d['detail']}"
            self._log(f"[dim yellow]API: {escape(d['detail'])}[/]")
        elif kind == "compact":
            self._log(f"[yellow]Request too large ({escape(d['reason'])}): compacting context (level {d['level']}).[/]")
        elif kind == "mode_switch":
            s["mode"] = "text (fallback)"
            self._log("[yellow]Tool calls kept failing at the API: switched to text actions.[/]")
        elif kind == "model_error":
            self._log(f"[red]Model API error:[/] {escape(d['error'][:300])}")
        elif kind == "restore":
            self._log(f"[yellow]Restored best checkpoint (round {d['round']})[/]")
        elif kind == "final":
            s["status"] = d["status"]
        elif kind == "done":
            res = d["result"]
            s["status"] = res.status
            self._log(Panel(Syntax(res.patch or "(empty patch)", "diff", theme="monokai", word_wrap=True), title="Final patch", border_style="green" if res.verified else "yellow"))
            self._log(f"[b]{escape(res.status)}[/b]\nReport: {res.report_path}\nPatch:  {res.run_dir / 'patch.diff'}\n[dim]Press Ctrl+Q to quit.[/]")
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        s = self.stats
        t = s.get("tokens") or {}
        body = (
            f"[b]Status[/b]  {escape(str(s['status']))}\n"
            f"[b]Model[/b]   {escape(str(s['model']))}\n"
            f"[b]Actions[/b] {s.get('mode') or '-'}\n"
            f"[b]Steps[/b]   {s['step']}\n"
            f"[b]Tokens[/b]  in {t.get('prompt', 0):,} (cached {t.get('cached', 0):,})\n"
            f"          out {t.get('completion', 0):,}\n"
            f"[b]Cost[/b]    ${s['cost']:.4f}\n"
            f"[b]Elapsed[/b] {int(time.time() - self._t0)}s"
        )
        try:
            self.screen.query_one("#stats", Static).update(Panel(body, title="Run", border_style="blue"))
            if self._verify_lines:
                self.screen.query_one("#verify", Static).update(Panel("\n".join(self._verify_lines), title="Verification", border_style="yellow"))
        except Exception:
            pass
