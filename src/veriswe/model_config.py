"""Resolve which model to talk to from config/model.yaml + environment, and probe its capabilities.

The only secret is AI_API_KEY, which is read from the environment and never written anywhere.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from veriswe import config_dir

logger = logging.getLogger("veriswe.model")

# provider -> litellm prefix
LITELLM_PREFIX = {
    "anthropic": "anthropic",
    "openai": "openai",
    "gemini": "gemini",
    "openrouter": "openrouter",
    "groq": "groq",
    "xai": "xai",
    "mistral": "mistral",
    "deepseek": "deepseek",
    "together": "together_ai",
    "dashscope": "dashscope",
    "dashscope_coding": "openai",
    "siliconflow": "openai",
    "fireworks": "fireworks_ai",
    "deepinfra": "deepinfra",
    "novita": "novita",
    "nvidia": "nvidia_nim",
    "huggingface": "openai",
}


class ModelConfigError(RuntimeError):
    pass


@dataclass
class ResolvedModel:
    model_name: str
    """Full litellm model name, e.g. anthropic/claude-sonnet-5"""
    provider: str
    action_mode: str  # auto | toolcall | text
    model_kwargs: dict[str, Any] = field(default_factory=dict)
    base_url: str = ""
    from_default: bool = False
    """True when no model was configured explicitly (then we may auto-select an available one)."""

    @property
    def display(self) -> str:
        return f"{self.model_name}" + (f" @ {self.base_url}" if self.base_url else "")


def detect_provider(api_key: str) -> str:
    """Guess the provider from the key format. Falls back to OpenAI-compatible."""
    k = api_key.strip()
    if k.startswith("sk-ant-"):
        return "anthropic"
    if k.startswith("AIza"):
        return "gemini"
    if k.startswith("sk-or-"):
        return "openrouter"
    if k.startswith("gsk_"):
        return "groq"
    if k.startswith("xai-"):
        return "xai"
    if k.startswith("sk-sp-"):
        return "dashscope_coding"  # Alibaba Model Studio "Coding Plan" key
    if k.startswith("sk-ws"):
        return "dashscope"  # Alibaba Model Studio workspace key (region is probed)
    if k.startswith("sk_"):
        return "novita"
    if k.startswith("nvapi-"):
        return "nvidia"
    if k.startswith("hf_"):
        return "huggingface"
    if k.startswith("fw_"):
        return "fireworks"
    return "openai"  # generic "sk-..." keys (OpenAI, DeepSeek, Qwen/DashScope, SiliconFlow, ...): see discover_endpoint


# Where a generic key may belong, in priority order. DeepSeek + Qwen hosts first (the prescribed model families).
# Each entry: (provider, OpenAI-compatible base URL). Probed with the free `GET /models` call.
ENDPOINT_CANDIDATES = [
    ("deepseek", "https://api.deepseek.com"),
    ("dashscope", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    ("dashscope", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ("dashscope", "https://dashscope-us.aliyuncs.com/compatible-mode/v1"),
    ("dashscope", "https://cn-hongkong.dashscope.aliyuncs.com/compatible-mode/v1"),
    ("siliconflow", "https://api.siliconflow.com/v1"),
    ("siliconflow", "https://api.siliconflow.cn/v1"),
    ("openai", "https://api.openai.com/v1"),
    ("together", "https://api.together.ai/v1"),
    ("fireworks", "https://api.fireworks.ai/inference/v1"),
    ("deepinfra", "https://api.deepinfra.com/v1/openai"),
]
# NOTE: only hosts whose `GET /models` returns 401 for an invalid key belong above (verified 2026-09-26).
# Novita, OpenRouter, NVIDIA and the HF router answer /models without auth, so they would give false positives;
# they are recognised by their distinctive key prefixes instead.
FIXED_BASE_URLS = {  # providers whose endpoint we always pass explicitly (never rely on library defaults)
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "huggingface": "https://router.huggingface.co/v1",
    "novita": "https://api.novita.ai/openai",
    "dashscope_coding": "https://coding-intl.dashscope.aliyuncs.com/v1",
}


@dataclass
class Endpoint:
    provider: str
    base_url: str
    models: list[str]


def discover_endpoint(api_key: str, candidates: list[tuple[str, str]] | None = None, timeout: float = 8) -> Endpoint | None:
    """Find which OpenAI-compatible host accepts this key (probes `GET /models` on all candidates in parallel).

    Returns the highest-priority candidate that answers 200 with a model list, or None.
    """
    import requests
    from concurrent.futures import ThreadPoolExecutor

    candidates = candidates or ENDPOINT_CANDIDATES

    def probe(c):
        provider, url = c
        try:
            r = requests.get(url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
            if r.status_code == 200:
                data = r.json().get("data", [])
                return Endpoint(provider, url, [m["id"] for m in data if isinstance(m, dict) and "id" in m])
        except Exception:
            pass
        return None

    # Return as soon as the highest-priority host that accepts the key is known (don't wait for slow hosts).
    pool = ThreadPoolExecutor(max_workers=len(candidates))
    futures = [pool.submit(probe, c) for c in candidates]
    try:
        for fut in futures:  # in priority order
            if (result := fut.result()) is not None:
                return result
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def load_model_yaml(path: Path | None = None) -> dict:
    path = path or config_dir / "model.yaml"
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _list_models(base_url: str, api_key: str) -> list[str]:
    import requests

    try:
        r = requests.get(
            base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=15
        )
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", []) if "id" in m]
    except Exception as e:  # pragma: no cover - network dependent
        logger.warning(f"Could not list models from {base_url}: {e}")
    return []


PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "xai": "https://api.x.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "together": "https://api.together.xyz/v1",
}
NON_CHAT = re.compile(
    r"whisper|tts|embed|guard|moderation|image|dall-e|audio|realtime|transcri|search|orpheus|allam|babbage|davinci|rerank|ocr|vision-only|sora|veo|imagen"
)
PREFERENCE = [
    # DeepSeek + Qwen first: the model families prescribed for the official evaluation (checked 2026-09-26).
    "deepseek-flash", "deepseek-v4.1", "deepseek-v4-pro", "deepseek-v4",
    "qwen3.8-max", "qwen3.7-max", "qwen3-coder-plus", "qwen3-coder-next", "qwen3-coder-480b", "qwen3.8-plus",
    "qwen3.7-plus", "qwen3-max", "qwen3-coder", "qwen3.8", "qwen3.7", "deepseek-v3", "deepseek",
    "claude-opus", "claude-sonnet", "gpt-5", "gemini-3", "gemini-2.5-pro", "gpt-oss-120b",
    "kimi-k2", "glm-4", "grok-4", "devstral", "codestral", "llama-4", "qwen3", "70b", "gpt-4.1",
    "gpt-4o", "claude", "gemini", "gpt-oss", "llama", "mistral", "qwen",
]  # fmt: skip
SMALL = re.compile(r"mini|nano|lite|flash-8b|small|haiku|[^0-9](1|3|7|8|9|20)b\b")


def list_provider_models(provider: str, api_key: str, base_url: str = "") -> list[str]:
    import requests

    try:
        if base_url or provider in PROVIDER_BASE_URLS:
            return _list_models(base_url or PROVIDER_BASE_URLS[provider], api_key)
        if provider == "anthropic":
            r = requests.get(
                "https://api.anthropic.com/v1/models?limit=100",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                timeout=15,
            )
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]
        if provider == "gemini":
            r = requests.get(
                "https://generativelanguage.googleapis.com/v1beta/models", params={"key": api_key, "pageSize": 200}, timeout=15
            )
            r.raise_for_status()
            return [
                m["name"].removeprefix("models/")
                for m in r.json().get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])
            ]
    except Exception as e:  # pragma: no cover - network dependent
        logger.warning(f"Could not list models for {provider}: {e}")
    return []


def pick_best_model(ids: list[str]) -> str:
    """Heuristic: the strongest general/coding chat model among what the key can access."""
    chat = [i for i in ids if not NON_CHAT.search(i.lower())]
    for pref in PREFERENCE:
        matches = [i for i in chat if pref in i.lower()]
        if matches:
            return sorted(matches, key=lambda i: (bool(SMALL.search(i.lower())), [-ord(c) for c in i]))[0]
    return chat[0] if chat else ""


def _first_model_from_endpoint(base_url: str, api_key: str) -> str:
    ids = _list_models(base_url, api_key)
    return ids[0] if ids else ""


def resolve_model(
    cli_model: str | None = None,
    *,
    yaml_path: Path | None = None,
    env: dict[str, str] | None = None,
    discover: bool = True,
) -> ResolvedModel:
    env = dict(os.environ if env is None else env)
    cfg = load_model_yaml(yaml_path)
    api_key = env.get("AI_API_KEY", "").strip()
    if not api_key:
        raise ModelConfigError(
            "AI_API_KEY is not set. Export it first:  export AI_API_KEY=\"<your key>\"  (see .env.example)"
        )

    base_url = (env.get("AI_BASE_URL") or cfg.get("base_url") or "").strip()
    provider = (env.get("AI_PROVIDER") or cfg.get("provider") or "auto").strip().lower()
    model_name = (cli_model or env.get("AI_MODEL") or cfg.get("model_name") or "").strip()

    if provider == "auto":
        key_provider = detect_provider(api_key)
        model_prefix = model_name.split("/", 1)[0] if "/" in model_name else ""
        inv = {v: k for k, v in LITELLM_PREFIX.items()}
        if base_url:
            provider = "openai"  # generic OpenAI-compatible endpoint
        elif key_provider == "dashscope" and discover and (
            ep := discover_endpoint(api_key, [c for c in ENDPOINT_CANDIDATES if c[0] == "dashscope"])
        ):
            provider, base_url = ep.provider, ep.base_url  # workspace key: find its region
            model_name = model_name or pick_best_model(ep.models)
        elif key_provider != "openai":
            provider = key_provider  # a distinctive key (sk-ant-, AIza, gsk_, ...) decides where requests go
            base_url = FIXED_BASE_URLS.get(provider, "")
        elif model_prefix in inv:
            provider = inv[model_prefix]  # generic key: trust an explicit provider prefix in the model name
        elif discover and (ep := discover_endpoint(api_key)):
            # Generic "sk-..." key: DeepSeek, Qwen (DashScope), SiliconFlow and OpenAI keys all look alike.
            provider, base_url = ep.provider, ep.base_url
            if not model_name:
                model_name = pick_best_model(ep.models)
            elif ep.models and model_name not in ep.models:
                # e.g. AI_MODEL=qwen3-coder-plus given without a host: keep it, the probe reports if it's wrong
                logger.warning(f"{model_name} is not listed at {ep.base_url}")
        else:
            provider = "openai"

    if not model_name and base_url:
        model_name = pick_best_model(_list_models(base_url, api_key)) or _first_model_from_endpoint(base_url, api_key)
    from_default = False
    if not model_name and discover:
        # Prefer the prescribed DeepSeek/Qwen family among what this key can actually use.
        listed = list_provider_models(provider, api_key, base_url)
        best = pick_best_model(listed)
        if best and re.search(r"deepseek|qwen", best, re.I):
            model_name = best
    if not model_name:
        model_name = (cfg.get("provider_defaults") or {}).get(provider, "")
        from_default = True
    if not model_name:
        raise ModelConfigError("No model configured. Set AI_MODEL or model_name in config/model.yaml")

    # litellm routes on the leading "<provider>/" segment. Model ids may themselves contain slashes
    # (groq: openai/gpt-oss-120b, openrouter: anthropic/claude-...), so always add the route prefix.
    prefix = LITELLM_PREFIX.get(provider, provider)
    full_name = model_name if model_name.startswith(f"{prefix}/") else f"{prefix}/{model_name}"

    model_kwargs: dict[str, Any] = {"drop_params": True, **(cfg.get("model_kwargs") or {})}
    for override in cfg.get("model_overrides") or []:
        if re.search(override.get("match", "(?!)"), full_name, re.I):
            model_kwargs.update(override.get("model_kwargs") or {})
    model_kwargs["api_key"] = api_key
    if base_url:
        model_kwargs["api_base"] = base_url

    action_mode = (cfg.get("action_mode") or "auto").strip().lower()
    if (tm := env.get("AI_TEXT_MODE", "").strip()) in {"0", "1"}:
        action_mode = "text" if tm == "1" else "toolcall"

    return ResolvedModel(
        model_name=full_name,
        provider=provider,
        action_mode=action_mode,
        model_kwargs=model_kwargs,
        base_url=base_url,
        from_default=from_default,
    )


ACCOUNT_ERROR_MARKERS = (
    "insufficient balance",
    "insufficient_balance",
    "insufficient_quota",
    "exceeded your current quota",
    "payment required",
    "arrearage",  # Alibaba Cloud: account overdue
    "account is not active",
)
DAILY_QUOTA_MARKERS = ("tokens per day", "(tpd)", "requests per day", "(rpd)", "daily limit", "daily quota")


def account_problem(e: Exception) -> str | None:
    """A human explanation if the error is about the account (credit or daily quota), not a transient limit."""
    text = str(e).lower()
    m = re.search(r'"message"\s*:\s*"([^"]+)"', str(e))
    reason = (m.group(1) if m else str(e)[-200:]).strip()
    if getattr(e, "status_code", None) == 402 or any(k in text for k in ACCOUNT_ERROR_MARKERS):
        return f"the API account behind AI_API_KEY has no usable credit (provider says: {reason}). Top up the account or use another key."
    if any(k in text for k in DAILY_QUOTA_MARKERS):
        return f"the API key's DAILY quota is used up (provider says: {reason}). Wait for the reset or use another key."
    return None


def probe_action_mode(resolved: ResolvedModel) -> tuple[str, str]:
    """Make one tiny call to check auth + native tool calling. Returns (mode, note)."""
    import litellm

    from minisweagent.models.utils.actions_toolcall import BASH_TOOL
    from veriswe import quiet_litellm
    from veriswe.models import adaptive_completion, extract_leaked_command

    quiet_litellm()

    kwargs = dict(resolved.model_kwargs)
    messages = [
        {"role": "system", "content": "You are a test harness. Always answer by calling the bash tool."},
        {"role": "user", "content": "Call the bash tool with the command: echo ok"},
    ]
    try:
        resp = adaptive_completion(resolved.model_name, messages, kwargs, tools=[BASH_TOOL])
    except litellm.exceptions.NotFoundError as e:
        api_key = resolved.model_kwargs.get("api_key", "")
        if resolved.from_default and (best := pick_best_model(list_provider_models(resolved.provider, api_key, resolved.base_url))):
            prefix = LITELLM_PREFIX.get(resolved.provider, "openai")
            new_name = f"{prefix}/{best}"
            if new_name != resolved.model_name:
                old_name, resolved.model_name, resolved.from_default = resolved.model_name, new_name, False
                mode, note = probe_action_mode(resolved)
                return mode, f"{note}; default {old_name} unavailable, auto-selected {new_name}"
        hint = ""
        if resolved.base_url and (ids := _list_models(resolved.base_url, resolved.model_kwargs.get("api_key", ""))):
            hint = "\nModels available at this endpoint: " + ", ".join(ids[:40]) + "\nSet one with AI_MODEL=<id>."
        raise ModelConfigError(f"Model probe failed for {resolved.display}: {e}{hint}") from e
    except litellm.exceptions.AuthenticationError as e:
        raise ModelConfigError(f"Model probe failed for {resolved.display}: {e}") from e
    except Exception as e:
        if problem := account_problem(e):
            raise ModelConfigError(f"{resolved.display}: {problem}") from e
        if isinstance(e, (litellm.exceptions.RateLimitError, litellm.exceptions.APIConnectionError, litellm.exceptions.Timeout)):
            raise ModelConfigError(f"{resolved.display}: the provider kept refusing requests after retries: {e}") from e
        # Tools probably unsupported; check the model answers at all without tools.
        try:
            adaptive_completion(resolved.model_name, [{"role": "user", "content": "Reply with: ok"}], kwargs)
        except Exception as e2:
            raise ModelConfigError(f"Model probe failed for {resolved.display}: {e2}") from e2
        return "text", f"tool calling failed ({type(e).__name__}); using text actions"
    resolved.model_kwargs = kwargs  # keep any learned provider adaptations (e.g. Qwen enable_thinking)
    if resp.choices and resp.choices[0].message.tool_calls:
        return "toolcall", "native tool calling works"
    if resp.choices and extract_leaked_command(resp.choices[0].message.content or ""):
        return "toolcall", "tool calls arrive inside the text; parsing them from content"
    return "text", "model did not emit a tool call; using text actions"
