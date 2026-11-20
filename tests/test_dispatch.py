import json
import os
import shlex
import tomllib

import pytest

from harness import HarnessError
from harness.dispatch import dispatch
from harness.paths import handoff_dir, state_dir, worktree_dir
from harness.roles import Role
from harness.runner import acquire_lock, build_command, safe_env
from harness.tasks import load, new_task


def test_safe_env_drops_api_keys():
    e = safe_env({"PATH": "p", "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", "JAVIS_TASK": "T1"}, {"JAVIS_AGENT": "1"})
    assert "ANTHROPIC_API_KEY" not in e and "OPENAI_API_KEY" not in e
    assert e["JAVIS_AGENT"] == "1" and e["JAVIS_TASK"] == "T1" and e["PATH"] == "p"


def test_codex_windows_sandbox_arg(fake_bins, git_repo, monkeypatch):
    role = Role("codex.dev", "codex", "gpt-6.2-sol", "medium")
    monkeypatch.setattr("harness.platform.is_windows", lambda: True)
    cmd = build_command(role, git_repo)
    assert cmd[-3:-1] == ["-c", 'windows.sandbox="elevated"'] and cmd[-1] == "-"
    monkeypatch.setattr("harness.platform.is_windows", lambda: False)
    assert 'windows.sandbox="elevated"' not in build_command(role, git_repo)


@pytest.mark.parametrize("windows", [False, True])
def test_build_codex_command(fake_bins, git_repo, tmp_path, monkeypatch, windows):
    monkeypatch.setattr("harness.platform.is_windows", lambda: windows)
    scripts = tmp_path / ("lead's Scripts" if windows else "lead's \"test\" bin")
    monkeypatch.setattr("harness.runner.sysconfig.get_path", lambda _: str(scripts))
    brain = tmp_path / "example brain"
    repo = git_repo.rename(tmp_path / "lead's project")
    cmd = build_command(Role("codex.dev", "codex", "gpt-6.2-sol", "medium"), repo, brain)
    assert cmd[0] == str(fake_bins["codex"])
    assert cmd[1:11] == ["exec", "-m", "gpt-6.2-sol", "-c", 'model_reasoning_effort="medium"',
                         "--ignore-user-config", "-C", str(repo), "-s", "workspace-write"]
    assert "--dangerously-bypass-hook-trust" in cmd and cmd[-1] == "-"
    config = next(value for flag, value in zip(cmd, cmd[1:])
                  if flag == "-c" and value.startswith("hooks.PreToolUse="))
    group, = tomllib.loads("x = " + config.split("=", 1)[1])["x"]
    hook, = group["hooks"]
    assert group["matcher"] == ".*" and hook["type"] == "command" and hook["timeout"] == 30
    if windows:
        expected = f"& '{scripts / 'harness.exe'}' hook codex-pre --repo '{repo}'"
        assert hook["command"] == expected.replace("lead's", "lead''s")
    else:
        assert shlex.split(hook["command"]) == [str(scripts / "harness"), "hook", "codex-pre", "--repo", str(repo)]


def test_build_claude_command(fake_bins, tmp_path, monkeypatch):
    monkeypatch.setenv("JAVIS_BRAIN_DIR", "example brain")
    cmd = build_command(Role("claude.dev", "claude", "claude-opus-5-5", "medium"), tmp_path)
    assert cmd[1:] == ["-p", "--model", "claude-opus-5-5", "--effort", "medium",
                       "--setting-sources", "project,local", "--permission-mode", "acceptEdits"]


def test_build_claude_command_with_brain(fake_bins, tmp_path):
    brain = tmp_path / "example brain"
    cmd = build_command(Role("claude.dev", "claude", "claude-opus-5-5", "medium"), tmp_path, brain)
    assert cmd[-2] == "--settings"
    assert json.loads(cmd[-1]) == {
        "sandbox": {"enabled": True, "filesystem": {"denyRead": [str(brain)]}}
    }


@pytest.mark.parametrize("shim", ["x.cmd", "x.bat"])
def test_build_claude_command_with_brain_and_windows_shim(monkeypatch, tmp_path, shim):
    monkeypatch.setattr("harness.runner.find_exe", lambda *_: tmp_path / shim)
    metacharacters = "%&|<>()!^"
    brain = tmp_path / f"example brain {metacharacters}"
    cmd = build_command(Role("claude.dev", "claude", "claude-opus-5-5", "medium"), tmp_path, brain)
    assert cmd[-2] == "--settings"
    assert json.loads(cmd[-1]) == {
        "sandbox": {"enabled": True, "filesystem": {"denyRead": [str(brain)]}}
    }
    assert not set(metacharacters).intersection(cmd[-1])


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
    wt = worktree_dir(root, t.id)
    assert (wt / "hud" / "demo.txt").exists()
    args = (wt / ".fake-args.txt").read_text(encoding="utf-8").splitlines()
    config = next(arg for arg in args if arg.startswith("hooks.PreToolUse="))
    hook = tomllib.loads(config)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert hook.endswith(f"hook codex-pre --repo '{root}'")
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


@pytest.mark.parametrize("brain_in_process_env", [False, True])
def test_dispatch_passes_brain_dir(git_repo_with_harness, fake_bins, monkeypatch, brain_in_process_env):
    root = git_repo_with_harness
    dotenv_brain = "../brain from dotenv & %TEMP% (example)"
    (root / ".env").write_text(f"JAVIS_BRAIN_DIR={dotenv_brain}\n", encoding="utf-8")
    if brain_in_process_env:
        brain = "../brain from process env"
        monkeypatch.setenv("JAVIS_BRAIN_DIR", brain)
    else:
        brain = dotenv_brain
        monkeypatch.delenv("JAVIS_BRAIN_DIR", raising=False)

    caller = root.parent / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    task = dispatch(root, new_task(root, "brain guard", "claude").id, "claude.dev")
    wt = worktree_dir(root, task.id)
    assert not (wt / ".env").exists()
    args = (wt / ".fake-args.txt").read_text(encoding="utf-8").splitlines()
    brain_path = str((root / brain).resolve())
    assert f"brain={brain_path}" in args
    assert json.loads(args[args.index("--settings") + 1])["sandbox"]["filesystem"]["denyRead"] == [brain_path]


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
