import io
import json
from pathlib import Path

import pytest

from harness.cli import main
from harness.guards import GuardContext
from harness.hooks import claude_pre, git_pre_commit, scan_all
from tests.conftest import run


@pytest.fixture
def ctx(git_repo, tmp_path) -> GuardContext:
    home = tmp_path / "home"
    home.mkdir()
    return GuardContext(repo=git_repo, home=home, brain_dir=None, worktree=None)


def test_claude_pre_blocks_env(ctx):
    code, msg = claude_pre({"tool_name": "Read", "tool_input": {"file_path": str(ctx.repo / ".env")}}, ctx)
    assert code == 2 and msg.startswith("R1")


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
def test_claude_pre_blocks_brain_read_from_dotenv(git_repo, tmp_path, monkeypatch, relative):
    monkeypatch.delenv("JAVIS_BRAIN_DIR", raising=False)
    monkeypatch.delenv("JAVIS_WORKTREE", raising=False)
    brain = tmp_path / "brain"
    value = "../brain" if relative else str(brain)
    (git_repo / ".env").write_text(f"JAVIS_BRAIN_DIR={value}\n", encoding="utf-8")
    subdir = git_repo / "subdir"
    subdir.mkdir()
    monkeypatch.chdir(subdir)
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(brain / "note.md")}}
    ctx = GuardContext.from_env(git_repo)
    assert ctx.brain_dir == brain.resolve()
    assert claude_pre(payload, ctx) == (2, "R1: brain vault is off limits")


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


def test_pre_commit_hook_runs_harness_without_sync():
    hook = Path(__file__).resolve().parents[1] / ".githooks" / "pre-commit"
    assert "exec uv run --no-sync --quiet harness hook git-pre-commit" in hook.read_text(
        encoding="utf-8"
    ).splitlines()


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


@pytest.mark.parametrize("source", ["absolute-dotenv", "relative-dotenv", "injected-env"])
def test_pre_commit_blocks_staged_brain_file(git_repo, monkeypatch, source):
    monkeypatch.delenv("JAVIS_BRAIN_DIR", raising=False)
    brain = git_repo / "brain"
    brain.mkdir()
    value = str(brain) if source == "absolute-dotenv" else "brain"
    env = {}
    if source == "injected-env":
        env["JAVIS_BRAIN_DIR"] = value
        value = "other-brain"
    (git_repo / ".env").write_text(f"JAVIS_BRAIN_DIR={value}\n", encoding="utf-8")
    (brain / "note.md").write_text("note\n", encoding="utf-8")
    run(["git", "add", "brain/note.md"], git_repo)
    subdir = git_repo / "subdir"
    subdir.mkdir()
    monkeypatch.chdir(subdir)
    assert git_pre_commit(git_repo, env) == (1, "R1: brain/note.md: brain vault is off limits")


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


def test_cli_scan_history_finds_removed_line(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    user_path = "C:" + "\\Users\\" + "alice"
    filename = "old notes.txt"
    notes = git_repo / filename
    notes.write_text(f"saved at {user_path}\n", encoding="utf-8")
    (git_repo / "binary.dat").write_bytes(b"\0" + user_path.encode("utf-8"))
    run(["git", "add", filename, "binary.dat"], git_repo)
    run(["git", "commit", "-qm", "add old notes"], git_repo)
    original = run(["git", "rev-parse", "--short", "HEAD"], git_repo).strip()
    notes.write_text("clean\n", encoding="utf-8")
    (git_repo / "binary.dat").unlink()
    run(["git", "add", "-u"], git_repo)
    run(["git", "commit", "-qm", "remove old data"], git_repo)

    assert scan_all(git_repo) == []
    assert main(["hook", "scan-all"]) == 0
    assert capsys.readouterr().out == ""
    assert main(["hook", "scan-all", "--history"]) == 1
    output = capsys.readouterr().out
    assert output == f"{original} {filename}: windows user path on line 1\n"
    assert user_path not in output


def test_cli_scan_history_clean(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    assert main(["hook", "scan-all", "--history"]) == 0
    assert capsys.readouterr().out == ""


def test_cli_scan_history_honors_allow_marker(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    user_path = "C:" + "\\Users\\" + "alice"
    (git_repo / "example.txt").write_text(f"{user_path}  # harness:allow\n", encoding="utf-8")
    run(["git", "add", "example.txt"], git_repo)
    run(["git", "commit", "-qm", "add allowed example"], git_repo)

    assert main(["hook", "scan-all", "--history"]) == 0
    assert capsys.readouterr().out == ""


def test_claude_pre_blocks_git_commit_via_powershell(ctx):
    assert claude_pre({"tool_name": "PowerShell", "tool_input": {"command": "git commit -m x"}}, ctx)[0] == 2
