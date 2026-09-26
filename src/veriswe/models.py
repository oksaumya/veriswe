"""Model wrappers that turn provider-side tool-call failures into recoverable format errors."""

from __future__ import annotations

import json
import logging
import re
import time

import litellm

from minisweagent.exceptions import FormatError
from minisweagent.models.litellm_model import LitellmModel
from minisweagent.models.litellm_textbased_model import LitellmTextbasedModel

TOOL_PARSE_MARKERS = (
    "failed to parse tool call",
    "tool_use_failed",
    "tool call validation failed",
    "invalid tool call",
    "failed to call a function",
    "function arguments",
    "tool_calls",
)

TOOL_PARSE_FEEDBACK = (
    "Your last tool call could not be parsed by the API (invalid JSON in the arguments). Try again with ONE bash tool "
    "call whose `command` is a plain JSON string. Keep it short: for multi-line file content prefer several smaller "
    "commands, and make sure quotes/newlines are properly escaped inside the JSON string."
)


def is_tool_parse_error(e: Exception) -> bool:
    msg = str(e).lower()
    return isinstance(e, litellm.exceptions.BadRequestError) and any(m in msg for m in TOOL_PARSE_MARKERS)


logger = logging.getLogger("litellm_model")
_RETRY_IN = re.compile(r"try again in (?:(\d+)m)?([\d.]+)(ms|s)", re.I)


def retry_after_seconds(e: Exception) -> float | None:
    """Server-suggested wait from a rate-limit error (Retry-After header or 'try again in 1m2.5s' text)."""
    headers = getattr(getattr(e, "response", None), "headers", None) or {}
    try:
        if (ra := headers.get("retry-after")) is not None:
            return float(ra)
    except (TypeError, ValueError):
        pass
    if m := _RETRY_IN.search(str(e)):
        minutes, value, unit = m.groups()
        secs = float(value) / (1000 if unit.lower() == "ms" else 1)
        return secs + 60 * int(minutes or 0)
    return None


_LIMIT_REQ = re.compile(r"Limit\s*:?\s*(\d+).{0,40}?Requested\s*:?\s*(\d+)", re.I | re.S)


class RequestTooLargeError(Exception):
    """A single request exceeds the provider's per-minute token budget: retrying can never succeed."""


def is_request_too_large(e: Exception) -> bool:
    msg = str(e)
    if "request too large" in msg.lower():
        return True
    m = _LIMIT_REQ.search(msg)
    return bool(m and int(m.group(2)) > int(m.group(1)))


def adapt_to_provider_error(e: Exception, kwargs: dict) -> bool:
    """Adjust request kwargs (in place) for known provider-specific 400s. Returns True if a retry makes sense.

    Qwen on Alibaba DashScope: open-weight Qwen3 models reject non-streaming calls while thinking is on
    ("parameter.enable_thinking must be set to false for non-streaming calls"); thinking-only models reject
    enable_thinking=false; models without thinking reject the parameter entirely.
    """
    msg = str(e).lower()
    if "enable_thinking" not in msg and "notsupportenablethinking" not in msg:
        return False
    extra = dict(kwargs.get("extra_body") or {})
    if ("must be set to false" in msg or "only support stream" in msg) and extra.get("enable_thinking") is not False:
        extra["enable_thinking"] = False
    elif "enable_thinking" in extra:
        extra.pop("enable_thinking")  # "restricted to True" / NotSupportEnableThinking
    else:
        return False
    if extra:
        kwargs["extra_body"] = extra
    else:
        kwargs.pop("extra_body", None)
    return True


TRANSIENT_ERRORS = (
    litellm.exceptions.RateLimitError,
    litellm.exceptions.ServiceUnavailableError,
    litellm.exceptions.InternalServerError,
    litellm.exceptions.APIConnectionError,
    litellm.exceptions.Timeout,
)


def adaptive_completion(model: str, messages: list[dict], kwargs: dict, *, max_waits: int = 8, **call_kwargs):
    """litellm.completion that learns provider quirks and rides out transient errors.

    `kwargs` is updated in place, so later calls reuse any learned fix (e.g. Qwen enable_thinking=false).
    Rate limits wait for the provider's suggested time; other transient errors back off exponentially.
    """
    adaptations = waits = 0
    while True:
        try:
            return litellm.completion(model=model, messages=messages, **call_kwargs, **kwargs)
        except litellm.exceptions.BadRequestError as e:
            if adaptations < 3 and adapt_to_provider_error(e, kwargs):
                adaptations += 1
                continue
            raise
        except TRANSIENT_ERRORS as e:
            if isinstance(e, litellm.exceptions.RateLimitError) and is_request_too_large(e):
                raise RequestTooLargeError(str(e)[:500]) from e
            if waits >= max_waits:
                raise
            suggested = retry_after_seconds(e) if isinstance(e, litellm.exceptions.RateLimitError) else None
            wait = min((suggested + 0.5) if suggested is not None else 2 ** (waits + 1), 90)
            waits += 1
            logger.warning(f"Retrying query in {wait:.1f} seconds as it raised {type(e).__name__}")
            time.sleep(wait)


class _NoBlindRetryMixin:
    # A 400 will fail identically on retry; don't burn minutes of exponential backoff on it.
    abort_exceptions = [*LitellmModel.abort_exceptions, litellm.exceptions.BadRequestError, RequestTooLargeError]
    max_rate_limit_waits = 12

    def _query(self, messages, **kwargs):
        """Honour the provider's Retry-After on rate limits instead of blind exponential backoff."""
        adaptations = 0
        for attempt in range(self.max_rate_limit_waits + 1):
            try:
                return super()._query(messages, **kwargs)
            except litellm.exceptions.BadRequestError as e:
                if adaptations < 2 and adapt_to_provider_error(e, self.config.model_kwargs):
                    adaptations += 1
                    logger.warning(f"Adapted request parameters after provider error: {str(e)[:160]}")
                    continue
                raise
            except litellm.exceptions.RateLimitError as e:
                if is_request_too_large(e):
                    raise RequestTooLargeError(str(e)[:500]) from e
                wait = retry_after_seconds(e)
                if wait is None or attempt == self.max_rate_limit_waits or wait > 300:
                    raise  # fall back to the generic tenacity retry
                wait = min(wait + 0.5, 120)
                logger.warning(f"Retrying query in {wait:.1f} seconds as it raised RateLimitError (server-suggested wait)")
                time.sleep(wait)


BASH_TOOL_NAMES = {"bash", "shell", "execute_bash", "run_bash", "terminal", "run_command", "execute_command", "run_shell_command"}
_LEAKED_PATTERNS = [
    # Qwen3-Coder XML:  <tool_call><function=bash><parameter=command>ls</parameter></function></tool_call>
    re.compile(r"<function=\s*(?P<name>[\w.-]+)\s*>.*?<parameter=\s*(?:command|cmd)\s*>\n?(?P<cmd>.*?)\n?</parameter>", re.S),
    # DeepSeek DSML:  <｜DSML｜invoke name="bash"> <｜DSML｜parameter name="command" ...>ls</｜DSML｜parameter>
    re.compile(r"invoke\s+name=\"(?P<name>[\w.-]+)\".*?parameter\s+name=\"(?:command|cmd)\"[^>]*>(?P<cmd>.*?)</[^>]*parameter>", re.S),
]
_LEAKED_JSON = re.compile(r"<tool_call>\s*(?P<json>\{.*?\})\s*</tool_call>", re.S)
_DS_LEGACY = re.compile(r"tool[▁_ ]sep\W*(?P<name>[\w.-]+)\s*```(?:json)?\s*(?P<json>\{.*?\})\s*```", re.S)


def parse_tool_arguments(raw) -> dict | None:
    """Tolerant JSON for tool arguments: code fences, trailing text, {"arguments": ...}/{"input": ...} wrappers."""
    if isinstance(raw, dict):
        args = raw
    else:
        text = (raw or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        args = None
        for candidate in (text, text[text.find("{") : text.rfind("}") + 1] if "{" in text else ""):
            try:
                args = json.loads(candidate)
                break
            except (ValueError, TypeError):
                continue
        if args is None:
            return None
    for _ in range(2):
        if isinstance(args, dict) and "command" not in args:
            inner = args.get("arguments", args.get("input", args.get("parameters")))
            if isinstance(inner, str):
                inner = parse_tool_arguments(inner)
            if isinstance(inner, dict):
                args = inner
                continue
        break
    if isinstance(args, dict) and "cmd" in args and "command" not in args:
        args["command"] = args["cmd"]
    return args if isinstance(args, dict) and isinstance(args.get("command"), str) else None


def extract_leaked_command(content: str) -> str | None:
    """Find a bash tool call that the provider left inside the text content instead of `tool_calls`."""
    if not content:
        return None
    for pat in _LEAKED_PATTERNS:
        for m in pat.finditer(content):
            if m["name"].lower() in BASH_TOOL_NAMES:
                return m["cmd"].strip()
    for pat in (_LEAKED_JSON, _DS_LEGACY):
        for m in pat.finditer(content):
            obj = parse_tool_arguments(m["json"])
            try:
                outer = json.loads(m["json"])
            except ValueError:
                outer = {}
            name = (m.groupdict().get("name") or outer.get("name") or "bash").lower()
            if obj and name in BASH_TOOL_NAMES:
                return obj["command"].strip()
    return None


class VeriToolcallModel(_NoBlindRetryMixin, LitellmModel):
    def _parse_actions(self, response) -> list[dict]:
        msg = response.choices[0].message
        tool_calls = msg.tool_calls or []
        if not tool_calls and (cmd := extract_leaked_command(msg.content or "")):
            return [{"command": cmd, "leaked_tool_call": True}]
        actions, bad = [], []
        for tc in tool_calls:
            name = (tc.function.name or "").strip().lower()
            args = parse_tool_arguments(tc.function.arguments)
            if name in BASH_TOOL_NAMES and args is not None:
                actions.append({"command": args["command"], "tool_call_id": tc.id})
            else:
                bad.append(tc)
        if actions and not bad:
            return actions
        return super()._parse_actions(response)  # produces mini's standard format-error feedback

    def query(self, messages, **kwargs) -> dict:
        try:
            return super().query(messages, **kwargs)
        except litellm.exceptions.BadRequestError as e:
            if not is_tool_parse_error(e):
                raise
            raise FormatError(
                {
                    "role": "user",
                    "content": TOOL_PARSE_FEEDBACK,
                    "extra": {"interrupt_type": "FormatError", "tool_parse_failure": True, "cost": 0.0, "provider_error": str(e)[:500]},
                }
            ) from e


class VeriTextModel(_NoBlindRetryMixin, LitellmTextbasedModel):
    """Weak models often emit several command blocks; run the first one instead of rejecting the whole reply."""

    def _parse_actions(self, response) -> list[dict]:
        content = response.choices[0].message.content or ""
        found = [a.strip() for a in re.findall(self.config.action_regex, content, re.DOTALL)]
        if not found:
            # Common deviations: a plain ```bash block, or a native tool call leaked into the text.
            found = [a.strip() for a in re.findall(r"```(?:bash|sh|shell)\s*\n(.*?)\n```", content, re.DOTALL)]
            if not found and (cmd := extract_leaked_command(content)):
                found = [cmd]
        self._dropped = max(0, len(found) - 1)
        if found:
            return [{"command": found[0]}]
        return super()._parse_actions(response)

    def query(self, messages, **kwargs) -> dict:
        self._dropped = 0
        message = super().query(messages, **kwargs)
        if self._dropped:
            message["extra"]["dropped_actions"] = self._dropped
        return message


def toolcall_history_to_text(messages: list[dict]) -> list[dict]:
    """Convert a tool-calling conversation into plain-text-action form (used when switching modes mid-run)."""
    out = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            content = (m.get("content") or "").strip()
            for a in (m.get("extra") or {}).get("actions", []):
                content += f"\n\n```mswea_bash_command\n{a.get('command', '')}\n```"
            new = {"role": "assistant", "content": content.strip() or "(no content)", "extra": m.get("extra", {})}
        elif role == "tool":
            new = {"role": "user", "content": m.get("content") or "", "extra": m.get("extra", {})}
        else:
            new = {k: v for k, v in m.items() if k not in ("tool_calls", "tool_call_id")}
        out.append(new)
    return out
