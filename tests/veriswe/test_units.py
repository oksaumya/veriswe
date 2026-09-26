"""Unit tests for VeriSWE building blocks (offline, no API key)."""

import subprocess
from pathlib import Path

import pytest

from veriswe import tools_dir
from veriswe.context import mask_observations
from veriswe.guards import LoopDetector, check_command
from veriswe.intake import read_issue
from veriswe.model_config import ModelConfigError, detect_provider, resolve_model
from veriswe.workspace import Workspace

# ---------------------------------------------------------------- model config


@pytest.mark.parametrize(
    ("key", "provider"),
    [("sk-ant-api03-x", "anthropic"), ("AIzaSyX", "gemini"), ("sk-or-v1-x", "openrouter"), ("gsk_x", "groq"), ("sk-proj-x", "openai"), ("whatever", "openai")],
)
def test_detect_provider(key, provider):
    assert detect_provider(key) == provider


def test_resolve_model_requires_key(tmp_path):
    with pytest.raises(ModelConfigError):
        resolve_model(env={}, yaml_path=tmp_path / "none.yaml")


def test_resolve_model_prefixes_and_passes_key(tmp_path):
    y = tmp_path / "m.yaml"
    y.write_text("model_name: claude-sonnet-5\nprovider: auto\nmodel_kwargs: {temperature: 0}\n")
    r = resolve_model(env={"AI_API_KEY": "sk-ant-abc"}, yaml_path=y)
    assert r.model_name == "anthropic/claude-sonnet-5"
    assert r.model_kwargs["api_key"] == "sk-ant-abc"
    assert r.model_kwargs["temperature"] == 0


def test_resolve_model_base_url_is_openai_compatible(tmp_path):
    y = tmp_path / "m.yaml"
    y.write_text("model_name: qwen3-coder\n")
    r = resolve_model(env={"AI_API_KEY": "k", "AI_BASE_URL": "http://host:8000/v1"}, yaml_path=y)
    assert r.model_name == "openai/qwen3-coder"
    assert r.model_kwargs["api_base"] == "http://host:8000/v1"


def test_env_overrides_model_and_mode(tmp_path):
    y = tmp_path / "m.yaml"
    y.write_text("model_name: gpt-5\n")
    r = resolve_model(env={"AI_API_KEY": "sk-x", "AI_MODEL": "gemini/gemini-2.5-pro", "AI_TEXT_MODE": "1"}, yaml_path=y)
    assert r.model_name == "gemini/gemini-2.5-pro"
    assert r.provider == "gemini"
    assert r.action_mode == "text"


def test_committed_config_has_no_secrets():
    root = Path(__file__).parents[2]
    for f in [root / "config" / "model.yaml", root / "config" / "agent.yaml", root / ".env.example", root / "Makefile"]:
        text = f.read_text()
        for marker in ("sk-ant-api", "sk-proj-", "AIzaSy", "sk-or-v1-"):
            assert marker not in text, f"possible secret in {f}"


# ---------------------------------------------------------------- guards


@pytest.mark.parametrize(
    "cmd",
    ["vim foo.py", "cat x | less", "git stash", "git reset --hard HEAD", "git checkout main", "git log --all", "git fetch origin", "rm -rf .git", "cd a && nano b"],
)
def test_blocked_commands(cmd):
    assert check_command(cmd) is not None


@pytest.mark.parametrize(
    "cmd",
    ["git diff", "git status", "git checkout -- a.py", "grep -rn lessons .", "python -m pytest -q", "echo more", "git log -3 --oneline"],
)
def test_allowed_commands(cmd):
    assert check_command(cmd) is None


def test_loop_detector_escalates():
    d = LoopDetector()
    assert d.record("ls", "a", 0) is None
    assert d.record("ls", "a", 0) is None
    assert "same command" in d.record("ls", "a", 0)
    d.record("ls", "a", 0)
    assert "STOP" in d.record("ls", "a", 0)


# ---------------------------------------------------------------- context masking


def _obs(i):
    return {"role": "tool", "content": f"output {i}\n" + "x" * 500, "extra": {"raw_output": "x", "returncode": 0}}


def test_masking_keeps_recent_and_is_chunked():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    for i in range(20):
        msgs += [{"role": "assistant", "content": f"a{i}"}, _obs(i)]
    out = mask_observations(msgs, keep_last=10, chunk=6)
    masked = [m for m in out if m.get("role") == "tool" and m["content"].startswith("[Output elided")]
    assert len(masked) == 6  # 10 maskable, rounded down to chunk of 6
    assert out[-1]["content"].startswith("output 19")
    assert msgs[3]["content"].startswith("output 0")  # original untouched


# ---------------------------------------------------------------- intake


def test_read_issue_text_and_file(tmp_path):
    assert "broken" in read_issue("Something is broken\nsee details").body
    f = tmp_path / "issue.md"
    f.write_text("from file")
    assert read_issue(f"@{f}").body == "from file"
    assert read_issue(str(f)).body == "from file"


# ---------------------------------------------------------------- workspace


def test_workspace_diff_includes_new_files_and_excludes_scratch(calc_repo: Path):
    ws = Workspace.prepare(calc_repo)
    assert ws.diff() == ""
    (calc_repo / "calc" / "new_mod.py").write_text("X = 1\n")
    (calc_repo / "reproduce_issue.py").write_text("print(1)\n")
    (calc_repo / ".veriswe" / "junk.txt").write_text("junk")
    d = ws.diff()
    assert "new_mod.py" in d
    assert "reproduce_issue.py" not in d and "junk" not in d
    # reverse + apply round trip
    ws.reverse(d)
    assert not (calc_repo / "calc" / "new_mod.py").exists()
    ws.apply(d)
    assert ws.diff() == d
    # the real index was not touched
    status = subprocess.run(["git", "status", "--porcelain"], cwd=calc_repo, capture_output=True, text=True).stdout
    assert "?? calc/new_mod.py" in status


# ---------------------------------------------------------------- tools


def _tool(name, *args, cwd, stdin=""):
    return subprocess.run([str(tools_dir / name), *args], cwd=cwd, input=stdin, capture_output=True, text=True)


def test_str_replace_unique_and_lint_revert(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("def f():\n    return 1\n\n\ndef g():\n    return 1\n")
    block = "<<<<<<< SEARCH\n    return 1\n=======\n    return 2\n>>>>>>> REPLACE\n"
    r = _tool("str_replace", "m.py", cwd=tmp_path, stdin=block)
    assert r.returncode == 1 and "2 locations" in r.stdout
    good = "<<<<<<< SEARCH\ndef g():\n    return 1\n=======\ndef g():\n    return 2\n>>>>>>> REPLACE\n"
    r = _tool("str_replace", "m.py", cwd=tmp_path, stdin=good)
    assert r.returncode == 0 and "return 2" in f.read_text()
    bad = "<<<<<<< SEARCH\ndef g():\n=======\ndef g(:\n>>>>>>> REPLACE\n"
    before = f.read_text()
    r = _tool("str_replace", "m.py", cwd=tmp_path, stdin=bad)
    assert r.returncode == 1 and "REVERTED" in r.stdout
    assert f.read_text() == before
    r = _tool("undo_edit", "m.py", cwd=tmp_path)
    assert r.returncode == 0 and "return 2" not in f.read_text()


def test_view_and_search(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(f"line{i}" for i in range(1, 251)))
    r = _tool("view", "a.py", "10", "12", cwd=tmp_path)
    assert "    10\tline10" in r.stdout and "line13" not in r.stdout
    r = _tool("search", "line24[0-9]", cwd=tmp_path)
    assert "line245" in r.stdout


def test_str_replace_lenient_markers_and_create_overwrite(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    f = tmp_path / "m.py"
    f.write_text("x = 1\n")
    sloppy = "<<<<<< SEARCH\nx = 1\n======\nx = 2\n>>>>>> REPLACE\n"
    r = _tool("str_replace", "m.py", cwd=tmp_path, stdin=sloppy)
    assert r.returncode == 0 and f.read_text() == "x = 2\n", r.stdout
    # untracked file created by the agent can be re-created; tracked project files cannot
    assert _tool("str_replace", "new.py", "--create", cwd=tmp_path, stdin="a = 1\n").returncode == 0
    r = _tool("str_replace", "new.py", "--create", cwd=tmp_path, stdin="a = 2\n")
    assert r.returncode == 0 and "Overwrote" in r.stdout
    subprocess.run(["git", "add", "m.py"], cwd=tmp_path)
    assert _tool("str_replace", "m.py", "--create", cwd=tmp_path, stdin="boom\n").returncode == 1


def test_pick_best_model():
    from veriswe.model_config import pick_best_model

    groq = ["allam-2-7b", "whisper-large-v3", "meta-llama/llama-prompt-guard-2-22m", "openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
    assert pick_best_model(groq) == "openai/gpt-oss-120b"
    assert pick_best_model(["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5-5"]) == "claude-opus-5-5"
    assert pick_best_model(["gpt-5-nano", "gpt-5-mini", "gpt-5", "text-embedding-3-large"]) == "gpt-5"


def test_retry_after_parsing():
    from veriswe.models import retry_after_seconds

    assert retry_after_seconds(Exception("Rate limit reached ... Please try again in 7.66s. Need more tokens?")) == 7.66
    assert retry_after_seconds(Exception("Please try again in 1m2.5s")) == 62.5
    assert retry_after_seconds(Exception("try again in 250ms")) == 0.25
    assert retry_after_seconds(Exception("no hint")) is None


def test_select_tests_via_indirect_package_import(tmp_path):
    from veriswe.verify import Verifier

    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "_core.py").write_text("X = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("import pkg as lib\n")
    (tmp_path / "tests" / "test_things.py").write_text("from . import lib\n")
    ws = Workspace(repo=tmp_path, base_commit="HEAD")
    assert Verifier(ws).select_python_tests(["src/pkg/_core.py"]) == ["tests/test_things.py"]
