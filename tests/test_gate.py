import json
import sys

from harness.cli import main
from harness.gate import parse_gate_commands, run_gate
from harness.paths import handoff_dir, worktree_dir
from harness.tasks import load
from tests.conftest import HUD_EXISTS, set_gate


def test_parse_crlf():
    spec = "## 완료 기준\r\n```gate\r\nuv run pytest -q\r\n# note\r\n\r\npython -c \"print(1)\"\r\n```\r\n"
    assert parse_gate_commands(spec) == [["uv", "run", "pytest", "-q"], ["python", "-c", "print(1)"]]


def test_parse_no_block():
    assert parse_gate_commands("## 완료 기준\nnothing\n") == []


def test_empty_gate_fails(dispatched_task):
    result = run_gate(*dispatched_task)
    assert not result.passed and load(*dispatched_task).status == "done"


def test_gate_pass(dispatched_task):
    set_gate(*dispatched_task, [HUD_EXISTS])
    result = run_gate(*dispatched_task)
    assert result.passed and result.results[0]["exit"] == 0 and len(result.tree) == 40
    saved = json.loads((handoff_dir(*dispatched_task) / "gate.json").read_text(encoding="utf-8"))
    assert saved["passed"] is True
    task = load(*dispatched_task)
    assert task.status == "gated" and task.gate_tree == result.tree


def test_gate_failing_command(dispatched_task):
    set_gate(*dispatched_task, [f'"{sys.executable}" -c "import sys; sys.exit(4)"'])
    result = run_gate(*dispatched_task)
    assert not result.passed and result.results[0]["exit"] == 4


def test_gate_missing_program(dispatched_task):
    set_gate(*dispatched_task, ["definitely-not-a-program-xyz --flag"])
    result = run_gate(*dispatched_task)
    assert not result.passed and result.results[0]["exit"] == 127


def test_gate_blocks_r7(dispatched_task):
    root, task_id = dispatched_task
    set_gate(root, task_id, [HUD_EXISTS])
    (worktree_dir(root, task_id) / "hud" / "note.md").write_text("see /Users/bob/x\n", encoding="utf-8")  # harness:allow
    result = run_gate(root, task_id)
    assert not result.passed and any("R7" in r["cmd"] for r in result.results)


def test_cli_gate_exit_code(dispatched_task, monkeypatch):
    root, task_id = dispatched_task
    monkeypatch.chdir(root)
    assert main(["gate", task_id]) == 1
    set_gate(root, task_id, [HUD_EXISTS])
    assert main(["gate", task_id]) == 0
