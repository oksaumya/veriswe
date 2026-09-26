"""Textual TUI: intake form -> mission-control dashboard (phases, issue, timeline, verification gate) -> result."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime

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
from textual.widgets import Button, ContentSwitcher, Input, Label, Markdown, RichLog, Static, TextArea

from veriswe import __version__
from veriswe.intake import issue_names_repo

# ---------------------------------------------------------------- palette
BG, PANEL, EDGE = "#0b1220", "#0f1a2b", "#1f6feb"
BLUE, CYAN, GREEN, RED, AMBER, GREY, WHITE = "#58a6ff", "#79c0ff", "#3fb950", "#ff6b81", "#f0b429", "#8b949e", "#e6edf3"
DIM_EDGE = "#30363d"

PHASES = ["Setup", "Explore", "Reproduce", "Fix", "Verify", "Done"]
CHECK_ICON = {"pass": ("✓", GREEN), "fail": ("✗", RED), "warn": ("!", AMBER), "skip": ("–", GREY)}
GATE_ORDER = ["diff", "syntax", "repro_before", "repro_after", "tests", "evidence"]
GATE_HELP = {
    "diff": "Changes exist against the original code.",
    "syntax": "Every changed file still parses.",
    "repro_before": "Evidence FAILS on the original code (problem is real).",
    "repro_after": "Evidence PASSES with the change.",
    "tests": "Related tests: no regressions vs. the original code.",
    "evidence": "Objective proof: fails before, passes after.",
}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
NAV = [("run", "▷", "RUN", "r"), ("issue", "▤", "ISSUE", "i"), ("agent", "◉", "AGENT", "a"), ("patch", "⑂", "PATCH", "p"),
       ("verify", "⛨", "VERIFY", "v"), ("logs", "≣", "LOGS", "l"), ("help", "?", "HELP", "h")]  # fmt: skip
EXPLORE_CMD = re.compile(r"^\s*(search|view|grep|rg|ls|find|cat|head|tail|nl|tree|git\s+(status|diff|grep|ls-files))\b")
TEST_CMD = re.compile(r"\b(pytest|unittest|tox|npm\s+test|go\s+test|cargo\s+test|mvn\s+test|gradle\s+test)\b")
RECOVERY_LABELS = {
    "api_retry": "API retry",
    "compact": "Context compacted",
    "mode_switch": "Switched to text actions",
    "restore": "Restored best checkpoint",
    "salvaged": "Recovered a rejected tool call",
    "format_error": "Malformed reply recovered",
    "loop_nudge": "Loop detected - nudged",
    "blocked": "Blocked an unsafe command",
    "model_error": "Model API error",
}
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
        return f"[{RED}]✗ AI_API_KEY is not set[/]  - run [b]export AI_API_KEY=...[/b] before [b]make run[/b]"
    from veriswe.model_config import detect_provider

    host = detect_provider(key)
    where = "auto-detected at start (DeepSeek, Qwen/DashScope, SiliconFlow, OpenAI, ...)" if host == "openai" else host
    model = os.getenv("AI_MODEL", "").strip() or "best available DeepSeek/Qwen model"
    return f"[{GREEN}]✓ AI_API_KEY is set[/]  [{GREY}]host:[/] {escape(where)}  [{GREY}]model:[/] {escape(model)}"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def diff_stats(diff: str) -> dict[str, list[int]]:
    """{file: [added, removed]} from a unified diff."""
    stats: dict[str, list[int]] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            stats.setdefault(current, [0, 0])
        elif current and line.startswith("+") and not line.startswith("+++"):
            stats[current][0] += 1
        elif current and line.startswith("-") and not line.startswith("---"):
            stats[current][1] += 1
    return stats


def first_line(text: str, limit: int = 90) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit] + ("…" if len(line) > limit else "")
    return ""


def section(title: str, body, *, color: str = BLUE, subtitle: str = "", edge: str = EDGE) -> Panel:
    return Panel(body, title=f"[b {color}]{title}[/]", title_align="left", subtitle=subtitle or None,
                 subtitle_align="right", border_style=edge, padding=(0, 1), style=f"on {PANEL}")  # fmt: skip


# ====================================================================== intake
class IntakeScreen(Screen):
    BINDINGS = [Binding("ctrl+s", "start", "Solve issue", priority=True), Binding("ctrl+q", "app.quit", "Quit")]

    def __init__(self, issue: str | None, repo: str | None):
        super().__init__()
        self._issue = issue or ""
        self._repo = repo or ""

    def compose(self) -> ComposeResult:
        yield Static(f"[b {BLUE}]VeriSWE v{__version__}[/] [{WHITE}]– Verification-First Coding Agent[/]", id="topbar")
        with Vertical(id="intake"):
            yield Static(Text(BANNER.strip("\n"), style=f"bold {CYAN}"), id="banner")
            yield Static(f"[b]Verification-first autonomous coding agent[/b]  [{GREY}]- explore, change, and PROVE it[/]", id="tagline")
            yield Static(key_status(), id="keystatus")
            yield Label(f"[b {BLUE}]1. TASK / ISSUE[/]  [{GREY}]GitHub issue URL, owner/repo#123, or any task in plain text[/]")
            yield TextArea(self._issue, id="issue")
            yield Label(f"[b {BLUE}]2. REPOSITORY[/]  [{GREY}]local path or git URL - not needed for a GitHub issue URL[/]")
            yield Input(value=self._repo, placeholder="/path/to/repo   or   https://github.com/owner/repo", id="repo")
            with Horizontal(id="buttons"):
                yield Button("▶  SOLVE   (Ctrl+S)", variant="primary", id="start")
            yield Label("", id="error")
            yield Static(f"[{GREY}]Examples:  https://github.com/owner/repo/issues/42   ·   owner/repo#42   ·   "
                         "\"Add a --json flag to the CLI\" + repository path[/]", id="examples")  # fmt: skip
        yield Static(f" [{AMBER}]Ctrl+S[/] solve    [{AMBER}]Ctrl+Q[/] quit", id="footerbar")

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
            err.update(f"[{RED}]✗ Please provide an issue URL or the task/issue text.[/]")
            return
        if not repo and not issue_names_repo(issue):
            err.update(f"[{RED}]✗ Please enter the repository (local path or git URL), or give a GitHub issue URL.[/]")
            return
        self.app.start_run(issue, repo or None)


HELP_TEXT = Panel(
    Group(
        Text.from_markup(f"[b {BLUE}]How VeriSWE works[/]\n"),
        Text("The model proposes; the HARNESS decides. When the agent says it is done, the verification gate re-runs the\n"
             "evidence on the ORIGINAL and the CHANGED code and runs related tests. A patch is VERIFIED only with objective\n"
             "evidence (something that fails before and passes after, no regressions); otherwise it is UNVERIFIED and kept\n"
             "for inspection only.\n"),  # fmt: skip
        Text.from_markup(
            f"[b {BLUE}]Keys[/]\n  [b {AMBER}]r[/] run dashboard   [b {AMBER}]i[/] issue   [b {AMBER}]a[/] agent activity   "
            f"[b {AMBER}]p[/]/[b {AMBER}]d[/] patch   [b {AMBER}]v[/] verification details   [b {AMBER}]l[/] event log   "
            f"[b {AMBER}]f[/] follow on/off   [b {AMBER}]q[/] quit\n"
        ),
        Text.from_markup(f"[b {BLUE}]Artifacts[/] (runs/<run>/)\n  report.md · patch.diff · telemetry.jsonl · "
                         "telemetry_summary.json · trajectory.json · evidence/"),  # fmt: skip
    ),
    title=f"[b {BLUE}]HELP[/]",
    title_align="left",
    border_style=EDGE,
    padding=(1, 2),
)


# ====================================================================== run dashboard
class RunScreen(Screen):
    BINDINGS = [
        Binding("r", "view('run')", "Run"),
        Binding("i", "view('issue')", "Issue"),
        Binding("a", "view('agent')", "Agent"),
        Binding("p", "view('patch')", "Patch"),
        Binding("d", "view('patch')", "Diff", show=False),
        Binding("v", "view('verify')", "Verify"),
        Binding("l", "view('logs')", "Logs"),
        Binding("h", "view('help')", "Help"),
        Binding("f", "toggle_follow", "Follow", show=False),
        Binding("q", "app.quit", "Quit"),
        Binding("ctrl+q", "app.quit", "Quit", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        yield Static(id="phases")
        with Horizontal(id="body"):
            with Vertical(id="nav"):
                yield Static(id="navlist")
                yield Static(id="runinfo")
            with ContentSwitcher(initial="run", id="views"):
                with Horizontal(id="run"):
                    with VerticalScroll(id="center"):
                        yield Static(id="issuectx")
                        yield Static(id="timeline")
                        yield Static(id="decision")
                    with VerticalScroll(id="right"):
                        yield Static(id="gate")
                        yield Static(id="patchstatus")
                        with Horizontal(id="claims"):
                            yield Static(id="claim")
                            yield Static(id="evidence")
                        yield Static(id="statusbar")
                with VerticalScroll(id="issue"):
                    yield Markdown("*waiting for the issue...*", id="issuemd")
                yield RichLog(id="agent", wrap=True, markup=True, highlight=False, auto_scroll=True)
                yield RichLog(id="patchlog", wrap=True, markup=True, highlight=False, auto_scroll=False)
                yield RichLog(id="verifylog", wrap=True, markup=True, highlight=False, auto_scroll=True)
                yield RichLog(id="eventlog", wrap=True, markup=True, highlight=False, auto_scroll=True)
                yield Static(HELP_TEXT, id="help")
        yield Static(id="footerbar")

    VIEW_WIDGET = {"patch": "patchlog", "verify": "verifylog", "logs": "eventlog"}

    def action_view(self, view: str) -> None:
        self.query_one("#views", ContentSwitcher).current = self.VIEW_WIDGET.get(view, view)
        self.app.active_view = view
        self.app._render_all()

    def action_toggle_follow(self) -> None:
        log = self.query_one("#agent", RichLog)
        log.auto_scroll = not log.auto_scroll
        self.notify(f"Follow {'on' if log.auto_scroll else 'off'}", timeout=2)


class VeriApp(App):
    TITLE = "VeriSWE"
    CSS = f"""
    Screen {{ background: {BG}; }}
    #topbar {{ height: 3; padding: 0 1; border: round {EDGE}; background: {PANEL}; content-align: left middle; }}
    #phases {{ height: 3; }}
    #footerbar {{ height: 3; padding: 0 1; border: round {EDGE}; background: {PANEL}; content-align: left middle; }}
    #body {{ height: 1fr; }}
    #nav {{ width: 30; border: round {EDGE}; background: {PANEL}; padding: 0 1; }}
    #navlist {{ height: auto; }}
    #runinfo {{ height: auto; margin-top: 1; }}
    #views {{ width: 1fr; }}
    #run {{ height: 1fr; }}
    #center {{ width: 1fr; margin: 0 1; scrollbar-size: 1 1; }}
    #right {{ width: 1fr; scrollbar-size: 1 1; }}
    #issuectx, #timeline, #decision, #gate, #patchstatus, #statusbar {{ height: auto; }}
    #claims {{ height: auto; }}
    #claim, #evidence {{ width: 1fr; height: auto; }}
    #issue, #agent, #patchlog, #verifylog, #eventlog, #help {{ border: round {EDGE}; background: {PANEL}; margin-left: 1; padding: 0 1; }}
    #intake {{ padding: 0 2; }}
    #banner {{ height: auto; margin-top: 1; }}
    #tagline {{ margin-bottom: 1; }}
    #keystatus {{ border: round {EDGE}; padding: 0 1; margin-bottom: 1; height: auto; background: {PANEL}; }}
    TextArea#issue {{ height: 1fr; min-height: 6; margin-bottom: 1; }}
    #repo {{ margin-bottom: 1; }}
    #buttons {{ height: auto; }}
    #error {{ margin-top: 1; height: auto; }}
    #examples {{ margin-top: 1; }}
    """

    def __init__(self, issue=None, repo=None, model=None, agent_overrides=None, replay: dict | None = None):
        super().__init__()
        self._issue, self._repo, self._model = issue, repo, model
        self._overrides = agent_overrides
        self._replay = replay
        self.stats = {"model": "resolving...", "mode": "", "step": 0, "step_limit": 0, "tokens": {}, "cost": 0.0,
                      "status": "starting", "busy": True}  # fmt: skip
        self.info = {"run_id": "-", "branch": "-", "repo": "-", "started": datetime.now().strftime("%H:%M:%S"), "provider": "-"}
        self.issue = {"title": "", "url": "", "body": ""}
        self.phase = "Setup"
        self.active_view = "run"
        self.timeline: list[dict] = []
        self.checks: list = []
        self.check_map: dict = {}
        self.verify_round = 0
        self.verifying = False
        self.verify_passed: bool | None = None
        self.files: list[str] = []
        self.diff = ""
        self.last_thought = ""
        self.claim: tuple[str, str] | None = None
        self.evidence_lines: tuple[list[str], str] | None = None
        self.result = None
        self._t0 = time.time()
        self._spin = 0
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
        self.info["started"] = datetime.now().strftime("%H:%M:%S")
        self.run_worker(lambda: self._worker(issue, repo), thread=True, exclusive=True)
        self.set_interval(0.2, self._tick)

    def _worker(self, issue: str, repo: str | None) -> None:
        from veriswe.runner import run_task

        try:
            run_task(issue, repo, model_override=self._model, on_event=self._on_event_thread,
                     agent_overrides=self._overrides, **(self._replay or {}))  # fmt: skip
        except Exception as e:  # config / intake errors: show them in the UI
            self.call_from_thread(self._fatal, f"{type(e).__name__}: {e}")

    def _on_event_thread(self, kind: str, data: dict) -> None:
        self.call_from_thread(self._handle, kind, data)

    def _fatal(self, message: str) -> None:
        self.stats.update(status="error", busy=False)
        self._milestone("Could not run", first_line(message, 120), RED)
        self._write("#agent", Panel(escape(message), title="✗ Could not run", border_style=RED))
        self._render_all()

    # ------------------------------------------------------------------ helpers
    def _q(self, selector, kind=None):
        try:
            return self.screen.query_one(selector, kind) if kind else self.screen.query_one(selector)
        except Exception:
            return None

    def _write(self, selector: str, renderable) -> None:
        if log := self._q(selector, RichLog):
            log.write(renderable)

    def _milestone(self, title: str, detail: str = "", color: str = GREEN, merge: bool = False) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        if merge and self.timeline and self.timeline[-1]["title"] == title:
            prev = self.timeline[-1]
            details = [x for x in prev["detail"].split(" · ") if x] + ([detail] if detail else [])
            prev["detail"] = " · ".join(details[-3:])
            prev["time"] = now
            return
        self.timeline.append({"time": now, "title": title, "detail": detail, "color": color})

    def _set_phase(self, phase: str, force: bool = False) -> None:
        if force or PHASES.index(phase) > PHASES.index(self.phase):
            self.phase = phase

    # ------------------------------------------------------------------ events
    def _handle(self, kind: str, d: dict) -> None:
        try:
            self._handle_event(kind, d)
            payload = first_line(str({k: v for k, v in d.items() if k not in ("result", "diff")}), 160)
            self._write("#eventlog", Text.assemble((datetime.now().strftime("%H:%M:%S "), GREY), (f"{kind:<14}", BLUE), (payload, GREY)))
        except Exception as e:  # a rendering bug must never break the run
            self._write("#eventlog", Text(f"ui: {e}", style=RED))
        self._render_all()

    def _handle_event(self, kind: str, d: dict) -> None:
        s = self.stats
        if kind == "model_ready":
            s["model"], s["mode"] = d["model"], d["mode"]
            self.info["provider"] = d["model"].split("/", 1)[0] if "/" in d["model"] else "-"
            self._milestone("Model ready", f"{d['model']} · {d['mode']}", BLUE)
        elif kind == "issue":
            self.issue = {"title": d.get("title") or "", "url": d.get("url") or "", "body": d.get("body") or ""}
            if md := self._q("#issuemd", Markdown):
                md.update(f"# {self.issue['title'] or 'Task'}\n\n{self.issue['url']}\n\n{self.issue['body']}")
        elif kind == "workspace":
            self.info.update(repo=d["repo"], branch=d.get("branch") or "-")
            s["status"] = "working"
            self._milestone("Workspace ready", f"{os.path.basename(d['repo'])} @ {d['base'][:10]}", BLUE)
        elif kind == "run_config":
            s["step_limit"] = d.get("step_limit") or 0
            self.info["run_id"] = os.path.basename(d.get("run_dir", "")) or "-"
        elif kind == "task_type":
            s["task_type"] = d["task_type"]
            self._milestone("Classified task", f"type: {d['task_type']}", CYAN)
        elif kind == "thinking":
            s["status"] = "thinking"
        elif kind == "model":
            s["step"], s["tokens"], s["cost"] = d["step"], d.get("tokens", {}), d.get("cost", 0.0)
            text = (d.get("content") or "").strip()
            if text:
                self.last_thought = text
            self._write("#agent", Rule(f"[b {BLUE}]Step {d['step']}[/]", style=DIM_EDGE, align="left"))
            if text:
                self._write("#agent", Text(text[:1500], style=f"italic {GREY}"))
            s["status"] = "running command"
        elif kind == "action":
            self._on_action(d["command"])
            self._write("#agent", Syntax(d["command"][:2000], "bash", theme="monokai", word_wrap=True, background_color="default"))
        elif kind == "observation":
            self._on_observation(d)
        elif kind == "patch":
            self.files, self.diff = d.get("files") or [], d.get("diff") or ""
            self._show_patch(self.diff)
        elif kind == "verify_start":
            self.verify_round, self.verifying = d["round"], True
            self._set_phase("Verify", force=True)
            s["status"] = f"verifying (round {d['round']})"
            self.claim = (self.last_thought or "The agent declared the task complete.", datetime.now().strftime("%H:%M:%S"))
            self.check_map = {}
            self._milestone("Submitted for verification", f"round {d['round']} - the harness re-checks independently", AMBER)
        elif kind == "verify":
            self._on_verify(d["result"])
        elif kind == "verify_error":
            self._milestone("VERIFICATION ERROR", "patch not accepted; the agent was informed", RED)
        elif kind in RECOVERY_LABELS:
            detail = d.get("detail") or d.get("error") or d.get("reason") or d.get("command") or ""
            color = RED if kind == "model_error" else AMBER
            self._milestone(f"Recovery: {RECOVERY_LABELS[kind]}", first_line(str(detail), 70), color, merge=True)
            if kind == "api_retry":
                s["status"] = f"API: {d['detail']}"
        elif kind == "final":
            s["status"] = d["status"]
        elif kind == "done":
            self._finish(d["result"])

    def _on_action(self, command: str) -> None:
        c = command.strip()
        if "check_repro" in c:
            self._set_phase("Reproduce" if not self._source_edited else "Fix")
        elif "reproduce_issue.py" in c and not self._source_edited:
            self._set_phase("Reproduce")
            self._milestone("Wrote reproduction", "reproduce_issue.py asserts the expected behaviour", CYAN)
        elif m := re.search(r"\b(str_replace|undo_edit)\s+(\S+)", c):
            target = m.group(2)
            if target.endswith("reproduce_issue.py"):
                return
            self._source_edited = True
            self._set_phase("Fix")
            if m.group(1) == "undo_edit":
                self._milestone("Reverted edit", f"{target} - changing strategy", AMBER)
            else:
                self._milestone("Applied patch", target, GREEN, merge=True)
        elif TEST_CMD.search(c):
            self._milestone("Ran tests", first_line(c, 70), CYAN)
        elif EXPLORE_CMD.search(c):
            self._set_phase("Explore")
            self._milestone("Explored code", first_line(c, 48), GREEN, merge=True)

    def _on_observation(self, d: dict) -> None:
        out = (d.get("output") or "").rstrip()
        rc = d.get("returncode")
        cmd = d.get("command", "")
        badge = Text(" ✓ " if rc == 0 else f" rc={rc} ", style=f"bold black on {GREEN}" if rc == 0 else f"bold white on {RED}")
        snippet = out if len(out) < 1400 else out[:700] + "\n[…]\n" + out[-500:]
        self._write("#agent", Text.assemble(badge, " ", Text(snippet or "(no output)", style=GREY)))
        if "check_repro" in cmd:
            if "FAILS as it should" in out or "FAILS (good" in out:
                self._milestone("Reproduced failure", "evidence fails on the ORIGINAL code", GREEN)
            if "VERDICT: OK" in out:
                self._milestone("Evidence passes", "fails before · passes after", GREEN)
            elif "VERDICT:" in out:
                self._milestone("Evidence not OK yet", first_line(out.split("VERDICT:")[1], 70), AMBER)
        elif TEST_CMD.search(cmd) and self.timeline and self.timeline[-1]["title"] == "Ran tests":
            summary = next((ln.strip() for ln in reversed(out.splitlines()) if re.search(r"passed|failed|error", ln)), "")
            self.timeline[-1]["detail"] = first_line(summary or f"rc={rc}", 70)
            self.timeline[-1]["color"] = GREEN if rc == 0 else RED
        elif self.timeline and self.timeline[-1]["title"] == "Applied patch" and self.diff and rc == 0:
            st = diff_stats(self.diff)
            self.timeline[-1]["detail"] = " · ".join(f"{f} (+{a} -{r})" for f, (a, r) in list(st.items())[:2])

    def _on_verify(self, r) -> None:
        self.verifying = False
        self.checks, self.verify_passed = r.checks, r.passed
        self.check_map = {c.name: c for c in r.checks}
        ev = self.check_map.get("evidence")
        if r.passed:
            lines = [e for e in (ev.summary.split("; ") if ev else []) if e]
        else:
            lines = [f"{c.name}: {c.summary}" for c in r.checks if c.status == "fail"]
        self.evidence_lines = (lines or ["(no details)"], datetime.now().strftime("%H:%M:%S"))
        failed = [c.name for c in r.checks if c.status == "fail"]
        if r.passed:
            self._milestone(f"Gate ACCEPTED (round {r.round})", "objective evidence confirmed by the harness", GREEN)
        else:
            self._set_phase("Fix", force=True)
            self._milestone(f"Gate REJECTED (round {r.round})", "failed: " + ", ".join(failed) + " - agent must recover", RED)
        table = Table.grid(padding=(0, 1))
        for c in r.checks:
            icon, color = CHECK_ICON.get(c.status, ("?", WHITE))
            table.add_row(Text(icon, style=f"bold {color}"), Text(c.name, style="bold"), Text(c.summary))
        verdict = Text(" ACCEPTED ", style=f"bold black on {GREEN}") if r.passed else Text(" REJECTED ", style=f"bold white on {RED}")
        self._write("#verifylog", Group(Rule(f"[b {AMBER}]Round {r.round}[/]", style=AMBER), table, verdict))
        for c in r.checks:
            if c.log:
                self._write("#verifylog", Panel(Text(c.log[-1500:], style=GREY), title=f"{c.name} log", border_style=DIM_EDGE))
        self._write("#agent", Group(table, verdict))

    def _show_patch(self, diff: str) -> None:
        if plog := self._q("#patchlog", RichLog):
            plog.clear()
            plog.write(Syntax(diff, "diff", theme="monokai", word_wrap=True, background_color="default") if diff.strip()
                       else Text("(no changes yet)", style=GREY))  # fmt: skip

    def _finish(self, res) -> None:
        self.result = res
        self.stats.update(status=res.status, busy=False)
        self._set_phase("Done", force=True)
        self.diff = res.patch
        self._show_patch(res.patch)
        tel = res.stats.get("telemetry") or {}
        self._milestone("VERIFIED" if res.verified else "UNVERIFIED",
                        f"{tel.get('model_calls', '?')} model calls · {tel.get('recovery_events', 0)} recoveries · "
                        f"{tel.get('verification_rounds', 0)} verification round(s)", GREEN if res.verified else AMBER)  # fmt: skip
        self._write("#agent", Text(f"Report: {res.report_path}\nPatch:  {res.run_dir / 'patch.diff'}\n"
                                   f"Telemetry: {res.run_dir / 'telemetry.jsonl'}", style=GREY))  # fmt: skip
        self.notify("VERIFIED" if res.verified else "UNVERIFIED - patch kept for inspection",
                    severity="information" if res.verified else "warning", timeout=8)  # fmt: skip

    # ------------------------------------------------------------------ rendering
    def _tick(self) -> None:
        self._spin = (self._spin + 1) % len(SPINNER)
        self._render_all()

    def _render_all(self) -> None:
        for fn in (self._render_top, self._render_phases, self._render_nav, self._render_issue, self._render_timeline,
                   self._render_decision, self._render_gate, self._render_patch_status, self._render_claims, self._render_footer):  # fmt: skip
            try:
                fn()
            except Exception as e:  # keep the dashboard alive; surface the problem in the event log
                self._write("#eventlog", Text(f"render {fn.__name__}: {e}", style=RED))

    def _upd(self, selector: str, renderable) -> None:
        if w := self._q(selector, Static):
            w.update(renderable)

    def _render_top(self) -> None:
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(justify="right")
        grid.add_row(
            Text.from_markup(f"[b {BLUE}]VeriSWE v{__version__}[/] [{WHITE}]– Verification-First Coding Agent[/]"),
            Text.from_markup(f"[{GREY}]Model:[/] {escape(str(self.stats['model']))}  [{GREY}]│  Provider:[/] "
                             f"{escape(self.info['provider'])}  [{GREY}]│  Run:[/] {escape(self.info['run_id'])}"),  # fmt: skip
        )
        self._upd("#topbar", grid)

    def _render_phases(self) -> None:
        cur = PHASES.index(self.phase)
        grid = Table.grid(expand=True, padding=0)
        for _ in PHASES:
            grid.add_column(ratio=1)
        cells = []
        for i, name in enumerate(PHASES):
            label = name.upper()
            if i < cur or (name == "Done" and i == cur):
                txt, border, bg = Text(f"{label}  ✓", style=f"bold {GREEN}", justify="center"), GREEN, PANEL
            elif i == cur:
                dot = SPINNER[self._spin] if self.stats.get("busy") else "●"
                txt, border, bg = Text(f"{label}  {dot}", style="bold white", justify="center"), BLUE, EDGE
            else:
                txt, border, bg = Text(f"{label}  ○", style=GREY, justify="center"), DIM_EDGE, PANEL
            cells.append(Panel(txt, border_style=border, style=f"on {bg}", padding=0))
        grid.add_row(*cells)
        self._upd("#phases", grid)

    def _render_nav(self) -> None:
        rows = []
        for vid, icon, label, key in NAV:
            active = vid == self.active_view
            t = Text(f" {icon}  {label:<11}", style=f"bold white on {EDGE}" if active else WHITE)
            t.append(f"[{key}] ", style=f"bold white on {EDGE}" if active else GREY)
            rows += [t, Text("")]
        self._upd("#navlist", Group(*rows))
        info = Table.grid(padding=(0, 1))
        info.add_column(style=GREY, no_wrap=True)
        info.add_column(overflow="ellipsis", no_wrap=True, max_width=16)
        info.add_row("Run ID", escape(self.info["run_id"]))
        info.add_row("Branch", escape(self.info["branch"]))
        info.add_row("Workspace", escape(os.path.basename(self.info["repo"]) or self.info["repo"]))
        info.add_row("Started", self.info["started"])
        info.add_row("Elapsed", fmt_duration(time.time() - self._t0))
        info.add_row("Steps", f"{self.stats['step']} / {self.stats.get('step_limit') or '-'}")
        info.add_row("Task", escape(self.stats.get("task_type") or "-"))
        self._upd("#runinfo", Group(Rule(style=DIM_EDGE), Text("RUN INFO", style=f"bold {WHITE}"), info))

    def _render_issue(self) -> None:
        body = self.issue["body"].strip()
        title = self.issue["title"]
        if not title:  # plain-text task: first line is the title
            lines = body.splitlines()
            title = (lines[0] if lines else "waiting for the task...").lstrip("# ").strip()
            body = "\n".join(lines[1:]).strip()
        parts = [Text.from_markup(f"[b {WHITE}]◆ {escape(title[:90])}[/]")]
        if self.issue["url"]:
            parts.append(Text(self.issue["url"], style=f"underline {BLUE}"))
        if self.stats.get("task_type"):
            parts.append(Text.from_markup(f"[{GREY}]Task type :[/] [b {CYAN}]{escape(self.stats['task_type'])}[/]"))
        if body:
            excerpt = "\n".join(body.splitlines()[:7])
            parts.append(Panel(Text(excerpt[:700], style=WHITE), border_style=DIM_EDGE, padding=(0, 1)))
        self._upd("#issuectx", section("ISSUE CONTEXT", Group(*parts)))

    def _render_timeline(self) -> None:
        items = self.timeline[-9:]
        body = Table.grid(padding=(0, 1))
        body.add_column(no_wrap=True)
        body.add_column(no_wrap=True, style=GREY)
        body.add_column()
        for n, e in enumerate(items):
            last = n == len(items) - 1
            dot = SPINNER[self._spin] if (last and self.stats.get("busy")) else "●"
            body.add_row(Text(dot, style=e["color"]), e["time"], Text(e["title"], style=f"bold {WHITE}"))
            if e["detail"]:
                body.add_row(Text("│" if not last else " ", style=DIM_EDGE), "", Text(f"→ {e['detail']}", style=f"italic {GREY}"))
        self._upd("#timeline", section("AGENT TIMELINE", body if items else Text("starting...", style=GREY)))

    def _render_decision(self) -> None:
        head = Table.grid(padding=(0, 2))
        head.add_column(no_wrap=True)
        head.add_column()
        thought = " ".join(self.last_thought.split())[:320] or "waiting for the agent..."
        head.add_row(Text("◉ AGENT", style=f"bold {CYAN}"), Text(thought, style=WHITE))
        parts = [head]
        if self.diff.strip():
            lines = [ln for ln in self.diff.splitlines() if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))][:8]
            foot = "  ".join(f"{f} +{a} -{r}" for f, (a, r) in list(diff_stats(self.diff).items())[:3])
            parts.append(Panel(Group(Syntax("\n".join(lines), "diff", theme="monokai", background_color="default"),
                                     Text(foot, style=GREY, justify="right")),  # fmt: skip
                               title=f"[{GREY}]# Applied patch (excerpt)[/]", title_align="left", border_style=DIM_EDGE))
        self._upd("#decision", section("AGENT DECISION", Group(*parts)))

    def _render_gate(self) -> None:
        rows = Table.grid(padding=(0, 1), expand=True)
        rows.add_column(no_wrap=True)
        rows.add_column(ratio=1)
        rows.add_column(no_wrap=True, justify="right")
        names = GATE_ORDER + (["verifier"] if "verifier" in self.check_map else [])
        for n, name in enumerate(names, 1):
            c = self.check_map.get(name)
            if c:
                icon, color = CHECK_ICON.get(c.status, ("?", WHITE))
                state = {"pass": "Completed", "fail": "Failed", "warn": "Warning", "skip": "Skipped"}.get(c.status, c.status)
                detail, frac = c.summary, "1/1" if c.status == "pass" else "0/1"
            elif self.verifying:
                icon, color, state, detail, frac = SPINNER[self._spin], BLUE, "In progress", GATE_HELP.get(name, ""), "0/1"
            else:
                icon, color, state, detail, frac = "○", GREY, "Pending", GATE_HELP.get(name, ""), "0/1"
            rows.add_row(Text(f"{n}. {name.upper()}", style=f"bold {WHITE}"), Text(f"{icon} {state}", style=color), Text(frac, style=GREY))
            rows.add_row("", Text(detail[:150], style=GREY), "")
        intro = Text("Verification is independent of the agent. The agent can propose a patch, "
                     "but the verifier decides whether to accept it.", style=WHITE)  # fmt: skip
        rnd = f"[b {CYAN}] CURRENT ROUND: {self.verify_round or '-'} [/]"
        self._upd("#gate", section("⛨ VERIFICATION GATE", Group(intro, Text(""), rows), subtitle=rnd))

    def _render_patch_status(self) -> None:
        final = self.result.verified if self.result is not None else None
        if final is True or (final is None and self.verify_passed):
            badge, color = f"[b black on {GREEN}] VERIFIED [/]", GREEN
        elif final is False:
            badge, color = f"[b white on {RED}] UNVERIFIED [/]", RED
        elif self.verify_passed is False and not self.verifying:
            badge, color = f"[b white on {RED}] REJECTED · ROUND {self.verify_round} [/]", RED
        elif self.diff.strip():
            badge, color = f"[b black on {AMBER}] PENDING [/]", AMBER
        else:
            badge, color = f"[{GREY}] NO PATCH YET [/]", GREY
        tests = self.check_map.get("tests")
        data = tests.data if tests is not None else {}
        grid = Table(expand=True, box=None, show_header=True, padding=(0, 1))
        for label, c in (("Passed", GREEN), ("Failed", RED), ("Skipped", AMBER), ("Total", WHITE)):
            grid.add_column(Text(label, style=f"bold {c}"))
        if data.get("total") is not None:
            grid.add_row(*(Text(str(data.get(k, 0)), style=f"bold {c}") for k, c in
                           (("passed", GREEN), ("failed", RED), ("skipped", AMBER), ("total", WHITE))))  # fmt: skip
        else:
            grid.add_row(*(Text("-", style=GREY) for _ in range(4)))
        risk, rcolor, why = self._risk()
        risk_row = Table.grid(expand=True)
        risk_row.add_column(ratio=1)
        risk_row.add_column(justify="right", no_wrap=True)
        risk_row.add_row(Group(Text("⚠ RISK / SAFETY", style=f"bold {rcolor}"), Text(why, style=GREY)), Text(risk, style=f"bold {rcolor}"))
        body = Group(
            Panel(grid, title=f"[{BLUE}]Test results[/] [{GREY}](harness run)[/]", title_align="left", border_style=DIM_EDGE),
            Panel(risk_row, border_style=rcolor),
        )
        self._upd("#patchstatus", Panel(body, title=f"[b {color}]PATCH STATUS[/]", title_align="left", subtitle=badge,
                                        subtitle_align="right", border_style=color, style=f"on {PANEL}"))  # fmt: skip
        if final is None:
            if self.verify_passed:
                text, scolor, icon = "VERIFIED - finishing", GREEN, "✓"
            elif self.verify_passed is False and not self.verifying:
                text, scolor, icon = f"ROUND {self.verify_round} REJECTED - the agent is recovering from the failure", RED, "✗"
            elif self.verifying:
                text, scolor, icon = f"VERIFYING - round {self.verify_round} in progress", BLUE, SPINNER[self._spin]
            else:
                text, scolor, icon = "Awaiting the harness verdict", GREY, "…"
        elif final:
            text, scolor, icon = "PATCH STATUS: VERIFIED", GREEN, "✓"
        else:
            text, scolor, icon = "PATCH STATUS: UNVERIFIED  (kept for inspection, not a success)", RED, "✗"
        self._upd("#statusbar", Panel(Text(f"{icon}  {text}", style=f"bold {scolor}"), border_style=scolor, style=f"on {PANEL}"))

    def _risk(self) -> tuple[str, str, str]:
        if (self.result is not None and self.result.verified) or (self.result is None and self.verify_passed):
            return "LOW", GREEN, "Objective evidence verified by the harness; no regressions."
        fails = {c.name for c in self.checks if c.status == "fail"}
        if fails & {"tests", "syntax", "verifier"}:
            return "HIGH", RED, "Regression or broken code detected. Not safe to accept."
        if self.checks:
            return "MEDIUM", AMBER, f"No objective proof yet ({', '.join(sorted(fails)) or 'evidence'}). Not safe to accept."
        if self.diff.strip():
            return "MEDIUM", AMBER, "Patch not verified yet. Not safe to accept."
        return "–", GREY, "Nothing to assess yet."

    def _render_claims(self) -> None:
        if self.claim:
            text, ts = self.claim
            sentences = [x.strip("-• ") for x in re.split(r"(?<=[.!?])\s+|\n", text) if x.strip()][:3]
            claim = Group(*[Text(f"• {x[:80]}", style=WHITE) for x in sentences], Text(f"CLAIM · {ts}", style=GREY, justify="right"))
        else:
            claim = Text("The model's claim appears here when it submits.", style=GREY)
        if self.evidence_lines:
            lines, ts = self.evidence_lines
            color = GREEN if self.verify_passed else RED
            ev = Group(*[Text(f"• {x[:80]}", style=color) for x in lines[:4]], Text(f"EVIDENCE · {ts}", style=GREY, justify="right"))
        else:
            ev = Text("The harness's independent findings appear here.", style=GREY)
        self._upd("#claim", section("◉ AGENT CLAIM", claim, color=CYAN))
        self._upd("#evidence", section("⛨ VERIFIER EVIDENCE", ev, color=GREEN if self.verify_passed else AMBER))

    def _render_footer(self) -> None:
        t = self.stats.get("tokens") or {}
        total, cached = t.get("prompt", 0) + t.get("completion", 0), t.get("cached", 0)
        keys = Text()
        for k, rest in (("r", "un"), ("i", "ssue"), ("a", "gent"), ("p", "atch"), ("v", "erify"), ("d", "iff"), ("l", "og"),
                        ("h", "elp"), ("q", "uit")):  # fmt: skip
            keys.append(f"[{k}]", style=f"bold {AMBER}")
            keys.append(f"{rest}   ", style=WHITE)
        status = escape(str(self.stats["status"]))[:60]
        if self.stats.get("busy"):
            status = f"[{CYAN}]{SPINNER[self._spin]}[/] {status}"
        cache = f" ({100 * cached // t['prompt']}% cached)" if t.get("prompt") and cached else ""
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(justify="right")
        grid.add_row(keys, Text.from_markup(
            f"{status}  [{GREY}]│[/]  Tokens: {total:,}{cache}  [{GREY}]│[/]  Time: {fmt_duration(time.time() - self._t0)}"
            f"  [{GREY}]│[/]  Cost: ${self.stats['cost']:.4f}"))  # fmt: skip
        self._upd("#footerbar", grid)
