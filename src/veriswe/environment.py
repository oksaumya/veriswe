"""Local execution environment: mini's LocalEnvironment, but with stdin closed and our tools on PATH."""

from __future__ import annotations

import os
import signal
import subprocess
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


class VeriEnvironmentConfig(LocalEnvironmentConfig):
    timeout: int = 180


class VeriEnvironment(LocalEnvironment):
    def __init__(self, **kwargs):
        super().__init__(config_class=VeriEnvironmentConfig, **kwargs)
        path = os.environ.get("PATH", "")
        self.config.env = {**self.config.env, "PATH": f"{tools_dir}{os.pathsep}{path}"}

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
