import shutil
import sys
from pathlib import Path

import pytest

collect_ignore_glob = ["fixtures/*"]

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def calc_repo(tmp_path: Path) -> Path:
    dest = tmp_path / "calc"
    shutil.copytree(FIXTURES / "calc", dest)
    return dest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Use the test interpreter for repro/pytest runs and keep run artefacts out of the repo."""
    import veriswe.runner as runner

    monkeypatch.setenv("VERISWE_PYTHON", sys.executable)
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runner, "WORKSPACE_DIR", tmp_path / "workspace")
