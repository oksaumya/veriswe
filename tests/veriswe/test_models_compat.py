"""DeepSeek / Qwen compatibility: provider quirks, leaked tool calls, tolerant arguments (offline)."""

import json
from types import SimpleNamespace

import litellm
import pytest

from veriswe.models import adapt_to_provider_error, extract_leaked_command, parse_tool_arguments


def _bad_request(msg):
    return litellm.exceptions.BadRequestError(message=msg, model="m", llm_provider="dashscope")


def test_qwen_enable_thinking_adaptation():
    kwargs = {"temperature": 0.7}
    e = _bad_request("parameter.enable_thinking must be set to false for non-streaming calls")
    assert adapt_to_provider_error(e, kwargs)
    assert kwargs["extra_body"] == {"enable_thinking": False}
    # model is thinking-only -> drop the parameter again
    assert adapt_to_provider_error(_bad_request("The value of the enable_thinking parameter is restricted to True"), kwargs)
    assert "extra_body" not in kwargs
    assert not adapt_to_provider_error(_bad_request("some unrelated 400"), kwargs)


@pytest.mark.parametrize(
    "content",
    [
        "I'll list files.\n<tool_call>\n<function=bash>\n<parameter=command>\nls -la\n</parameter>\n</function>\n</tool_call>",
        '<tool_call>{"name": "bash", "arguments": {"command": "ls -la"}}</tool_call>',
        '<｜DSML｜function_calls><｜DSML｜invoke name="bash"><｜DSML｜parameter name="command" string="true">ls -la</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜function_calls>',
        '<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>bash\n```json\n{"command": "ls -la"}\n```<｜tool▁call▁end｜>',
    ],
)
def test_extract_leaked_tool_calls(content):
    assert extract_leaked_command(content) == "ls -la"


def test_leaked_other_tool_is_ignored():
    assert extract_leaked_command('<tool_call>{"name": "web_search", "arguments": {"command": "x"}}</tool_call>') is None
    assert extract_leaked_command("just prose, no call") is None


@pytest.mark.parametrize(
    "raw",
    [
        '{"command": "ls"}',
        '```json\n{"command": "ls"}\n```',
        '{"command": "ls"} trailing words',
        '{"arguments": {"command": "ls"}}',
        '{"input": "{\\"command\\": \\"ls\\"}"}',
        '{"cmd": "ls"}',
    ],
)
def test_tolerant_arguments(raw):
    assert parse_tool_arguments(raw)["command"] == "ls"


def _response(content=None, tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])


def test_toolcall_model_accepts_aliases_and_leaks():
    from veriswe.models import VeriToolcallModel

    m = VeriToolcallModel(model_name="openai/x", cost_tracking="ignore_errors")
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="execute_bash", arguments='{"arguments": {"command": "pwd"}}'))
    assert m._parse_actions(_response(tool_calls=[tc])) == [{"command": "pwd", "tool_call_id": "c1"}]
    leaked = m._parse_actions(_response(content="<tool_call>\n<function=bash>\n<parameter=command>\npwd\n</parameter>\n</function>\n</tool_call>"))
    assert leaked[0]["command"] == "pwd"


def test_text_model_accepts_plain_bash_block():
    from veriswe.models import VeriTextModel

    m = VeriTextModel(model_name="openai/x", cost_tracking="ignore_errors")
    m._dropped = 0
    assert m._parse_actions(_response(content="THOUGHT: look\n\n```bash\nls\n```")) == [{"command": "ls"}]


def test_deepseek_and_qwen_overrides_from_config():
    from veriswe import config_dir
    from veriswe.model_config import resolve_model

    ds = resolve_model(env={"AI_API_KEY": "sk-x", "AI_MODEL": "deepseek/deepseek-flash"}, yaml_path=config_dir / "model.yaml")
    assert ds.model_kwargs["reasoning_effort"] == "high"  # makes LiteLLM pass reasoning_content back
    qw = resolve_model(env={"AI_API_KEY": "sk-ws-abc", "AI_PROVIDER": "dashscope", "AI_MODEL": "qwen3.8-max"}, yaml_path=config_dir / "model.yaml")
    assert qw.model_name == "dashscope/qwen3.8-max"
    assert qw.model_kwargs["temperature"] == 0.7 and qw.model_kwargs["top_p"] == 0.8
    assert ds.model_kwargs["timeout"] >= 600


def test_pick_best_prefers_current_deepseek_and_qwen():
    from veriswe.model_config import pick_best_model

    assert pick_best_model(["deepseek-v4-pro", "deepseek-flash"]) == "deepseek-flash"
    dashscope = ["text-embedding-v4", "qwen-plus", "qwen3-coder-plus", "qwen3.8-max", "qwen3.8-flash", "gte-rerank-v2"]
    assert pick_best_model(dashscope) == "qwen3.8-max"
    assert pick_best_model(["qwen-plus", "qwen3-coder-plus", "qwen3-coder-flash"]) == "qwen3-coder-plus"


def test_distinctive_alibaba_and_novita_keys():
    from veriswe.model_config import detect_provider

    assert detect_provider("sk-sp-abc") == "dashscope_coding"
    assert detect_provider("sk-ws-abc") == "dashscope"
    assert detect_provider("sk_abc") == "novita"
    assert detect_provider("sk-abc") == "openai"  # generic -> probed


def test_deepseek_cache_hits_are_counted():
    from veriswe.agent import VeriAgent

    agent = VeriAgent.__new__(VeriAgent)
    agent.tokens = {"prompt": 0, "completion": 0, "cached": 0}
    usage = {"prompt_tokens": 100, "completion_tokens": 5, "prompt_cache_hit_tokens": 64, "prompt_cache_miss_tokens": 36}
    agent._account_tokens({"extra": {"response": {"usage": usage}}})
    assert agent.tokens == {"prompt": 100, "completion": 5, "cached": 64}
    assert json.dumps(agent.tokens)


def test_billing_errors_are_explained_not_misdiagnosed(monkeypatch):
    import veriswe.model_config as mc
    from veriswe.model_config import ModelConfigError, ResolvedModel, probe_action_mode

    err = litellm.exceptions.BadRequestError(
        message='DeepseekException - {"error":{"message":"Insufficient Balance"}}', model="deepseek/deepseek-flash", llm_provider="deepseek"
    )

    def boom(*a, **k):
        raise err

    monkeypatch.setattr("veriswe.models.adaptive_completion", boom)
    r = ResolvedModel(model_name="deepseek/deepseek-flash", provider="deepseek", action_mode="auto", model_kwargs={"api_key": "k"})
    with pytest.raises(ModelConfigError, match="no usable credit"):
        probe_action_mode(r)
    assert mc.account_problem(Exception("rate limit reached")) is None
    # a per-minute limit whose text mentions billing is NOT an account problem...
    tpm = "Rate limit reached on output tokens per minute (OTPM): Limit 1000. Upgrade at https://console.groq.com/settings/billing"
    assert mc.account_problem(Exception(tpm)) is None
    # ...but an exhausted daily quota is reported as such
    tpd = '{"error":{"message":"Rate limit reached ... on tokens per day (TPD): Limit 200000, Used 194225"}}'
    assert "DAILY quota" in mc.account_problem(Exception(tpd))


def test_discovery_returns_without_waiting_for_slow_hosts(monkeypatch):
    import time

    import requests

    import veriswe.model_config as mc

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "deepseek-flash"}]}

    def get(url, **kw):
        if "deepseek" in url:
            return R()
        time.sleep(3)  # a slow, irrelevant host
        raise requests.ConnectionError

    monkeypatch.setattr(requests, "get", get)
    t = time.time()
    ep = mc._real_discover_endpoint("sk-x")
    assert ep.provider == "deepseek" and time.time() - t < 1.5


def test_probe_survives_rate_limits(monkeypatch):
    from veriswe.model_config import ResolvedModel, probe_action_mode

    calls = {"n": 0}
    ok = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[object()], content=""))])

    def completion(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise litellm.exceptions.RateLimitError(
                message="Rate limit reached ... output tokens per minute (OTPM): Limit 1000, Used 520, Requested 508. Please try again in 0.05s.",
                model="groq/qwen/qwen3.8-27b",
                llm_provider="groq",
            )
        return ok

    monkeypatch.setattr(litellm, "completion", completion)
    r = ResolvedModel(model_name="groq/qwen/qwen3.8-27b", provider="groq", action_mode="auto", model_kwargs={"api_key": "k"})
    assert probe_action_mode(r)[0] == "toolcall"
    assert calls["n"] == 2


def test_salvage_tool_call_rejected_by_server_parser(monkeypatch):
    """Groq/vLLM: 'tool_use_failed' with the raw generation attached -> run the command anyway."""
    from veriswe.models import VeriToolcallModel, salvage_failed_generation

    raw = "<tool_call>\n<function=bash>\n<parameter=command>\ncd /repo && ls -la src/ && echo \"---\"\n</parameter>\n</function>\n</tool_call>"
    body = json.dumps({"error": {"message": "Failed to call a function.", "code": "tool_use_failed", "failed_generation": raw}})
    err = litellm.exceptions.BadRequestError(message=f"GroqException - {body}", model="groq/qwen", llm_provider="groq")
    assert salvage_failed_generation(err) == (raw, 'cd /repo && ls -la src/ && echo "---"')

    m = VeriToolcallModel(model_name="groq/qwen/qwen3.8-27b", cost_tracking="ignore_errors")

    def fail(*a, **k):
        raise err

    monkeypatch.setattr(litellm, "completion", fail)
    msg = m.query([{"role": "user", "content": "hi"}])
    assert msg["extra"]["actions"][0]["command"].startswith("cd /repo")
    assert msg["role"] == "assistant" and "tool_calls" not in msg


def test_probe_is_not_fooled_by_one_flaky_tool_parse(monkeypatch):
    from veriswe.model_config import ResolvedModel, probe_action_mode

    calls = {"n": 0}
    ok = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[object()], content=""))])
    flaky = litellm.exceptions.BadRequestError(message='{"error":{"message":"Failed to call a function.","code":"tool_use_failed"}}', model="m", llm_provider="groq")

    def completion(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise flaky
        return ok

    monkeypatch.setattr(litellm, "completion", completion)
    r = ResolvedModel(model_name="groq/qwen/qwen3.8-27b", provider="groq", action_mode="auto", model_kwargs={"api_key": "k"})
    assert probe_action_mode(r)[0] == "toolcall"
