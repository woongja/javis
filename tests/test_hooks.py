import io
import json

import pytest

from harness.cli import main
from harness.guards import GuardContext
from harness.hooks import claude_pre, git_pre_commit
from tests.conftest import run


@pytest.fixture
def ctx(git_repo, tmp_path) -> GuardContext:
    home = tmp_path / "home"
    home.mkdir()
    return GuardContext(repo=git_repo, home=home, brain_dir=None, worktree=None)


def test_claude_pre_blocks_env(ctx):
    code, msg = claude_pre({"tool_name": "Read", "tool_input": {"file_path": str(ctx.repo / ".env")}}, ctx)
    assert code == 2 and msg.startswith("R1")


def test_claude_pre_blocks_git_commit(ctx):
    assert claude_pre({"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}, ctx)[0] == 2


def test_claude_pre_grep_path(ctx):
    payload = {"tool_name": "Grep", "tool_input": {"pattern": "x", "path": str(ctx.home / ".ssh")}}
    assert claude_pre(payload, ctx)[0] == 2
    assert claude_pre({"tool_name": "Grep", "tool_input": {"pattern": "x"}}, ctx) == (0, "")


def test_claude_pre_allows_unknown_tool(ctx):
    assert claude_pre({"tool_name": "WebSearch", "tool_input": {}}, ctx) == (0, "")


def test_claude_pre_allows_normal_edit(ctx):
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(ctx.repo / "core" / "a.py")}}
    assert claude_pre(payload, ctx) == (0, "")


def test_pre_commit_blocks_agent(git_repo):
    (git_repo / "a.txt").write_text("x\n", encoding="utf-8")
    run(["git", "add", "a.txt"], git_repo)
    assert git_pre_commit(git_repo, {"JAVIS_AGENT": "1"})[0] != 0
    assert git_pre_commit(git_repo, {"JAVIS_AGENT": "1", "JAVIS_MERGE": "1"})[0] == 0
    assert git_pre_commit(git_repo, {})[0] == 0


def test_pre_commit_blocks_user_path(git_repo):
    (git_repo / "b.md").write_text("see /Users/bob/notes\n", encoding="utf-8")  # harness:allow
    run(["git", "add", "b.md"], git_repo)
    code, msg = git_pre_commit(git_repo, {})
    assert code != 0 and "R7" in msg


def test_pre_commit_blocks_env_file(git_repo):
    (git_repo / ".env").write_text("K=v\n", encoding="utf-8")
    run(["git", "add", "-f", ".env"], git_repo)
    code, msg = git_pre_commit(git_repo, {})
    assert code != 0 and "R1" in msg


def test_cli_claude_pre_stdin(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(git_repo / ".env")}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert main(["hook", "claude-pre"]) == 2
    assert "R1" in capsys.readouterr().err


def test_cli_scan_all(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    assert main(["hook", "scan-all"]) == 0
    (git_repo / "leak.txt").write_text("token ghp_" + "c" * 36 + "\n", encoding="utf-8")
    run(["git", "add", "leak.txt"], git_repo)
    assert main(["hook", "scan-all"]) == 1
    assert "leak.txt" in capsys.readouterr().out


def test_claude_pre_blocks_git_commit_via_powershell(ctx):
    assert claude_pre({"tool_name": "PowerShell", "tool_input": {"command": "git commit -m x"}}, ctx)[0] == 2