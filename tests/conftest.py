import subprocess
import sys
from pathlib import Path

import pytest


def run(cmd: list[str], cwd: Path, env: dict | None = None) -> str:
    """Run a command, raise on failure, return stdout."""
    return subprocess.run(
        cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


FAKE_AGENT = Path(__file__).parent / "fakes" / "fake_agent.py"


def make_fake_bin(folder: Path, name: str) -> Path:
    """Executable wrapper that runs fake_agent.py with the test interpreter."""
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        exe = folder / f"{name}.cmd"
        exe.write_text(f'@"{sys.executable}" "{FAKE_AGENT}" %*\r\n', encoding="utf-8")
    else:
        exe = folder / name
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_AGENT}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    return exe


@pytest.fixture
def fake_bins(tmp_path, monkeypatch) -> dict[str, Path]:
    bins = {tool: make_fake_bin(tmp_path / "bin", tool) for tool in ("claude", "codex")}
    monkeypatch.setenv("JAVIS_CLAUDE_BIN", str(bins["claude"]))
    monkeypatch.setenv("JAVIS_CODEX_BIN", str(bins["codex"]))
    monkeypatch.setenv("JAVIS_FAKE_MODE", "ok")
    return bins


@pytest.fixture
def git_repo_with_harness(git_repo) -> Path:
    """A repo whose .gitignore hides harness state, like the real project."""
    (git_repo / ".gitignore").write_text(".harness/\n.handoff/\n.task/\n.fake-args.txt\n", encoding="utf-8")
    run(["git", "add", ".gitignore"], git_repo)
    run(["git", "commit", "-q", "-m", "ignore state"], git_repo)
    return git_repo


@pytest.fixture
def git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo dir"
    repo.mkdir()
    run(["git", "init", "-q", "-b", "main"], repo)
    run(["git", "config", "user.email", "t@example.invalid"], repo)
    run(["git", "config", "user.name", "t"], repo)
    run(["git", "config", "commit.gpgsign", "false"], repo)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-q", "-m", "init"], repo)
    return repo
