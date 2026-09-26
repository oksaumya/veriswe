"""Offline recovery demo: TASK -> EXPLORE -> ACT -> TEST -> FAILURE -> RECOVER -> VERIFY -> REPORT.

The model's decisions are REPLAYED from a script (no API key, fully reproducible), while everything else is real:
the harness loop, the tools, the project's tests, the verification gate, telemetry and the report. The scripted
agent first makes a plausible-but-wrong fix; the harness gate catches the regression, the agent recovers, and the
final patch is verified with objective evidence.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from minisweagent.models.test_models import DeterministicModel, make_output

from veriswe import repo_root

FIXTURE = repo_root / "tests" / "veriswe" / "fixtures" / "calc"
ISSUE = repo_root / "tests" / "veriswe" / "fixtures" / "calc_issue.md"

_REPRO = """cat > reproduce_issue.py <<'EOF'
from calc.stats import median

# The issue: even-length input must return the mean of the two middle values.
assert median([1, 2, 3, 4]) == 2.5, f"median([1, 2, 3, 4]) = {median([1, 2, 3, 4])}, expected 2.5"
assert median([4, 1, 3, 2]) == 2.5, "unsorted even-length input"
assert median([3, 1, 2]) == 2, "odd-length input must keep working"
print("reproduction OK")
EOF"""

_NAIVE_FIX = """str_replace calc/stats.py <<'EOF'
<<<<<<< SEARCH
    mid = len(data) // 2
    return data[mid]
=======
    mid = len(data) // 2
    return (data[mid - 1] + data[mid]) / 2
>>>>>>> REPLACE
EOF"""

_GOOD_FIX = """str_replace calc/stats.py <<'EOF'
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

SCRIPT: list[tuple[str, str]] = [
    ("TASK TYPE: bug\nThe issue says median() is wrong for even-length inputs. Let me find the implementation.",
     "search 'def median' calc"),
    ("Found it in calc/stats.py. Let me read the function.", "view calc/stats.py"),
    ("It always returns data[mid]. Before touching the code I write a reproduction that asserts the correct behaviour.",
     _REPRO),
    ("Check that the reproduction fails on the original code for the right reason.", "check_repro"),
    ("Confirmed (median([1,2,3,4]) returns 3). Quick fix: average the two middle values.", _NAIVE_FIX),
    ("Submitting.", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ("The harness rejected my patch: test_median_odd now fails - I broke odd-length inputs. "
     "My fix was too broad. Revert it and change strategy.", "undo_edit calc/stats.py"),
    ("Only average when the length is even; keep the middle element otherwise.", _GOOD_FIX),
    ("Re-check the evidence: must fail on the original code and pass on mine.", "check_repro"),
    ("Evidence OK. Run the project's related tests to be sure there is no regression.",
     '"$VERISWE_PYTHON" -m pytest tests/test_stats.py -q -p no:cacheprovider 2>&1 | tail -n 5'),
    ("All green. Submitting for independent verification.", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
]  # fmt: skip


def replay_model() -> DeterministicModel:
    return DeterministicModel(
        outputs=[make_output(thought, [{"command": cmd}], cost=0.0) for thought, cmd in SCRIPT],
        model_name="replay (scripted decisions)",
    )


def prepare_repo(workspace: Path) -> Path:
    dest = workspace / "demo_recovery"
    shutil.rmtree(dest, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE, dest)
    return dest


def issue_spec() -> str:
    return f"@{ISSUE}"


def run_kwargs() -> dict:
    """Arguments for runner.run_task: the replayed model, run on VeriSWE's own interpreter (always has pytest)."""
    import os
    import sys

    os.environ["VERISWE_PYTHON"] = sys.executable
    return {"model": replay_model(), "mode": "toolcall", "model_display": "replay (scripted decisions, real harness)"}
