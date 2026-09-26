"""Context management: observation masking that stays prompt-cache friendly."""

from __future__ import annotations

import copy


def _is_observation(msg: dict) -> bool:
    extra = msg.get("extra") or {}
    if msg.get("role") == "tool":
        return True
    return msg.get("role") == "user" and ("raw_output" in extra or extra.get("interrupt_type") == "FormatError")


def _content_text(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, list):
        return "".join(part.get("text", "") for part in c if isinstance(part, dict))
    return c or ""


def mask_observations(messages: list[dict], *, keep_last: int = 10, chunk: int = 6) -> list[dict]:
    """Return a copy of `messages` where all but the most recent observations are replaced by a short stub.

    Masking happens in multiples of `chunk`, so the prefix of the prompt only changes once every `chunk`
    steps. That keeps provider-side prompt caching effective while bounding context growth.
    """
    obs = [i for i, m in enumerate(messages) if _is_observation(m)]
    n_mask = max(0, len(obs) - keep_last)
    if chunk > 1:
        n_mask = (n_mask // chunk) * chunk
    if n_mask <= 0:
        return messages
    out = list(messages)
    for i in obs[:n_mask]:
        m = copy.copy(out[i])
        text = _content_text(m)
        if len(text) < 300:  # already tiny; keep verbatim
            continue
        extra = m.get("extra") or {}
        rc = extra.get("returncode", "?")
        n_lines = text.count("\n") + 1
        m["content"] = (
            f"[Output elided to save context: {n_lines} lines, returncode {rc}. "
            f"First line: {text.strip().splitlines()[0][:150] if text.strip() else ''!s}. Re-run the command if needed.]"
        )
        out[i] = m
    return out


def shrink_for_overflow(messages: list[dict], level: int = 1) -> list[dict]:
    """Emergency compaction after a context/size error. Higher levels are more aggressive."""
    keep = {1: 3, 2: 2}.get(level, 1)
    max_turn = {1: 2000, 2: 1000}.get(level, 600)
    max_obs = {1: 6000, 2: 2500}.get(level, 1200)
    out = mask_observations(messages, keep_last=keep, chunk=1)
    trimmed = []
    for i, m in enumerate(out):
        text = _content_text(m)
        limit = max_obs if _is_observation(m) else max_turn
        if i >= 2 and len(text) > limit and m.get("role") in ("assistant", "tool", "user"):
            m = copy.copy(m)
            m["content"] = text[: limit // 2] + "\n[...trimmed to save context...]\n" + text[-limit // 2 :]
        trimmed.append(m)
    return trimmed
