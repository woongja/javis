import json
import sys

import pytest

from harness.cli import main
from harness.gate import _r3_check, parse_gate_commands, run_gate
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


def test_gate_checks_scope_from_spec(dispatched_task):
    set_gate(*dispatched_task, [HUD_EXISTS])
    spec = handoff_dir(*dispatched_task) / "spec.md"
    gate_file, test_dir = (r"HARNESS\GATE.PY", "TESTS/") if sys.platform == "win32" else (r"harness\gate.py", "tests/")
    spec.write_text(spec.read_text(encoding="utf-8").replace(
        "## 건드릴 경로\n", f"## 건드릴 경로 (설명)\n- `{gate_file}`, `{test_dir}`, `hud/` (설명)\n"
    ) + "\n## Other paths\n- `harness/gate.py.bak`\n", encoding="utf-8")
    wt = worktree_dir(*dispatched_task)
    for path in (wt / "harness/gate.py", wt / "tests/sub/범위 확인.txt"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("scope fixture\n", encoding="utf-8")
    assert _r3_check(wt, spec.read_text(encoding="utf-8"), "codex") is None

    (wt / "harness/gate.py.bak").write_text("outside scope\n", encoding="utf-8")
    scope_failure = {"cmd": "R3 scope: harness/gate.py.bak", "exit": 1, "duration_s": 0}
    assert _r3_check(wt, spec.read_text(encoding="utf-8"), "codex") == scope_failure
    result = run_gate(*dispatched_task)
    assert not result.passed
    assert scope_failure in result.results
    saved = json.loads((handoff_dir(*dispatched_task) / "gate.json").read_text(encoding="utf-8"))
    assert saved["results"] == result.results


@pytest.mark.parametrize("section", ["", "## 건드릴 경로\n- 설명만 있음\n"])
def test_gate_falls_back_to_task_ownership(dispatched_task, section, monkeypatch):
    monkeypatch.setattr("harness.gate.load_ownership", lambda: {"codex": ["hud/", ""]})
    set_gate(*dispatched_task, [HUD_EXISTS])
    spec = handoff_dir(*dispatched_task) / "spec.md"
    spec.write_text(spec.read_text(encoding="utf-8").replace("## 건드릴 경로\n", section), encoding="utf-8")
    assert run_gate(*dispatched_task).passed

    wt = worktree_dir(*dispatched_task)
    (wt / "README.md").write_text("modified outside scope\n", encoding="utf-8")
    result = run_gate(*dispatched_task)
    assert not result.passed and result.results[-1]["cmd"] == "R3 scope: README.md"


@pytest.mark.parametrize("allowed, outside", [("README.md", "hud/moved.md"), ("hud/", "README.md")])
def test_gate_checks_both_ends_of_rename(dispatched_task, allowed, outside):
    set_gate(*dispatched_task, [HUD_EXISTS])
    spec = handoff_dir(*dispatched_task) / "spec.md"
    spec.write_text(spec.read_text(encoding="utf-8").replace(
        "## 건드릴 경로\n", f"## 건드릴 경로\n- `hud/demo.txt`, `{allowed}`\n"
    ), encoding="utf-8")
    wt = worktree_dir(*dispatched_task)
    (wt / "README.md").rename(wt / "hud/moved.md")
    result = run_gate(*dispatched_task)
    assert not result.passed and result.results[-1]["cmd"] == f"R3 scope: {outside}"


def test_cli_gate_exit_code(dispatched_task, monkeypatch):
    root, task_id = dispatched_task
    monkeypatch.chdir(root)
    assert main(["gate", task_id]) == 1
    set_gate(root, task_id, [HUD_EXISTS])
    assert main(["gate", task_id]) == 0
