from pathlib import Path

import pytest

from harness.guards import GuardContext, check_command, check_path, scan_text
from harness.hooks import claude_pre


@pytest.fixture
def ctx(tmp_path) -> GuardContext:
    repo, home, brain = tmp_path / "repo", tmp_path / "home", tmp_path / "brain"
    for p in (repo, home, brain):
        p.mkdir()
    return GuardContext(repo=repo, home=home, brain_dir=brain, worktree=None)


@pytest.fixture
def worker_ctx(ctx, tmp_path) -> GuardContext:
    wt = tmp_path / "javis-wt" / "T001"
    wt.mkdir(parents=True)
    return GuardContext(repo=ctx.repo, home=ctx.home, brain_dir=ctx.brain_dir, worktree=wt)


def test_env_blocked_example_allowed(ctx):
    assert not check_path(ctx.repo / ".env", "read", ctx).allowed
    assert not check_path(ctx.repo / ".env.local", "read", ctx).allowed
    assert check_path(ctx.repo / ".env.example", "read", ctx).allowed


def test_secrets_dir_blocked(ctx):
    assert check_path(ctx.repo / "secrets" / "k.txt", "read", ctx).rule == "R1"


def test_brain_and_home_blocked(ctx):
    assert check_path(ctx.brain_dir / "daily" / "x.md", "read", ctx).rule == "R1"
    assert check_path(ctx.home / ".codex" / "auth.json", "read", ctx).rule == "R1"
    assert check_path(ctx.home / ".claude" / "settings.json", "write", ctx).rule == "R1"
    assert check_path(ctx.home / ".ssh" / "id_ed25519", "read", ctx).rule == "R1"


def test_case_insensitive_env(ctx):
    assert not check_path(ctx.repo / ".ENV", "write", ctx).allowed


def test_relative_path_resolved_against_repo(ctx):
    assert check_path(Path(".env"), "read", ctx).rule == "R1"
    assert check_path(Path("core/a.py"), "write", ctx).allowed


def test_ordinary_file_allowed(ctx):
    v = check_path(ctx.repo / "core" / "a.py", "write", ctx)
    assert v.allowed and v.rule is None


def test_worker_write_outside_worktree(worker_ctx):
    assert check_path(worker_ctx.repo / "core/a.py", "write", worker_ctx).rule == "R2"
    assert check_path(worker_ctx.worktree / "core/a.py", "write", worker_ctx).allowed
    assert check_path(worker_ctx.repo / "core/a.py", "read", worker_ctx).allowed


@pytest.mark.parametrize(
    "cmd",
    [
        "git commit -m x",
        "git -C . merge t",
        "git commit --no-verify",
        "git rebase main",
        "git reset --hard HEAD~1",
        "cd x && git commit -am y",
    ],
)
def test_git_blocked(cmd, ctx):
    assert check_command(cmd, ctx).rule == "R4"


@pytest.mark.parametrize(
    "cmd",
    [
        "git push",
        "git push origin",
        "git push origin main",
        "git push -u origin feat/x",
        " \tgit push origin main \t",
    ],
)
def test_lead_plain_push_allowed(cmd, ctx):
    assert check_command(cmd, ctx).allowed


@pytest.mark.parametrize(
    "cmd",
    [
        "git push --force",
        "git push -f origin main",
        "git push --force-with-lease",
        "git push --force-if-includes",
        "git push origin +main",
        "git push origin :main",
        "git push --delete origin main",
        "git push -d origin main",
        "git push --mirror",
        "git push --all",
        "git push --tags",
        "git push --prune",
        "git push --no-verify",
        "git push upstream main",
        "git push origin main other",
        "git push origin main; rm -rf x",
        "git push && git commit -m x",
        "git push || git status",
        "git push | cat",
        "git push\n",
        "\ngit push",
        "git push origin main\r\ngit status",
        "git push origin `git status`",
        "git push origin $(git status)",
    ],
)
def test_lead_push_outside_allowlist_blocked(cmd, ctx):
    assert check_command(cmd, ctx).rule == "R4"


@pytest.mark.parametrize("cmd", ["git push", "git push origin main"])
def test_worker_plain_push_blocked(cmd, worker_ctx):
    assert check_command(cmd, worker_ctx).rule == "R4"


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_claude_pre_allows_lead_plain_push(tool, ctx):
    payload = {"tool_name": tool, "tool_input": {"command": "git push origin main"}}
    assert claude_pre(payload, ctx) == (0, "")


@pytest.mark.parametrize("cmd", ["git status", "git diff main", "git log --oneline", "uv run pytest -q"])
def test_harmless_commands_allowed(cmd, ctx):
    assert check_command(cmd, ctx).allowed


def test_harness_merge_allowed(ctx):
    assert check_command("uv run harness merge T001", ctx).allowed
    assert check_command("harness merge T001", ctx).allowed


def test_scan_text():
    f = scan_text("path C:\\Users\\alice\\x\nok line\nme@acme.io harness:allow")  # harness:allow
    assert [x.line for x in f] == [1] and f[0].rule == "R7"


def test_scan_ignores_reserved_example_domains():
    assert scan_text("ci@example.invalid and t@example.com") == []


@pytest.mark.parametrize(
    "text",
    [
        "contact: bob@acme.io",  # harness:allow
        "/Users/bob/notes",  # harness:allow
        "/home/bob/x",  # harness:allow
        "-----BEGIN OPENSSH PRIVATE KEY-----",  # harness:allow
        "sk-" + "a" * 24,
        "ghp_" + "b" * 36,
        "xoxb-123",  # harness:allow
        "AKIA" + "A" * 16,
    ],
)
def test_scan_detects(text):
    assert len(scan_text(text)) == 1


def test_scan_clean_text():
    assert scan_text("def f(x):\n    return x  # no secrets here\n") == []


def test_lead_may_use_agent_memory_but_not_rest_of_claude_home(ctx, worker_ctx):
    mem = ctx.home / ".claude" / "projects" / "any-project" / "memory" / "note.md"
    assert check_path(mem, "write", ctx).allowed
    assert check_path(mem, "read", ctx).allowed
    assert not check_path(ctx.home / ".claude" / "settings.json", "write", ctx).allowed
    assert not check_path(ctx.home / ".claude" / "projects" / "p" / "session.jsonl", "read", ctx).allowed
    assert not check_path(mem, "read", worker_ctx).allowed  # workers never see memory
