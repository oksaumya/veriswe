"""VeriAgent: mini-swe-agent's DefaultAgent + guards, context masking, and a harness-run verification gate."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from typing import Any

from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.exceptions import FormatError, InterruptAgentFlow, LimitsExceeded, Submitted

from veriswe.context import mask_observations, shrink_for_overflow
from veriswe.environment import SECRET_ENV_VARS
from veriswe.guards import LoopDetector, check_command
from veriswe.models import toolcall_history_to_text
from veriswe.verify import VerificationResult, Verifier, detect_python
from veriswe.workspace import Workspace


class VeriAgentConfig(AgentConfig):
    max_verification_rounds: int = 3
    keep_last_observations: int = 10
    mask_chunk: int = 6
    test_timeout: int = 600
    action_mode: str = "toolcall"


EventHandler = Callable[[str, dict], None]


def _is_overflow(e: Exception) -> bool:
    text = str(e).lower()
    return (
        "ContextWindowExceeded" in type(e).__name__
        or type(e).__name__ == "RequestTooLargeError"
        or "context length" in text
        or "context window" in text
        or "maximum context" in text
    )


def _secrets() -> list[str]:
    return [v for k in SECRET_ENV_VARS if len(v := os.environ.get(k, "")) >= 8]


class VeriAgent(DefaultAgent):
    def __init__(
        self,
        model,
        env,
        workspace: Workspace,
        *,
        on_event: EventHandler | None = None,
        text_model_factory: Callable[[], Any] | None = None,
        **kwargs,
    ):
        super().__init__(model, env, config_class=VeriAgentConfig, **kwargs)
        self.text_model_factory = text_model_factory
        self.tool_parse_failures = 0
        self.ws = workspace
        self.verifier = Verifier(workspace, test_timeout=self.config.test_timeout)
        self.loop = LoopDetector()
        self.on_event = on_event or (lambda kind, data: None)
        self.attempts: list[VerificationResult] = []
        self.final: VerificationResult | None = None
        self.final_status = ""
        self.tokens = {"prompt": 0, "completion": 0, "cached": 0}
        self._overflow_level = 0
        self.extra_template_vars |= {
            "repo": str(workspace.repo),
            "python": detect_python(workspace.repo),
            "action_mode": self.config.action_mode,
        }

    # ------------------------------------------------------------------ events
    def emit(self, kind: str, **data: Any) -> None:
        try:
            self.on_event(kind, {"step": self.n_calls, **data})
        except Exception:  # UI problems must never kill the run
            pass

    # ------------------------------------------------------------------ model
    def step(self) -> list[dict]:
        try:
            return super().step()
        except FormatError as e:
            if (e.messages[0].get("extra") or {}).get("tool_parse_failure"):
                self.tool_parse_failures += 1
                if self.tool_parse_failures >= 2 and self.config.action_mode == "toolcall" and self.text_model_factory:
                    self._switch_to_text_mode()
                    raise FormatError(
                        {
                            "role": "user",
                            "content": (
                                "HARNESS NOTE: native tool calls kept failing at the API, so you now act in TEXT mode. "
                                "From now on reply with a short THOUGHT and then exactly ONE block:\n\n"
                                "```mswea_bash_command\n<command>\n```"
                            ),
                            "extra": {"interrupt_type": "FormatError", "cost": 0.0},
                        }
                    ) from e
            raise

    def _switch_to_text_mode(self) -> None:
        self.model = self.text_model_factory()
        self.config.action_mode = "text"
        self.extra_template_vars["action_mode"] = "text"
        self.messages = toolcall_history_to_text(self.messages)
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = {**self.messages[0], "content": self._render_template(self.config.system_template)}
        self.emit("mode_switch", mode="text")

    def query(self) -> dict:
        over_steps = 0 < self.config.step_limit <= self.n_calls
        over_cost = 0 < self.config.cost_limit <= self.cost
        over_time = 0 < self.config.wall_time_limit_seconds <= int(time.time() - self._start_time)
        if over_steps or over_cost or over_time:
            reason = "StepLimit" if over_steps else ("CostLimit" if over_cost else "TimeLimit")
            self._finish_on_limit(reason)
        self.n_calls += 1
        self.emit("thinking")
        msgs = mask_observations(
            self.messages, keep_last=self.config.keep_last_observations, chunk=self.config.mask_chunk
        )
        message = None
        while message is None:
            try:
                sent = shrink_for_overflow(msgs, self._overflow_level) if self._overflow_level else msgs
                message = self.model.query(sent)
            except InterruptAgentFlow:
                raise  # format errors etc. are normal control flow
            except Exception as e:
                if not _is_overflow(e):
                    self._handle_model_failure(e)
                    raise
                # Too big for the context window or the per-minute token budget: compact harder and retry.
                if self._overflow_level >= 3:
                    self._finish_on_limit("ContextOverflow")
                self._overflow_level += 1
                self.emit("compact", level=self._overflow_level, reason=type(e).__name__)
        self.cost += message.get("extra", {}).get("cost", 0.0)
        self._account_tokens(message)
        self.add_messages(message)
        self.emit(
            "model",
            content=message.get("content") or "",
            actions=[a.get("command", "") for a in message.get("extra", {}).get("actions", [])],
            tokens=dict(self.tokens),
            cost=self.cost,
        )
        return message

    def _handle_model_failure(self, e: Exception) -> None:
        """API died mid-run (after retries): still verify + submit the best work so far."""
        if self.n_calls > 1:
            from veriswe.model_config import account_problem

            self.emit("model_error", error=account_problem(e) or f"{type(e).__name__}: {e}")
            self._finish_on_limit(f"ModelError ({type(e).__name__})")

    def _account_tokens(self, message: dict) -> None:
        usage = (message.get("extra", {}).get("response") or {}).get("usage") or {}
        if not isinstance(usage, dict):
            return
        self.tokens["prompt"] += int(usage.get("prompt_tokens") or 0)
        self.tokens["completion"] += int(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details") or {}
        cached = (
            (details.get("cached_tokens") if isinstance(details, dict) else 0)
            or usage.get("prompt_cache_hit_tokens")  # DeepSeek
            or usage.get("cache_read_input_tokens")  # Anthropic
        )
        self.tokens["cached"] += int(cached or 0)

    # ------------------------------------------------------------------ actions
    def execute_actions(self, message: dict) -> list[dict]:
        outputs = []
        for action in message.get("extra", {}).get("actions", []):
            command = action.get("command", "")
            self.emit("action", command=command)
            if blocked := check_command(command):
                output = {"output": blocked, "returncode": 1, "exception_info": ""}
            else:
                try:
                    output = self.env.execute(action)
                except Submitted:
                    output = self._on_submit()  # raises Submitted if accepted
            if nudge := self.loop.record(command, output.get("output", ""), output.get("returncode")):
                output = {**output, "output": (output.get("output") or "") + "\n\n" + nudge}
            if dropped := message.get("extra", {}).get("dropped_actions"):
                note = f"HARNESS NOTE: only the FIRST of your {dropped + 1} command blocks was executed. Send one command per reply."
                output = {**output, "output": (output.get("output") or "") + "\n\n" + note}
            self.emit(
                "observation",
                command=command,
                output=output.get("output", ""),
                returncode=output.get("returncode"),
                exception=output.get("exception_info", ""),
            )
            outputs.append(output)
        return self.add_messages(*self.model.format_observation_messages(message, outputs, self.get_template_vars()))

    # ------------------------------------------------------------------ verification gate
    def _verify(self) -> VerificationResult:
        round_no = len(self.attempts) + 1
        self.emit("verify_start", round=round_no)
        res = self.verifier.verify(
            round_no,
            allow_missing_repro=round_no >= 2,
            allow_weak_repro=round_no >= 2,
        )
        self.attempts.append(res)
        self.emit("verify", result=res)
        return res

    def _on_submit(self) -> dict:
        try:
            res = self._verify()
        except Exception as e:  # never lose the agent's work because the gate itself broke
            self.emit("verify_error", error=f"{type(e).__name__}: {e}")
            diff = self.ws.diff()
            self.final_status = f"Submitted (verifier error: {type(e).__name__})"
            raise Submitted(
                {"role": "exit", "content": diff, "extra": {"exit_status": self.final_status, "submission": diff}}
            )
        if res.passed:
            self._accept(res, "Submitted (verified)")
        if len(self.attempts) >= self.config.max_verification_rounds:
            self._accept(self._best(), "Submitted (verification incomplete)")
        return {"output": res.feedback, "returncode": 1, "exception_info": ""}

    def _best(self) -> VerificationResult:
        # highest score wins; later attempts win ties (they include more work)
        return max(reversed(self.attempts), key=lambda r: r.score)

    def _restore(self, res: VerificationResult) -> None:
        if self.ws.diff() != res.diff:
            self.ws.restore(res.diff)
            self.emit("restore", round=res.round)

    def _accept(self, res: VerificationResult, status: str, exc_cls=Submitted) -> None:
        self._restore(res)
        self.final, self.final_status = res, status
        self.emit("final", status=status, result=res)
        raise exc_cls(
            {
                "role": "exit",
                "content": res.diff,
                "extra": {"exit_status": status, "submission": res.diff, "verified": res.passed},
            }
        )

    def _finish_on_limit(self, reason: str) -> None:
        """Out of budget: verify the current state, and submit the best patch we have instead of nothing."""
        current = self.ws.diff()
        if current.strip() and (not self.attempts or self.attempts[-1].diff != current):
            self._verify()
        if self.attempts:
            best = self._best()
            status = f"AutoSubmitted after {reason}" + (" (verified)" if best.passed else " (unverified)")
            self._accept(best, status, exc_cls=LimitsExceeded)
        self.final_status = f"Stopped: {reason} (no changes)"
        raise LimitsExceeded(
            {"role": "exit", "content": reason, "extra": {"exit_status": self.final_status, "submission": ""}}
        )

    def save(self, path, *extra_dicts) -> dict:
        data = self.serialize(*extra_dicts)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(data, indent=2, default=str)
            for secret in _secrets():
                text = text.replace(secret, "***REDACTED***")
            path.write_text(text)
        return data

    def serialize(self, *extra_dicts) -> dict:
        return super().serialize(
            {
                "info": {
                    "veriswe": {
                        "tokens": self.tokens,
                        "final_status": self.final_status,
                        "verification_attempts": [a.to_dict() for a in self.attempts],
                    }
                }
            },
            *extra_dicts,
        )
