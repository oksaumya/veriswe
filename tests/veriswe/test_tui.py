"""Headless tests of the Textual TUI: intake form -> run dashboard -> result, with a scripted model."""

import asyncio
from pathlib import Path

import veriswe.runner as runner
from tests.veriswe.test_agent_e2e import GOOD_FIX, REPRO, SUBMIT, script
from veriswe.tui import IntakeScreen, RunScreen, VeriApp, key_status

ISSUE_TEXT = (Path(__file__).parent / "fixtures" / "calc_issue.md").read_text()


def _patch_runner(monkeypatch, *commands):
    real = runner.run_task

    def fake_run_task(issue, repo, **kw):
        kw.pop("model_override", None)
        return real(issue, repo, model=script(*commands), mode="text", model_display="scripted", **kw)

    monkeypatch.setattr(runner, "run_task", fake_run_task)


def test_tui_intake_and_run(calc_repo, monkeypatch, tmp_path):
    _patch_runner(monkeypatch, "search 'def median' calc", REPRO, "check_repro", GOOD_FIX, "check_repro", SUBMIT)

    async def drive():
        app = VeriApp(issue=None, repo=str(calc_repo))
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, IntakeScreen)
            app.screen.query_one("#issue").load_text(ISSUE_TEXT)
            await pilot.press("ctrl+s")
            for _ in range(300):
                await pilot.pause(0.1)
                if app.result is not None:
                    await pilot.pause(0.3)
                    break
            assert isinstance(app.screen, RunScreen)
            app.save_screenshot(filename="tui_done.svg", path=str(tmp_path))
            await pilot.press("p")  # PATCH view
            await pilot.pause(0.3)
            patch_lines = "".join(str(line.text) for line in app.screen.query_one("#patchlog").lines)
            for key in "iavlhr":  # every view opens without errors
                await pilot.press(key)
                await pilot.pause(0.05)
            assert app.active_view == "run"
            return app, patch_lines

    app, patch_text = asyncio.run(drive())
    assert app.result.verified and app.stats["status"] == "VERIFIED"
    assert app.phase == "Done" and app.verify_passed is True
    assert app.stats["step"] == 6 and app.stats["step_limit"] > 0
    assert "calc/stats.py" in app.files
    assert "(data[mid - 1] + data[mid]) / 2" in patch_text  # live/final patch tab shows the fix
    assert (tmp_path / "tui_done.svg").stat().st_size > 1000


def test_tui_intake_validation(monkeypatch):
    async def drive():
        app = VeriApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+s")  # empty issue
            await pilot.pause()
            first = str(app.screen.query_one("#error").render())
            app.screen.query_one("#issue").load_text("some bug description without a repo")
            await pilot.press("ctrl+s")
            await pilot.pause()
            second = str(app.screen.query_one("#error").render())
            still_intake = isinstance(app.screen, IntakeScreen)  # never started a run
            return still_intake, first, second

    still_intake, first, second = asyncio.run(drive())
    assert still_intake
    assert "issue" in first.lower()
    assert "repository" in second.lower()


def test_key_status_never_reveals_the_key(monkeypatch):
    secret = "sk-SUPERSECRETVALUE1234567890"
    monkeypatch.setenv("AI_API_KEY", secret)
    text = key_status()
    assert "set" in text and secret not in text and "SUPERSECRET" not in text
    monkeypatch.delenv("AI_API_KEY")
    assert "not set" in key_status()
