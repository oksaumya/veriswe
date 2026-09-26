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
    # Tests are offline: never probe real provider endpoints for generic keys.
    import veriswe.model_config as mc

    mc.__dict__.setdefault("_real_discover_endpoint", mc.discover_endpoint)
    monkeypatch.setattr(mc, "discover_endpoint", lambda *a, **k: None)
    # LiteLLM auto-loads ./.env on import; a developer's local model settings must not leak into tests.
    for var in ("AI_MODEL", "AI_BASE_URL", "AI_PROVIDER", "AI_TEXT_MODE", "AI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
