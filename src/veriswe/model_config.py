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

KNOWN_PREFIXES = (
    "anthropic/",
    "openai/",
    "gemini/",
    "vertex_ai/",
    "openrouter/",
    "groq/",
    "xai/",
    "mistral/",
    "deepseek/",
    "together_ai/",
    "azure/",
    "bedrock/",
    "ollama/",
    "ollama_chat/",
    "hosted_vllm/",
    "fireworks_ai/",
    "cerebras/",
)

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
    return "openai"


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
    "claude-opus", "claude-sonnet", "gpt-5", "gemini-3", "gemini-2.5-pro", "qwen3-coder", "gpt-oss-120b",
    "kimi-k2", "deepseek", "glm-4", "grok-4", "devstral", "codestral", "llama-4", "qwen3", "70b", "gpt-4.1",
    "gpt-4o", "claude", "gemini", "gpt-oss", "llama", "mistral",
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
    cli_model: str | None = None, *, yaml_path: Path | None = None, env: dict[str, str] | None = None
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
        # An explicit prefix in the model name wins over key sniffing.
        prefix = model_name.split("/", 1)[0] if "/" in model_name else ""
        inv = {v: k for k, v in LITELLM_PREFIX.items()}
        if prefix in inv and not base_url:
            provider = inv[prefix]
        elif base_url:
            provider = "openai"  # generic OpenAI-compatible endpoint
        else:
            provider = detect_provider(api_key)

    if not model_name and base_url:
        model_name = _first_model_from_endpoint(base_url, api_key)
    from_default = False
    if not model_name:
        model_name = (cfg.get("provider_defaults") or {}).get(provider, "")
        from_default = True
    if not model_name:
        raise ModelConfigError("No model configured. Set AI_MODEL or model_name in config/model.yaml")

    prefix = LITELLM_PREFIX.get(provider, "openai")
    if base_url:
        # Custom endpoint: keep the served model id verbatim after the provider prefix.
        full_name = model_name if model_name.startswith(f"{prefix}/") else f"{prefix}/{model_name}"
    elif model_name.startswith(KNOWN_PREFIXES):
        full_name = model_name
    else:
        full_name = f"{prefix}/{model_name}"

    model_kwargs: dict[str, Any] = {"drop_params": True, **(cfg.get("model_kwargs") or {})}
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


def probe_action_mode(resolved: ResolvedModel) -> tuple[str, str]:
    """Make one tiny call to check auth + native tool calling. Returns (mode, note)."""
    import litellm

    from minisweagent.models.utils.actions_toolcall import BASH_TOOL
    from veriswe import quiet_litellm

    quiet_litellm()

    kwargs = dict(resolved.model_kwargs)
    messages = [
        {"role": "system", "content": "You are a test harness. Always answer by calling the bash tool."},
        {"role": "user", "content": "Call the bash tool with the command: echo ok"},
    ]
    try:
        resp = litellm.completion(model=resolved.model_name, messages=messages, tools=[BASH_TOOL], **kwargs)
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
        # Tools probably unsupported; check the model answers at all without tools.
        try:
            litellm.completion(
                model=resolved.model_name, messages=[{"role": "user", "content": "Reply with: ok"}], **kwargs
            )
        except Exception as e2:
            raise ModelConfigError(f"Model probe failed for {resolved.display}: {e2}") from e2
        return "text", f"tool calling failed ({type(e).__name__}); using text actions"
    if resp.choices and resp.choices[0].message.tool_calls:
        return "toolcall", "native tool calling works"
    return "text", "model did not emit a tool call; using text actions"
