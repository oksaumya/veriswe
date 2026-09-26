"""Headless smoke test of the Textual TUI: intake form -> run screen -> result, with a scripted model."""

import asyncio
from pathlib import Path

import veriswe.runner as runner
from tests.veriswe.test_agent_e2e import GOOD_FIX, REPRO, SUBMIT, script
from veriswe.tui import RunScreen, VeriApp


def test_tui_intake_and_run(calc_repo, monkeypatch):
    real = runner.run_task

    def fake_run_task(issue, repo, **kw):
        kw.pop("model_override", None)
        return real(issue, repo, model=script(REPRO, GOOD_FIX, SUBMIT), mode="text", model_display="scripted", **kw)

    monkeypatch.setattr(runner, "run_task", fake_run_task)
    issue_text = (Path(__file__).parent / "fixtures" / "calc_issue.md").read_text()

    async def drive():
        app = VeriApp(issue=None, repo=str(calc_repo))
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app.screen.query_one("#issue").load_text(issue_text)
            await pilot.press("ctrl+s")
            for _ in range(300):
                await pilot.pause(0.1)
                if str(app.stats.get("status", "")).startswith("Submitted"):
                    await pilot.pause(0.5)
                    break
            assert isinstance(app.screen, RunScreen)
            return dict(app.stats)

    stats = asyncio.run(drive())
    assert stats["status"].startswith("Submitted (verified)"), stats
    assert stats["step"] == 3
