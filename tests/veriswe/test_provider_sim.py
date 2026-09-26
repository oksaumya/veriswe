"""Full VeriSWE runs against local fake servers that enforce real DeepSeek / Qwen (DashScope) API rules.

No network and no API key: the servers speak the OpenAI chat-completions protocol and reject requests the way
the real providers do, so these tests catch protocol mistakes that scripted models cannot.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.veriswe.test_agent_e2e import GOOD_FIX, ISSUE, REPRO, SUBMIT
from veriswe.runner import run_task

SCRIPT = [REPRO, GOOD_FIX, SUBMIT]


class FakeProvider:
    """rules: 'deepseek' -> thinking responses + 400 if reasoning_content is not passed back while tools are used.
    'qwen-open' -> 400 unless enable_thinking=false is sent (open-weight Qwen3, non-streaming)."""

    def __init__(self, rules: str):
        self.rules = rules
        self.requests: list[dict] = []
        self.errors: list[str] = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # GET /models
                self._send(200, {"object": "list", "data": [{"id": "deepseek-flash"}, {"id": "qwen3-32b"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                provider.requests.append(body)
                status, payload = provider.respond(body)
                self._send(status, payload)

            def _send(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def error(self, msg):
        self.errors.append(msg)
        return 400, {"error": {"message": msg, "type": "invalid_request_error", "code": "invalid_request_error"}}

    def respond(self, body):
        msgs = body["messages"]
        if self.rules == "deepseek" and body.get("tools"):
            for m in msgs:
                if m["role"] == "assistant" and not m.get("reasoning_content"):
                    return self.error("The reasoning_content in the thinking mode must be passed back to the API.")
        if self.rules == "qwen-open" and body.get("enable_thinking") is not False:
            return self.error("<400> InternalError.Algo.InvalidParameter: parameter.enable_thinking must be set to false for non-streaming calls")
        is_probe = "test harness" in (msgs[0].get("content") or "")
        step = sum(1 for m in msgs if m["role"] == "assistant")
        command = "echo ok" if is_probe else SCRIPT[min(step, len(SCRIPT) - 1)]
        message = {
            "role": "assistant",
            "content": f"Step {step}.",
            "tool_calls": [{"id": f"call_{len(self.requests)}", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": command})}}],
        }
        if self.rules == "deepseek":
            message["reasoning_content"] = f"Reasoning for step {step}."
        usage = {"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220, "prompt_cache_hit_tokens": 128, "prompt_cache_miss_tokens": 72,
                 "prompt_tokens_details": {"cached_tokens": 128}}  # fmt: skip
        return 200, {"id": "x", "object": "chat.completion", "created": 1, "model": body["model"],
                     "choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}], "usage": usage}  # fmt: skip

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake(request):
    p = FakeProvider(request.param)
    yield p
    p.close()


def _yaml(tmp_path: Path, model: str) -> Path:
    import yaml

    from veriswe import config_dir

    cfg = yaml.safe_load((config_dir / "model.yaml").read_text())
    cfg["model_name"] = model
    y = tmp_path / "model.yaml"
    y.write_text(yaml.safe_dump(cfg))
    return y


@pytest.mark.parametrize("fake", ["deepseek"], indirect=True)
def test_full_run_against_deepseek_rules(fake, calc_repo, tmp_path, monkeypatch):
    import veriswe.model_config as mc

    monkeypatch.setenv("AI_API_KEY", "sk-deepseek-test")
    monkeypatch.setenv("AI_PROVIDER", "deepseek")
    monkeypatch.setenv("AI_BASE_URL", fake.url)
    real_load = mc.load_model_yaml
    monkeypatch.setattr(mc, "load_model_yaml", lambda path=None: real_load(_yaml(tmp_path, "deepseek-flash")))
    res = run_task(ISSUE, str(calc_repo))
    assert fake.errors == [], fake.errors  # reasoning_content was passed back on every turn
    assert res.verified, res.status
    assert res.stats["tokens"]["cached"] > 0
    agent_requests = [r for r in fake.requests if "test harness" not in r["messages"][0]["content"]]
    assert all(r.get("thinking") == {"type": "enabled"} for r in agent_requests)


@pytest.mark.parametrize("fake", ["qwen-open"], indirect=True)
def test_full_run_against_qwen_open_model_rules(fake, calc_repo, tmp_path, monkeypatch):
    import veriswe.model_config as mc

    monkeypatch.setenv("AI_API_KEY", "sk-qwen-test")
    monkeypatch.setenv("AI_PROVIDER", "dashscope")
    monkeypatch.setenv("AI_BASE_URL", fake.url)
    real_load = mc.load_model_yaml
    monkeypatch.setattr(mc, "load_model_yaml", lambda path=None: real_load(_yaml(tmp_path, "qwen3-32b")))
    res = run_task(ISSUE, str(calc_repo))
    assert res.verified, res.status
    assert len(fake.errors) == 1  # only the very first probe call; the fix is learned and reused afterwards
    assert all(r.get("enable_thinking") is False for r in fake.requests[1:])
    assert all(r.get("temperature") == 0.7 for r in fake.requests), [(r.get("temperature"), r["messages"][0]["content"][:30]) for r in fake.requests]
