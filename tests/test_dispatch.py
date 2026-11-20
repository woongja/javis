import json
import os

import pytest

from harness import HarnessError
from harness.dispatch import dispatch
from harness.paths import handoff_dir, state_dir, worktree_dir
from harness.roles import Role
from harness.runner import acquire_lock, build_command, safe_env
from harness.tasks import load, new_task


def test_safe_env_drops_api_keys():
    e = safe_env({"PATH": "p", "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", "JAVIS_X": "1"}, {"JAVIS_AGENT": "1"})
    assert "ANTHROPIC_API_KEY" not in e and "OPENAI_API_KEY" not in e
    assert e["JAVIS_AGENT"] == "1" and e["JAVIS_X"] == "1" and e["PATH"] == "p"


def test_build_codex_command(fake_bins, tmp_path):
    cmd = build_command(Role("codex.dev", "codex", "gpt-6.2-sol", "medium"), tmp_path)
    assert cmd[0] == str(fake_bins["codex"])
    assert cmd[1:] == ["exec", "-m", "gpt-6.2-sol", "-c", 'model_reasoning_effort="medium"',
                       "--ignore-user-config", "-C", str(tmp_path), "-s", "workspace-write", "-"]


def test_build_claude_command(fake_bins, tmp_path):
    cmd = build_command(Role("claude.dev", "claude", "claude-opus-5-5", "medium"), tmp_path)
    assert cmd[1:] == ["-p", "--model", "claude-opus-5-5", "--effort", "medium",
                       "--setting-sources", "project,local", "--permission-mode", "acceptEdits"]


def test_build_command_missing_exe(monkeypatch, tmp_path):
    monkeypatch.delenv("JAVIS_CODEX_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda n: None)
    with pytest.raises(HarnessError, match="codex not found"):
        build_command(Role("codex.dev", "codex", "m", "low"), tmp_path)


def _events(root):
    return [json.loads(l) for l in (state_dir(root) / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]


def test_dispatch_ok(git_repo_with_harness, fake_bins):
    root = git_repo_with_harness
    t = new_task(root, "한글 제목 공백 포함", "codex")
    t = dispatch(root, t.id, "codex.dev")
    assert t.status == "done" and t.handoffs == 1
    assert (handoff_dir(root, t.id) / "result.md").read_text(encoding="utf-8").startswith("done")
    assert (worktree_dir(root, t.id) / "hud" / "demo.txt").exists()
    end = [e for e in _events(root) if e["event"] == "dispatch.end"][0]
    assert end["model"] == "gpt-6.2-sol" and end["exit"] == 0 and end["role"] == "codex.dev"


def test_dispatch_passes_worker_env(git_repo_with_harness, fake_bins, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    root = git_repo_with_harness
    t = new_task(root, "x", "claude")
    dispatch(root, t.id, "claude.dev")
    args = (worktree_dir(root, t.id) / ".fake-args.txt").read_text(encoding="utf-8")
    assert "claude-opus-5-5" in args and "leaked=[]" in args
    assert f"worker=1|{worktree_dir(root, t.id)}" in args


def test_dispatch_quota(git_repo_with_harness, fake_bins, monkeypatch):
    monkeypatch.setenv("JAVIS_FAKE_MODE", "quota")
    root = git_repo_with_harness
    t = dispatch(root, new_task(root, "x", "codex").id, "codex.dev")
    assert t.status == "blocked:quota"
    assert [e["event"] for e in _events(root)].count("dispatch.start") == 1


def test_dispatch_failure(git_repo_with_harness, fake_bins, monkeypatch):
    monkeypatch.setenv("JAVIS_FAKE_MODE", "fail")
    root = git_repo_with_harness
    assert dispatch(root, new_task(root, "x", "codex").id, "codex.dev").status == "failed"


def test_dispatch_unknown_role(git_repo_with_harness, fake_bins):
    root = git_repo_with_harness
    with pytest.raises(HarnessError, match="unknown role"):
        dispatch(root, new_task(root, "x", "codex").id, "codex.nope")


def test_stale_lock_reclaimed(git_repo):
    lock = state_dir(git_repo) / "locks" / "codex.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(str(2**22 + 7), encoding="utf-8")
    with acquire_lock(git_repo, "codex"):
        assert lock.read_text(encoding="utf-8") == str(os.getpid())
    assert not lock.exists()


def test_busy_lock(git_repo):
    with acquire_lock(git_repo, "codex"):
        with pytest.raises(HarnessError, match="busy: codex"):
            with acquire_lock(git_repo, "codex"):
                pass
