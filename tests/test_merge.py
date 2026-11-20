import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import harness.merge as merge_module
from harness import HarnessError
from harness.cli import main
from harness.merge import merge
from harness.paths import worktree_dir
from harness.tasks import load
from tests.conftest import run

HARNESS_SRC = Path(__file__).resolve().parents[1]


def test_merge_happy(gated_task, monkeypatch):
    root, task_id = gated_task
    archive = Mock()
    monkeypatch.setattr(merge_module.shutil, "copytree", archive)
    sha, warnings = merge(root, task_id)
    assert warnings == []
    archive.assert_not_called()
    assert run(["git", "log", "-1", "--format=%s"], root).strip() == f"feat(codex)({task_id}): demo"
    assert run(["git", "rev-parse", "HEAD"], root).strip() == sha
    assert (root / "hud" / "demo.txt").exists()
    assert not worktree_dir(root, task_id).exists()
    assert "task/" not in run(["git", "branch"], root)
    assert load(root, task_id).status == "merged"


def test_merge_cli_continues_cleanup_after_worktree_removal_fails(gated_task, monkeypatch, tmp_path, capsys):
    root, task_id = gated_task
    task = load(root, task_id)
    brain = tmp_path / "brain"
    git = merge_module._git

    def fail_worktree_remove(cwd, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            raise HarnessError("directory locked\naccess denied")
        return git(cwd, *args, **kwargs)

    monkeypatch.chdir(root)
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(brain))
    monkeypatch.setattr(merge_module, "_git", fail_worktree_remove)

    assert main(["merge", task_id]) == 0

    sha = run(["git", "rev-parse", "main"], root).strip()
    assert run(["git", "log", "-1", "--format=%s", "main"], root).strip() == f"feat(codex)({task_id}): demo"
    assert (root / "hud" / "demo.txt").exists()
    assert load(root, task_id).status == "merged"
    assert worktree_dir(root, task_id).exists()
    assert not run(["git", "branch", "--list", task.branch], root).strip()
    assert (brain / "javis-dev" / "handoff" / task_id / "spec.md").exists()
    captured = capsys.readouterr()
    assert captured.out == f"{sha}\n"
    assert captured.err == f"warning: merged as {sha[:7]}, but worktree removal failed: directory locked access denied\n"


def test_merge_rejects_ungated(dispatched_task):
    with pytest.raises(HarnessError, match="not gated"):
        merge(*dispatched_task)


def test_merge_rejects_changed_after_gate(gated_task):
    (worktree_dir(*gated_task) / "hud" / "new.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(HarnessError, match="changed since gate"):
        merge(*gated_task)


def test_merge_rejects_dirty_main(gated_task):
    (gated_task[0] / "stray.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(HarnessError, match="main not clean"):
        merge(*gated_task)


@pytest.mark.parametrize("use_process_env", [False, True], ids=["dotenv-only", "process-env-wins"])
def test_merge_archives_handoff(gated_task, tmp_path, monkeypatch, use_process_env):
    root, task_id = gated_task
    dotenv_brain = tmp_path / "dotenv brain"
    (root / ".env").write_text(f"JAVIS_BRAIN_DIR={dotenv_brain}\n", encoding="utf-8")
    brain = dotenv_brain
    if use_process_env:
        brain = tmp_path / "process brain"
        monkeypatch.setenv("JAVIS_BRAIN_DIR", str(brain))

    _, warnings = merge(root, task_id)

    assert warnings == []
    assert (brain / "javis-dev" / "handoff" / task_id / "spec.md").is_file()
    if use_process_env:
        assert not dotenv_brain.exists()


def test_merge_reports_branch_and_archive_failures(gated_task, tmp_path, monkeypatch):
    git = merge_module._git

    def fail_branch_delete(cwd, *args, **kwargs):
        if args[:2] == ("branch", "-D"):
            raise HarnessError("branch ref locked")
        return git(cwd, *args, **kwargs)

    def fail_archive(*args, **kwargs):
        assert load(*gated_task).status == "merged"
        raise OSError("disk full")

    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(tmp_path / "brain"))
    monkeypatch.setattr(merge_module, "_git", fail_branch_delete)
    monkeypatch.setattr(merge_module.shutil, "copytree", fail_archive)

    _, warnings = merge(*gated_task)

    assert warnings == [
        "branch deletion failed: branch ref locked",
        "archive to brain failed: disk full",
    ]


@pytest.fixture
def gated_task_with_hook(git_repo_with_harness, monkeypatch, request):
    root = git_repo_with_harness
    hook = root / ".githooks" / "pre-commit"
    hook.parent.mkdir()
    hook.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" -m harness.cli hook git-pre-commit\n',
                    encoding="utf-8")
    hook.chmod(0o755)
    run(["git", "add", ".githooks"], root)
    run(["git", "commit", "-q", "-m", "hooks"], root)
    run(["git", "config", "core.hooksPath", ".githooks"], root)
    monkeypatch.setenv("PYTHONPATH", str(HARNESS_SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return request.getfixturevalue("gated_task")


def test_pre_commit_hook_allows_merge(gated_task_with_hook):
    root, task_id = gated_task_with_hook
    wt = worktree_dir(root, task_id)
    assert (wt / ".githooks" / "pre-commit").is_file()
    assert run(["git", "config", "--get", "core.hooksPath"], wt).strip() == ".githooks"
    merge(root, task_id)
    assert load(root, task_id).status == "merged"
