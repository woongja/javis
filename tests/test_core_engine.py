import json
import os
import time
from pathlib import Path

import pytest

from core.config import EngineSettings, load_settings
from core.engine import ClaudeEngine
from worklog.store import Item


def success(items):
    return {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps({"items": items})}


def fake_claude(tmp_path: Path, code: str) -> Path:
    shim = tmp_path / "claude.py"
    shim.write_text(
        "import json, os, subprocess, sys, time\nfrom pathlib import Path\n"
        "question = sys.stdin.buffer.read().decode('utf-8')\n" + code,
        encoding="utf-8",
    )
    return shim


def test_claude_arguments_stdin_environment_and_sources(tmp_path, monkeypatch):
    capture = tmp_path / "capture.json"
    envelope = success([{
        "text": "Usage limit is normal answer content",
        "sources": [
            "https://example.org/one", "ftp://example.org", "/relative", "https:///missing-host",
            "https://x@y.example.org/",  # harness:allow — synthetic URL with user info
            "https://example.org/has space", "https://example.org/\n",
            "https://example.org/one", "http://example.org/two", "https://example.org/\u0081",
        ],
    }, {"text": "Second", "sources": ["https://example.org/one"]}])
    shim = fake_claude(tmp_path, f"""
Path({str(capture)!r}).write_text(json.dumps({{
    'argv': sys.argv[1:], 'stdin': question, 'env': list(os.environ), 'cwd_entries': os.listdir(),
}}), encoding='utf-8')
print(json.dumps({envelope!r}))
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-test-value")
    monkeypatch.setenv("JAVIS_TELEGRAM_TOKEN", "fake-test-value")
    monkeypatch.setenv("TELEGRAM_TOKEN", "fake-test-value")
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path))
    question = '질문 "&"\n다음 줄'
    result = ClaudeEngine(load_settings(), shim).run(question)
    assert (result.status, result.reason, result.model) == ("ok", None, "claude-sonnet-5-5")
    assert result.duration_s >= 0
    assert result.items == (
        Item("Usage limit is normal answer content", ("https://example.org/one", "http://example.org/two")),
        Item("Second", ("https://example.org/one",)),
    )
    recorded = json.loads(capture.read_text(encoding="utf-8"))
    argv = recorded["argv"]
    assert recorded["stdin"] == question and all(question not in arg for arg in argv)
    assert recorded["cwd_entries"] == []
    assert {"--restricted", "--safe-mode", "--strict-mcp-config", "--no-session-persistence", "-p"} <= set(argv)
    for flag, value in {
        "--input-format": "text", "--output-format": "json", "--permission-mode": "dontAsk",
        "--tools": "WebSearch", "--allowedTools": "WebSearch", "--model": "claude-sonnet-5-5",
    }.items():
        assert argv[argv.index(flag) + 1] == value
    prompt = argv[argv.index("--system-prompt") + 1]
    assert '{"items":' in prompt
    assert "Do not prefix item text with numbers or list markers." in prompt
    assert "PATH" in recorded["env"]
    assert {"ANTHROPIC_API_KEY", "JAVIS_TELEGRAM_TOKEN", "TELEGRAM_TOKEN", "JAVIS_DATA_DIR"}.isdisjoint(recorded["env"])


@pytest.mark.parametrize("body", [
    '```json\n{"items":[]}\n```', '{"items":[', '{"items":[]}',
    '{"items":[{"text":" ","sources":[]}]}', '{"items":[{"text":"ok","sources":[1]}]}',
    '{"items":[{"text":"ok","sources":"https://example.org"}]}',
])
def test_invalid_body(tmp_path, body):
    envelope = success([]) | {"result": body}
    result = ClaudeEngine(EngineSettings(), fake_claude(tmp_path, f"print(json.dumps({envelope!r}))\n")).run("test")
    assert (result.status, result.reason, result.items) == ("error", "invalid_output", ())


@pytest.mark.parametrize(("envelope", "code", "stderr", "expected"), [
    ({"type": "other"}, 0, "", ("error", "invalid_output")),
    (success([]) | {"is_error": 0}, 0, "", ("error", "invalid_output")),
    ({"type": "result", "subtype": "error", "is_error": True, "result": "Bad request"}, 0, "", ("error", "engine_error")),
    ({"type": "result", "subtype": "error", "is_error": True, "result": "You've hit your limit"}, 1, "", ("quota", "usage_limit")),
    (None, 1, "Not logged in", ("error", "configuration")),
    (None, 1, "unknown option --safe-mode", ("error", "configuration")),
    (None, 1, "rate_limit_error", ("quota", "usage_limit")),
    (success([{"text": "usage limit", "sources": []}]), 2, "", ("error", "engine_error")),
])
def test_failures(tmp_path, envelope, code, stderr, expected):
    shim = fake_claude(tmp_path, f"""
print(json.dumps({envelope!r}))
print({stderr!r}, file=sys.stderr)
sys.exit({code})
""")
    result = ClaudeEngine(EngineSettings(), shim).run("test")
    assert (result.status, result.reason) == expected
    assert result.items == ()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_bound(tmp_path, stream):
    shim = fake_claude(tmp_path, f"sys.{stream}.buffer.write(b'x' * (1024 * 1024 + 1))\ntime.sleep(10)\n")
    result = ClaudeEngine(EngineSettings(timeout_s=5), shim).run("test")
    assert (result.status, result.reason) == ("error", "output_limit")


def test_timeout_kills_descendant_after_parent_exits(tmp_path):
    started = tmp_path / "started"
    survived = tmp_path / "survived"
    child = f"from pathlib import Path; import time; time.sleep(1.5); Path({str(survived)!r}).touch()"
    shim = fake_claude(tmp_path, f"""
subprocess.Popen([sys.executable, '-c', {child!r}])
Path({str(started)!r}).touch()
""")
    result = ClaudeEngine(EngineSettings(timeout_s=0.8), shim).run("test")
    assert (result.status, result.reason) == ("timeout", "timeout")
    assert started.exists()
    time.sleep(1.6)
    assert not survived.exists()


def test_unconfigured_model_is_omitted(tmp_path):
    envelope = success([{"text": "default model", "sources": []}])
    shim = fake_claude(tmp_path, f"assert '--model' not in sys.argv\nprint(json.dumps({envelope!r}))\n")
    result = ClaudeEngine(EngineSettings(), shim).run("test")
    assert result.status == "ok" and result.model is None


def test_plain_usage_error_on_nonzero_exit(tmp_path):
    shim = fake_claude(tmp_path, "print('Usage limit reached')\nsys.exit(1)\n")
    result = ClaudeEngine(EngineSettings(), shim).run("test")
    assert (result.status, result.reason) == ("quota", "usage_limit")


@pytest.mark.skipif(os.name != "nt", reason="Windows npm shim")
def test_windows_npm_discovery_runs_native_binary(tmp_path, monkeypatch):
    shim = tmp_path / "claude.cmd"
    shim.touch()
    executable = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr("core.engine.shutil.which", lambda name: str(shim))

    def transport(args, payload, cwd, env, timeout):
        assert Path(args[0]) == executable
        envelope = success([{"text": "ok", "sources": []}])
        return 0, json.dumps(envelope).encode(), b""

    monkeypatch.setattr("core.engine.run_bounded", transport)
    assert ClaudeEngine(EngineSettings()).run("test").status == "ok"
