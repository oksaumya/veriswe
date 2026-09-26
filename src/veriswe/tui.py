"""Textual TUI: intake form -> live run dashboard (phases, activity, patch, verification, stats) -> result."""

from __future__ import annotations

import os
import re
import time

from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from veriswe import __version__
from veriswe.intake import issue_names_repo

PHASES = ["Setup", "Explore", "Reproduce", "Fix", "Verify", "Done"]
CHECK_ICON = {"pass": ("✓", "green"), "fail": ("✗", "red"), "warn": ("!", "yellow"), "skip": ("–", "grey50")}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
EXPLORE_CMD = re.compile(r"^\s*(search|view|grep|rg|ls|find|cat|head|tail|nl|tree|git\s+(status|diff|grep|ls-files))\b")
BANNER = r"""
 __     __        _ ______        _______
 \ \   / /__ _ __(_) ___\ \      / / ____|
  \ \ / / _ \ '__| \___ \\ \ /\ / /|  _|
   \ V /  __/ |  | |___) |\ V  V / | |___
    \_/ \___|_|  |_|____/  \_/\_/  |_____|
"""


def key_status() -> str:
    """Describe the configured credential without ever revealing it."""
    key = os.getenv("AI_API_KEY", "").strip()
    if not key:
        return "[red]✗ AI_API_KEY is not set[/]  - run [b]export AI_API_KEY=...[/b] before [b]make run[/b]"
    from veriswe.model_config import detect_provider

    host = detect_provider(key)
    where = "auto-detected at start (DeepSeek, Qwen/DashScope, SiliconFlow, OpenAI, ...)" if host == "openai" else host
    model = os.getenv("AI_MODEL", "").strip() or "best available DeepSeek/Qwen model"
    return f"[green]✓ AI_API_KEY is set[/]  [dim]host:[/] {escape(where)}  [dim]model:[/] {escape(model)}"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def bar(value: int, total: int, width: int = 18) -> str:
    if total <= 0:
        return ""
    filled = min(width, round(width * value / total))
    return f"[cyan]{'█' * filled}[/][grey30]{'░' * (width - filled)}[/]"


# ====================================================================== intake
class IntakeScreen(Screen):
    BINDINGS = [Binding("ctrl+s", "start", "Solve issue", priority=True), Binding("ctrl+q", "app.quit", "Quit")]

    def __init__(self, issue: str | None, repo: str | None):
        super().__init__()
        self._issue = issue or ""
        self._repo = repo or ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="intake"):
            yield Static(Text(BANNER.strip("\n"), style="bold cyan"), id="banner")
            yield Static(
                "[b]Verification-first autonomous coding agent[/b]  [dim]- reproduce, fix, and PROVE it[/]",
                id="tagline",
            )
            yield Static(key_status(), id="keystatus")
            yield Label("[b]1. Issue[/b]  [dim]GitHub issue URL, owner/repo#123, or paste the issue text[/]")
            yield TextArea(self._issue, id="issue")
            yield Label("[b]2. Repository[/b]  [dim]local path or git URL - not needed for a GitHub issue URL[/]")
            yield Input(value=self._repo, placeholder="/path/to/repo   or   https://github.com/owner/repo", id="repo")
            with Horizontal(id="buttons"):
                yield Button("▶  Solve issue   (Ctrl+S)", variant="success", id="start")
            yield Label("", id="error")
            yield Static(
                "[dim]Examples:  https://github.com/owner/repo/issues/42   ·   owner/repo#42   ·   "
                "text + repository path[/]",
                id="examples",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#issue", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start":
            self.action_start()

    def action_start(self) -> None:
        issue = self.query_one("#issue", TextArea).text.strip()
        repo = self.query_one("#repo", Input).value.strip()
        err = self.query_one("#error", Label)
        if not issue:
            err.update("[red]✗ Please provide an issue URL or the issue text.[/red]")
            return
        if not repo and not issue_names_repo(issue):
            err.update("[red]✗ Please enter the repository (local path or git URL), or give a GitHub issue URL.[/red]")
            return
        self.app.start_run(issue, repo or None)


# ====================================================================== run dashboard
class RunScreen(Screen):
    BINDINGS = [
        Binding("ctrl+q", "app.quit", "Quit"),
        Binding("a", "show_tab('activity')", "Activity"),
        Binding("p", "show_tab('patch')", "Patch"),
        Binding("i", "show_tab('issue')", "Issue"),
        Binding("f", "toggle_follow", "Follow"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="phases")
        with Horizontal(id="main"):
            with TabbedContent(initial="activity", id="tabs"):
                with TabPane("Activity", id="activity"):
                    yield RichLog(id="log", wrap=True, markup=True, highlight=False, auto_scroll=True)
                with TabPane("Patch", id="patch"):
                    yield RichLog(id="patchlog", wrap=True, markup=True, highlight=False, auto_scroll=False)
                with TabPane("Issue", id="issue"):
                    yield Markdown("*waiting for the issue...*", id="issuemd")
            with VerticalScroll(id="side"):
                yield Static(id="result")
                yield Static(id="stats")
                yield Static(id="verify")
                yield Static(id="files")
        yield Footer()

    def action_show_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab

    def action_toggle_follow(self) -> None:
        log = self.query_one("#log", RichLog)
        log.auto_scroll = not log.auto_scroll
        self.notify(f"Follow {'on' if log.auto_scroll else 'off'}", timeout=2)


class VeriApp(App):
    TITLE = "VeriSWE"
    SUB_TITLE = f"v{__version__} · verification-first coding agent"
    CSS = """
    #intake { padding: 0 2; }
    #banner { height: auto; margin-top: 1; }
    #tagline { margin-bottom: 1; }
    #keystatus { border: round $primary; padding: 0 1; margin-bottom: 1; height: auto; }
    #issue { height: 1fr; min-height: 6; margin-bottom: 1; }
    #repo { margin-bottom: 1; }
    #buttons { height: auto; }
    #error { margin-top: 1; height: auto; }
    #examples { margin-top: 1; }
    #phases { height: 3; padding: 0 1; content-align: left middle; border: round $primary; }
    #main { height: 1fr; }
    #tabs { width: 3fr; }
    #side { width: 1fr; min-width: 40; max-width: 60; padding: 0 1; }
    #result, #stats, #verify, #files { height: auto; margin-bottom: 1; }
    """

    def __init__(self, issue=None, repo=None, model=None, agent_overrides=None):
        super().__init__()
        self._issue, self._repo, self._model = issue, repo, model
        self._overrides = agent_overrides
        self.stats = {
            "model": "resolving...",
            "mode": "",
            "step": 0,
            "step_limit": 0,
            "tokens": {},
            "cost": 0.0,
            "status": "starting",
            "busy": True,
        }
        self.phase = "Setup"
        self.checks: list = []
        self.verify_round = 0
        self.verify_passed: bool | None = None
        self.files: list[str] = []
        self.result = None
        self._t0 = time.time()
        self._spin = 0
        self._step_started = time.time()
        self._source_edited = False

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
        self.set_interval(0.15, self._tick)

    def _worker(self, issue: str, repo: str | None) -> None:
        from veriswe.runner import run_task

        try:
            run_task(issue, repo, model_override=self._model, on_event=self._on_event_thread, agent_overrides=self._overrides)
        except Exception as e:  # config / intake errors: show them in the UI
            self.call_from_thread(self._fatal, f"{type(e).__name__}: {e}")

    def _on_event_thread(self, kind: str, data: dict) -> None:
        self.call_from_thread(self._handle, kind, data)

    def _fatal(self, message: str) -> None:
        self.stats.update(status="error", busy=False)
        self._log(Panel(escape(message), title="✗ Could not run", border_style="red"))
        self._update_result(Panel(Text(message[:400]), title="✗ ERROR", border_style="red"))

    # ------------------------------------------------------------------ helpers
    def _q(self, selector, kind=None):
        try:
            return self.screen.query_one(selector, kind) if kind else self.screen.query_one(selector)
        except Exception:
            return None

    def _log(self, renderable) -> None:
        if log := self._q("#log", RichLog):
            log.write(renderable)

    def _set_phase(self, phase: str, force: bool = False) -> None:
        if force or PHASES.index(phase) > PHASES.index(self.phase):
            self.phase = phase

    def _infer_phase(self, command: str) -> None:
        if "reproduce_issue.py" in command or "check_repro" in command:
            if not self._source_edited:
                self._set_phase("Reproduce")
        elif re.search(r"\b(str_replace|undo_edit)\b", command) or re.search(r"\bsed\s+-i\b", command):
            self._source_edited = True
            self._set_phase("Fix")
        elif re.search(r"\b(pytest|unittest|tox|npm\s+test|go\s+test|cargo\s+test)\b", command) and self._source_edited:
            self._set_phase("Fix")
        elif EXPLORE_CMD.search(command):
            self._set_phase("Explore")

    # ------------------------------------------------------------------ events
    def _handle(self, kind: str, d: dict) -> None:
        try:
            self._handle_event(kind, d)
        except Exception as e:  # a rendering bug must never break the run
            self._log(f"[dim red]ui: {escape(str(e))}[/]")
        self._render_side()

    def _handle_event(self, kind: str, d: dict) -> None:
        s = self.stats
        if kind == "model_ready":
            s["model"], s["mode"] = d["model"], d["mode"]
            self._log(Text.assemble(("● ", "green"), ("Model ready  ", "bold"), (d["model"], "cyan"), (f"   {d['note']}", "dim")))
        elif kind == "issue":
            title = d.get("title") or "Issue"
            body = d.get("body") or ""
            if md := self._q("#issuemd", Markdown):
                md.update(f"# {title}\n\n{d.get('url') or ''}\n\n{body}")
            self._log(Panel(Text(body[:900] + ("…" if len(body) > 900 else "")), title=f"[b]{escape(title)}[/b]", border_style="cyan"))
        elif kind == "workspace":
            s["repo"] = d["repo"]
            s["status"] = "working"
            self._log(Text.assemble(("● ", "green"), ("Repository  ", "bold"), (d["repo"], "cyan"), (f"  @ {d['base'][:12]}", "dim")))
        elif kind == "run_config":
            s["step_limit"] = d.get("step_limit") or 0
            s["run_dir"] = d.get("run_dir", "")
        elif kind == "thinking":
            s["status"] = "thinking"
            self._step_started = time.time()
        elif kind == "model":
            s["step"], s["tokens"], s["cost"] = d["step"], d.get("tokens", {}), d.get("cost", 0.0)
            took = time.time() - self._step_started
            self._log(Rule(f"[b magenta]Step {d['step']}[/]  [dim]{took:.1f}s[/]", style="grey35", align="left"))
            text = (d.get("content") or "").strip()
            if text:
                self._log(Text(text[:900] + ("…" if len(text) > 900 else ""), style="italic grey70"))
            s["status"] = "running command"
        elif kind == "action":
            command = d["command"]
            self._infer_phase(command)
            self._log(Syntax(command[:2000], "bash", theme="monokai", word_wrap=True, background_color="default"))
        elif kind == "observation":
            out = (d.get("output") or "").rstrip()
            rc = d.get("returncode")
            snippet = out if len(out) < 1400 else out[:700] + "\n[…]\n" + out[-500:]
            badge = Text(" ✓ " if rc == 0 else f" rc={rc} ", style="bold black on green" if rc == 0 else "bold white on red")
            self._log(Text.assemble(badge, " ", Text(snippet or "(no output)", style="grey62")))
        elif kind == "patch":
            self.files = d.get("files") or []
            self._show_patch(d.get("diff") or "")
        elif kind == "verify_start":
            self.verify_round = d["round"]
            self._set_phase("Verify", force=True)
            s["status"] = f"verifying (round {d['round']})"
            self._log(Rule(f"[b yellow]Harness verification · round {d['round']}[/]", style="yellow"))
        elif kind == "verify":
            r = d["result"]
            self.checks, self.verify_passed = r.checks, r.passed
            table = Table.grid(padding=(0, 1))
            for c in r.checks:
                icon, color = CHECK_ICON.get(c.status, ("?", "white"))
                table.add_row(Text(icon, style=f"bold {color}"), Text(c.name, style="bold"), Text(c.summary))
            verdict = Text(" ACCEPTED ", style="bold black on green") if r.passed else Text(" REJECTED ", style="bold white on red")
            self._log(Group(table, Text.assemble(verdict, (f"  {r.seconds:.1f}s", "dim"))))
            if not r.passed:
                self._set_phase("Fix", force=True)
                self._log(Text("The harness sent the failures back to the agent.", style="yellow"))
        elif kind == "api_retry":
            s["status"] = f"API: {d['detail']}"
            self._log(Text(f"⟳ API {d['detail']}", style="dim yellow"))
        elif kind == "compact":
            self._log(Text(f"⇣ Context compacted (level {d['level']}, {d['reason']})", style="yellow"))
        elif kind == "mode_switch":
            s["mode"] = "text (fallback)"
            self._log(Text("⇄ Tool calls kept failing at the API: switched to text actions", style="yellow"))
        elif kind == "model_error":
            self._log(Panel(escape(d["error"][:500]), title="Model API error", border_style="red"))
        elif kind == "restore":
            self._log(Text(f"↺ Restored the best verified checkpoint (round {d['round']})", style="yellow"))
        elif kind == "final":
            s["status"] = d["status"]
        elif kind == "done":
            self._finish(d["result"])

    def _show_patch(self, diff: str) -> None:
        if plog := self._q("#patchlog", RichLog):
            plog.clear()
            if diff.strip():
                plog.write(Syntax(diff, "diff", theme="monokai", word_wrap=True, background_color="default"))
            else:
                plog.write(Text("(no changes yet)", style="dim"))

    def _finish(self, res) -> None:
        self.result = res
        s = self.stats
        s.update(status=res.status, busy=False)
        self._set_phase("Done", force=True)
        self._show_patch(res.patch)
        t = res.stats.get("tokens", {})
        summary = Table.grid(padding=(0, 1))
        summary.add_row("[dim]Status[/]", escape(res.status))
        summary.add_row("[dim]Steps[/]", str(res.stats.get("steps")))
        summary.add_row("[dim]Tokens[/]", f"{t.get('prompt', 0) + t.get('completion', 0):,}")
        summary.add_row("[dim]Time[/]", fmt_duration(res.stats.get("wall_seconds", 0)))
        summary.add_row("[dim]Report[/]", escape(str(res.report_path)))
        title, style = ("✓ VERIFIED FIX", "green") if res.verified else (("! UNVERIFIED PATCH", "yellow") if res.patch.strip() else ("✗ NO PATCH", "red"))
        self._update_result(Panel(summary, title=f"[b]{title}[/b]", border_style=style))
        self._log(Rule(f"[b {style}]{title}[/]", style=style))
        self._log(Text(f"Report: {res.report_path}\nPatch:  {res.run_dir / 'patch.diff'}\nPress p for the patch, Ctrl+Q to quit.", style="dim"))
        if res.patch.strip() and (tabs := self._q("#tabs", TabbedContent)):
            tabs.active = "patch"
        self.notify(title, severity="information" if res.verified else "warning", timeout=8)

    def _update_result(self, renderable) -> None:
        if w := self._q("#result", Static):
            w.update(renderable)

    # ------------------------------------------------------------------ periodic rendering
    def _tick(self) -> None:
        self._spin = (self._spin + 1) % len(SPINNER)
        self._render_side()

    def _render_phases(self) -> None:
        w = self._q("#phases", Static)
        if not w:
            return
        cur = PHASES.index(self.phase)
        parts = []
        for i, name in enumerate(PHASES):
            if i < cur or (name == "Done" and i == cur):
                parts.append(f"[green]✓ {name}[/]")
            elif i == cur:
                spin = SPINNER[self._spin] if self.stats.get("busy") else "●"
                parts.append(f"[b reverse cyan] {spin} {name} [/]")
            else:
                parts.append(f"[grey50]○ {name}[/]")
        w.update("  [grey42]→[/]  ".join(parts))

    def _render_side(self) -> None:
        self._render_phases()
        s = self.stats
        t = s.get("tokens") or {}
        prompt, cached, completion = t.get("prompt", 0), t.get("cached", 0), t.get("completion", 0)
        cache_pct = f" ({100 * cached // prompt}% cached)" if prompt and cached else ""
        status = escape(str(s["status"]))
        if s.get("busy"):
            status = f"[cyan]{SPINNER[self._spin]}[/] {status}"
        steps = f"{s['step']}" + (f" / {s['step_limit']}  {bar(s['step'], s['step_limit'], 12)}" if s.get("step_limit") else "")
        grid = Table.grid(padding=(0, 1))
        grid.add_column(style="dim", no_wrap=True)
        grid.add_column()
        grid.add_row("Status", status)
        grid.add_row("Model", escape(str(s["model"])))
        grid.add_row("Actions", escape(s.get("mode") or "-"))
        grid.add_row("Steps", steps)
        grid.add_row("Tokens", f"in {prompt:,}{cache_pct}")
        grid.add_row("", f"out {completion:,}")
        grid.add_row("Cost", f"${s['cost']:.4f}")
        grid.add_row("Elapsed", fmt_duration(time.time() - self._t0))
        if w := self._q("#stats", Static):
            w.update(Panel(grid, title="[b]Run[/b]", border_style="blue"))
        if w := self._q("#verify", Static):
            if self.checks:
                table = Table.grid(padding=(0, 1))
                for c in self.checks:
                    icon, color = CHECK_ICON.get(c.status, ("?", "white"))
                    table.add_row(Text(icon, style=f"bold {color}"), Text(c.name))
                style = "green" if self.verify_passed else "red"
                verdict = "accepted" if self.verify_passed else "rejected"
                w.update(Panel(table, title=f"[b]Verification · round {self.verify_round} · {verdict}[/b]", border_style=style))
            else:
                w.update(Panel(Text("runs when the agent submits:\nrepro fails before, passes after,\nsyntax, regression tests", style="dim"), title="[b]Verification[/b]", border_style="grey35"))
        if w := self._q("#files", Static):
            body = "\n".join(f"[yellow]M[/] {escape(f)}" for f in self.files[:12]) or "[dim]no changes yet[/]"
            w.update(Panel(body, title=f"[b]Changed files ({len(self.files)})[/b]", border_style="grey35"))
