"""VeriSWE: a verification-first coding-agent harness built on mini-swe-agent."""

import os
from pathlib import Path

# Keep the upstream banner / first-run wizard out of our UI. Must run before importing minisweagent.
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
os.environ.setdefault("MSWEA_CONFIGURED", "1")
os.environ.setdefault("MSWEA_COST_TRACKING", "ignore_errors")
os.environ.setdefault("LITELLM_LOG", "ERROR")

__version__ = "0.1.0"

package_dir = Path(__file__).resolve().parent
tools_dir = package_dir / "tools"
repo_root = package_dir.parent.parent
config_dir = Path(os.getenv("VERISWE_CONFIG_DIR") or repo_root / "config")


def quiet_litellm() -> None:
    import litellm

    litellm.suppress_debug_info = True
    litellm.set_verbose = False
