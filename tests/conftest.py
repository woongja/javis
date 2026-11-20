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
    (git_repo / ".gitignore").write_text(".env\n.harness/\n.handoff/\n.task/\n.fake-args.txt\n", encoding="utf-8")
    run(["git", "add", ".gitignore"], git_repo)
    run(["git", "commit", "-q", "-m", "ignore state"], git_repo)
    return git_repo


def set_gate(root: Path, task_id: str, lines: list[str]) -> None:
    """Replace the ```gate block of a task spec with the given command lines."""
    spec = root / ".handoff" / task_id / "spec.md"
    text = spec.read_text(encoding="utf-8")
    head, _, tail = text.partition("```gate\n")
    _, _, rest = tail.partition("```")
    spec.write_text(head + "```gate\n" + "".join(l + "\n" for l in lines) + "```" + rest, encoding="utf-8")


HUD_EXISTS = f'"{sys.executable}" -c "import pathlib,sys; sys.exit(0 if pathlib.Path(\'hud/demo.txt\').exists() else 1)"'


@pytest.fixture
def dispatched_task(git_repo_with_harness, fake_bins) -> tuple[Path, str]:
    from harness.dispatch import dispatch
    from harness.tasks import new_task

    root = git_repo_with_harness
    task = new_task(root, "demo", "codex")
    dispatch(root, task.id, "codex.dev")
    return root, task.id


@pytest.fixture
def gated_task(dispatched_task, monkeypatch) -> tuple[Path, str]:
    from harness.gate import run_gate

    monkeypatch.delenv("JAVIS_BRAIN_DIR", raising=False)
    set_gate(*dispatched_task, [HUD_EXISTS])
    assert run_gate(*dispatched_task).passed
    return dispatched_task


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
