"""Model wrappers that turn provider-side tool-call failures into recoverable format errors."""

from __future__ import annotations

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


class _NoBlindRetryMixin:
    # A 400 will fail identically on retry; don't burn minutes of exponential backoff on it.
    abort_exceptions = [*LitellmModel.abort_exceptions, litellm.exceptions.BadRequestError, RequestTooLargeError]
    max_rate_limit_waits = 12

    def _query(self, messages, **kwargs):
        """Honour the provider's Retry-After on rate limits instead of blind exponential backoff."""
        for attempt in range(self.max_rate_limit_waits + 1):
            try:
                return super()._query(messages, **kwargs)
            except litellm.exceptions.RateLimitError as e:
                if is_request_too_large(e):
                    raise RequestTooLargeError(str(e)[:500]) from e
                wait = retry_after_seconds(e)
                if wait is None or attempt == self.max_rate_limit_waits or wait > 300:
                    raise  # fall back to the generic tenacity retry
                wait = min(wait + 0.5, 120)
                logger.warning(f"Retrying query in {wait:.1f} seconds as it raised RateLimitError (server-suggested wait)")
                time.sleep(wait)


class VeriToolcallModel(_NoBlindRetryMixin, LitellmModel):
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
        self._dropped = max(0, len(found) - 1)
        if len(found) > 1:
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
