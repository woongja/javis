import subprocess
from pathlib import Path

import pytest


def run(cmd: list[str], cwd: Path, env: dict | None = None) -> str:
    """Run a command, raise on failure, return stdout."""
    return subprocess.run(
        cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


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
