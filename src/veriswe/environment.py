"""Local execution environment: mini's LocalEnvironment, but with stdin closed and our tools on PATH."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from minisweagent.environments.local import LocalEnvironment, LocalEnvironmentConfig

from veriswe import tools_dir


SECRET_ENV_VARS = {
    "AI_API_KEY",
    "GITHUB_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
}


def scrubbed_environ() -> dict[str, str]:
    """os.environ without credentials: the agent's shell never sees the API key."""
    return {k: v for k, v in os.environ.items() if k not in SECRET_ENV_VARS}


_SHIMS_DIR: Path | None = None


def tool_shims_dir() -> Path:
    """Directory of launchers that run our helper tools with *this* interpreter.

    The tool scripts' `#!/usr/bin/env python3` would pick whatever python3 is on PATH, which may be too old
    or missing on the evaluation machine; the launchers pin them to VeriSWE's own (>= 3.10) Python.
    """
    global _SHIMS_DIR
    if _SHIMS_DIR is None or not _SHIMS_DIR.is_dir():
        d = Path(tempfile.mkdtemp(prefix="veriswe-tools-"))
        for tool in sorted(tools_dir.iterdir()):
            if tool.is_file() and not tool.name.startswith((".", "_")):
                shim = d / tool.name
                shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(tool))} "$@"\n')
                shim.chmod(0o755)
        _SHIMS_DIR = d
    return _SHIMS_DIR


class VeriEnvironmentConfig(LocalEnvironmentConfig):
    timeout: int = 180


class VeriEnvironment(LocalEnvironment):
    def __init__(self, **kwargs):
        super().__init__(config_class=VeriEnvironmentConfig, **kwargs)
        path = os.environ.get("PATH", "")
        self.config.env = {**self.config.env, "PATH": f"{tool_shims_dir()}{os.pathsep}{path}"}

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        cwd = cwd or self.config.cwd or os.getcwd()
        try:
            result = _run(command, cwd, scrubbed_environ() | self.config.env, timeout or self.config.timeout)
            output = {"output": result.stdout, "returncode": result.returncode, "exception_info": ""}
        except subprocess.TimeoutExpired as e:
            raw = e.output.decode("utf-8", "replace") if isinstance(e.output, bytes) else (e.output or "")
            output = {
                "output": raw,
                "returncode": -1,
                "exception_info": (
                    f"Command timed out after {e.timeout}s and was killed. Run long commands in a narrower scope "
                    "(e.g. a single test file) or in the background with output redirected to a file."
                ),
                "extra": {"exception_type": "TimeoutExpired"},
            }
        except Exception as e:
            output = {
                "output": "",
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: {e}",
                "extra": {"exception_type": type(e).__name__, "exception": str(e)},
            }
        self._check_finished(output)
        return output


def _run(command: str, cwd: str, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    """subprocess.run with stdin closed (never steal the TUI's terminal) and process-group kill on timeout."""
    process = subprocess.Popen(
        command,
        shell=True,
        executable="/bin/bash" if os.path.exists("/bin/bash") else None,
        text=True,
        cwd=cwd,
        env=env,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL) if os.name == "posix" else process.kill()
        stdout, _ = process.communicate()
        raise subprocess.TimeoutExpired(command, timeout, output=stdout)
    return subprocess.CompletedProcess(command, process.returncode, stdout=stdout)
