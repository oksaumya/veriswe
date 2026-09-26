"""End-to-end agent runs with a scripted (deterministic) model: no API key, no network."""

from pathlib import Path

from minisweagent.models.test_models import DeterministicModel, make_output

from veriswe.runner import run_task

ISSUE = (Path(__file__).parent / "fixtures" / "calc_issue.md").read_text()

REPRO = """cat > reproduce_issue.py <<'EOF'
from calc.stats import median
assert median([1, 2, 3, 4]) == 2.5, median([1, 2, 3, 4])
assert median([3, 1, 2]) == 2
print("OK")
EOF"""

GOOD_FIX = """str_replace calc/stats.py <<'EOF'
<<<<<<< SEARCH
    mid = len(data) // 2
    return data[mid]
=======
    mid = len(data) // 2
    if len(data) % 2 == 0:
        return (data[mid - 1] + data[mid]) / 2
    return data[mid]
>>>>>>> REPLACE
EOF"""

# "Fixes" the issue but breaks odd-length medians (a regression the existing tests catch)
BAD_FIX = """str_replace calc/stats.py <<'EOF'
<<<<<<< SEARCH
    mid = len(data) // 2
    return data[mid]
=======
    mid = len(data) // 2
    return (data[mid - 1] + data[mid]) / 2
>>>>>>> REPLACE
EOF"""

UNDO = "undo_edit calc/stats.py"
SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


def script(*commands: str) -> DeterministicModel:
    return DeterministicModel(outputs=[make_output(f"step {i}", [{"command": c}], cost=0.0) for i, c in enumerate(commands)])


def run(repo, model, **overrides):
    events = []
    res = run_task(
        ISSUE,
        str(repo),
        model=model,
        mode="text",
        model_display="scripted",
        on_event=lambda k, d: events.append((k, d)),
        agent_overrides=overrides or None,
    )
    return res, events


def test_happy_path_is_verified(calc_repo):
    model = script("search 'def median' calc", REPRO, "python reproduce_issue.py || true", GOOD_FIX, "python reproduce_issue.py", SUBMIT)
    res, events = run(calc_repo, model)
    assert res.status == "VERIFIED", res.status
    assert res.verified
    assert "(data[mid - 1] + data[mid]) / 2" in res.patch
    assert "reproduce_issue.py" not in res.patch
    final_checks = {c.name: c.status for c in [e[1]["result"] for e in events if e[0] == "verify"][-1].checks}
    assert final_checks["repro_after"] == "pass"
    assert final_checks["repro_before"] == "pass"  # i.e. it FAILED on original code -> bug reproduced
    assert final_checks["tests"] == "pass"
    # artefacts
    assert (res.run_dir / "patch.diff").read_text() == res.patch
    report = res.report_path.read_text()
    assert "Verified by harness:** yes" in report
    assert (res.run_dir / "evidence" / "reproduce_issue.py").exists()
    assert not (calc_repo / "reproduce_issue.py").exists()  # repo left clean
    assert not (calc_repo / ".veriswe").exists()


def test_gate_rejects_empty_and_regressing_patches(calc_repo):
    model = script(
        SUBMIT,  # round 1: empty diff -> rejected
        REPRO,
        BAD_FIX,
        SUBMIT,  # round 2: breaks test_median_odd -> rejected with logs
        UNDO,
        GOOD_FIX,
        SUBMIT,  # round 3: accepted
    )
    res, events = run(calc_repo, model)
    verdicts = [e[1]["result"] for e in events if e[0] == "verify"]
    assert [v.passed for v in verdicts] == [False, False, True]
    assert any(c.name == "tests" and c.status == "fail" and "test_median_odd" in c.summary for c in verdicts[1].checks)
    assert res.verified and res.status == "VERIFIED"


def test_no_objective_evidence_is_never_verified(calc_repo):
    """A correct-looking fix whose only support is 'existing tests still pass' is UNVERIFIED (audit 3.1)."""
    model = script(GOOD_FIX, SUBMIT, SUBMIT, SUBMIT)
    res, events = run(calc_repo, model)
    verdicts = [e[1]["result"] for e in events if e[0] == "verify"]
    assert [v.passed for v in verdicts] == [False, False, False]  # never relaxed in later rounds
    assert "No objective evidence" in verdicts[0].feedback
    assert res.status.startswith("UNVERIFIED") and not res.verified
    assert "(data[mid - 1] + data[mid]) / 2" in res.patch  # patch preserved for inspection
    assert "UNVERIFIED" in res.report_path.read_text()


def test_new_test_that_fails_before_counts_as_evidence(calc_repo):
    """Feature/test-fix flow: a new test that fails on the original code and passes now is objective evidence."""
    add_test = """cat >> tests/test_stats.py <<'EOF'


def test_median_even():
    assert median([1, 2, 3, 4]) == 2.5
EOF"""
    model = script(add_test, GOOD_FIX, SUBMIT)
    res, events = run(calc_repo, model)
    final = [e[1]["result"] for e in events if e[0] == "verify"][-1]
    ev = next(c for c in final.checks if c.name == "evidence")
    assert res.verified and ev.status == "pass" and "test_median_even" in ev.summary


def test_declared_refactor_is_verified_by_preserved_behaviour(calc_repo):
    refactor = """str_replace calc/stats.py <<'EOF'
<<<<<<< SEARCH
    values = list(values)
    if not values:
=======
    values = [v for v in values]
    if len(values) == 0:
>>>>>>> REPLACE
EOF"""
    model = DeterministicModel(outputs=[make_output("TASK TYPE: refactor", [{"command": refactor}], cost=0.0),
                                        make_output("done", [{"command": SUBMIT}], cost=0.0)])  # fmt: skip
    res, events = run(calc_repo, model)
    assert res.verified, res.status
    assert res.stats["telemetry"]["task_type"] == "refactor"


def test_verifier_crash_is_not_a_submission(calc_repo, monkeypatch):
    """Audit 3.2: a crashing verifier yields VERIFICATION ERROR feedback, never an accepted patch."""
    from veriswe.verify import Verifier

    calls = {"n": 0}
    real = Verifier.verify

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk on fire")
        return real(self, *a, **k)

    monkeypatch.setattr(Verifier, "verify", flaky)
    model = script(REPRO, GOOD_FIX, SUBMIT, SUBMIT)
    res, events = run(calc_repo, model)
    verdicts = [e[1]["result"] for e in events if e[0] == "verify"]
    assert verdicts[0].passed is False and verdicts[0].checks[0].name == "verifier"
    assert "VERIFICATION ERROR" in verdicts[0].feedback
    assert verdicts[1].passed and res.verified  # accepted only after a real verification


def test_telemetry_artifacts(calc_repo):
    import json

    model = script(REPRO, BAD_FIX, SUBMIT, UNDO, GOOD_FIX, SUBMIT)
    res, _ = run(calc_repo, model)
    summary = json.loads((res.run_dir / "telemetry_summary.json").read_text())
    events = [json.loads(line) for line in (res.run_dir / "telemetry.jsonl").read_text().splitlines()]
    assert summary["verified"] and summary["verification_rounds"] == 2 and summary["verification_failures"] == 1
    assert summary["tool_calls"] == 6 and summary["model_calls"] == 6
    assert summary["recovery_events"] >= 1  # the rejected round the agent recovered from
    assert summary["context_chars_before"] >= summary["context_chars_sent"] > 0
    kinds = {e["event"] for e in events}
    assert {"issue", "model", "action", "observation", "verify", "done"} <= kinds
    assert "AGENT EXECUTION" in res.report_path.read_text()


def test_step_limit_autosubmits_best_patch(calc_repo):
    model = script(REPRO, GOOD_FIX, "ls", "ls", "ls")
    res, _ = run(calc_repo, model, step_limit=3)
    assert res.status == "VERIFIED (best patch, stopped by StepLimit)", res.status
    assert "(data[mid - 1] + data[mid]) / 2" in res.patch
    assert res.verified


def test_best_checkpoint_is_restored(calc_repo):
    # Good verified-ish attempt is rejected only for a missing repro; later attempts get worse -> restore the good one.
    model = script(GOOD_FIX, SUBMIT, UNDO, BAD_FIX, REPRO, SUBMIT, "echo hi", SUBMIT)
    res, events = run(calc_repo, model, max_verification_rounds=3)
    assert "if len(data) % 2 == 0" in res.patch, res.patch


def test_blocked_command_does_not_run(calc_repo):
    model = script("git stash", REPRO, GOOD_FIX, SUBMIT)
    res, events = run(calc_repo, model)
    obs = [d for k, d in events if k == "observation"]
    assert "BLOCKED" in obs[0]["output"]
    assert res.verified


def test_api_key_never_reaches_shell_or_trajectory(calc_repo, monkeypatch):
    secret = "sk-test-SECRET-1234567890"
    monkeypatch.setenv("AI_API_KEY", secret)
    model = script("echo KEY=$AI_API_KEY", REPRO, GOOD_FIX, SUBMIT)
    res, events = run(calc_repo, model)
    obs = [d for k, d in events if k == "observation"]
    assert secret not in obs[0]["output"]
    assert secret not in (res.run_dir / "trajectory.json").read_text()


def test_format_errors_are_retried_not_fatal(calc_repo):
    bad = make_output("no action here", [], cost=0.0)
    model = script(REPRO, GOOD_FIX, SUBMIT)
    model.config.outputs.insert(0, bad)

    # DeterministicModel returns outputs verbatim; emulate the parse failure the real model raises
    from minisweagent.exceptions import FormatError

    orig = model.query

    def query(messages, **kw):
        out = orig(messages, **kw)
        if not out["extra"]["actions"]:
            raise FormatError({"role": "user", "content": "Format error: provide one action", "extra": {"interrupt_type": "FormatError"}})
        return out

    model.query = query
    res, _ = run(calc_repo, model)
    assert res.verified


def test_model_crash_midrun_autosubmits(calc_repo):
    model = script(REPRO, GOOD_FIX)
    orig = model.query

    def query(messages, **kw):
        if model.current_index >= 1:
            raise RuntimeError("API down")
        return orig(messages, **kw)

    model.query = query
    res, _ = run(calc_repo, model)
    assert res.status.startswith("VERIFIED (best patch, stopped by ModelError"), res.status
    assert res.verified


def test_switches_to_text_mode_after_tool_parse_failures(calc_repo):
    from minisweagent.exceptions import FormatError

    toolcall = script(REPRO)
    text = script(GOOD_FIX, SUBMIT)
    calls = {"n": 0}
    orig = toolcall.query

    def flaky(messages, **kw):
        calls["n"] += 1
        if calls["n"] in (2, 3):
            raise FormatError({"role": "user", "content": "bad json", "extra": {"interrupt_type": "FormatError", "tool_parse_failure": True}})
        return orig(messages, **kw)

    toolcall.query = flaky
    events = []
    import veriswe.runner as runner

    real_agent = runner.VeriAgent

    class Patched(real_agent):
        def __init__(self, *a, **kw):
            kw["text_model_factory"] = lambda: text
            super().__init__(*a, **kw)

    runner.VeriAgent = Patched
    try:
        res = run_task(ISSUE, str(calc_repo), model=toolcall, mode="toolcall", model_display="x", on_event=lambda k, d: events.append(k))
    finally:
        runner.VeriAgent = real_agent
    assert "mode_switch" in events
    assert res.verified


def test_oversized_request_compacts_instead_of_dying(calc_repo):
    from veriswe.models import RequestTooLargeError, is_request_too_large

    assert is_request_too_large(Exception("tokens per minute (TPM): Limit 8000, Requested 8554, please reduce"))
    assert not is_request_too_large(Exception("TPM: Limit 8000, Used 7000, Requested 900"))
    model = script(REPRO, GOOD_FIX, SUBMIT)
    orig = model.query
    state = {"raised": 0}

    def query(messages, **kw):
        if model.current_index == 0 and state["raised"] < 2:  # 2nd call is "too large" twice
            state["raised"] += 1
            raise RequestTooLargeError("Limit 8000, Requested 8554")
        return orig(messages, **kw)

    model.query = query
    res, events = run(calc_repo, model)
    assert [d["level"] for k, d in events if k == "compact"] == [1, 2]
    assert res.verified


def test_regression_tests_run_even_if_project_python_lacks_pytest(calc_repo, tmp_path, monkeypatch):
    # Project interpreter = a Python venv without pytest -> verifier must fall back and still run the tests.
    import subprocess
    import sys

    venv = tmp_path / "nopytest"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    monkeypatch.setenv("VERISWE_PYTHON", str(venv / "bin" / "python"))
    res, events = run(calc_repo, script(REPRO, GOOD_FIX, SUBMIT))
    tests = next(c for c in [d["result"] for k, d in events if k == "verify"][-1].checks if c.name == "tests")
    assert tests.status == "pass", tests.summary
    assert res.verified


def test_check_repro_tool_matches_the_gate(calc_repo):
    weak = "cat > reproduce_issue.py <<'EOF'\nfrom calc.stats import median\nassert median([3, 1, 2]) == 2\nEOF"
    model = script(REPRO, "check_repro", GOOD_FIX, "check_repro", weak, "check_repro", REPRO, SUBMIT)
    res, events = run(calc_repo, model)
    obs = [d["output"] for k, d in events if k == "observation"]
    assert "FAILS as it should" in obs[1]  # before any fix
    assert "VERDICT: OK" in obs[3]  # after the fix
    assert "PASSES (bad: does not reproduce" in obs[5]  # weak reproduction detected
    assert "(data[mid - 1] + data[mid]) / 2" in res.patch  # the fix survived all the undo/redo
    assert res.verified


def test_daily_quota_exhaustion_autosubmits_immediately(calc_repo):
    import time

    from veriswe.models import QuotaExhaustedError

    model = script(REPRO, GOOD_FIX)
    orig = model.query

    def query(messages, **kw):
        if model.current_index >= 1:
            raise QuotaExhaustedError("tokens per day (TPD): Limit 200000, Used 199990")
        return orig(messages, **kw)

    model.query = query
    t = time.time()
    res, _ = run(calc_repo, model)
    assert res.status.startswith("VERIFIED (best patch, stopped by ModelError (QuotaExhaustedError)") and res.verified
    assert time.time() - t < 20


def test_offline_recovery_demo(tmp_path):
    """`make demo-recovery`: failure -> harness rejection -> recovery -> VERIFIED, with telemetry to show it."""
    from veriswe import demo

    repo = demo.prepare_repo(tmp_path / "ws")
    res = run_task(demo.issue_spec(), str(repo), **demo.run_kwargs())
    tel = res.stats["telemetry"]
    assert res.verified and res.status == "VERIFIED"
    assert tel["verification_rounds"] == 2 and tel["verification_failures"] == 1
    assert tel["recovery_events"] >= 1 and tel["task_type"] == "bug"
    assert "if len(data) % 2 == 0" in res.patch


def test_context_reduction_is_measured_on_large_outputs(calc_repo):
    from veriswe.runner import load_agent_yaml

    big = "python3 -c \"print('x' * 30000)\""
    model = script(big, big, REPRO, GOOD_FIX, SUBMIT)
    model.config.observation_template = load_agent_yaml()["agent"]["observation_template"]  # the real (truncating) one
    res, _ = run(calc_repo, model)
    tel = res.stats["telemetry"]
    assert tel["context_chars_reduced"] > 40_000  # head/tail truncation of 2 x 30K outputs, over later calls
    assert tel["context_chars_sent"] < tel["context_chars_before"]
