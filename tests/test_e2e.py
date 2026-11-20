"""Whole flow through the CLI with fake agents: task new → dispatch → gate → merge."""
import json

from harness.cli import main
from harness.paths import state_dir
from tests.conftest import HUD_EXISTS, set_gate


def test_full_flow(git_repo_with_harness, fake_bins, monkeypatch, capsys):
    root = git_repo_with_harness
    monkeypatch.chdir(root)
    assert main(["task", "new", "HUD 데모 파일", "--owner", "codex"]) == 0
    task_id = capsys.readouterr().out.strip()
    set_gate(root, task_id, [HUD_EXISTS])
    assert main(["dispatch", task_id, "--role", "codex.dev"]) == 0
    assert main(["gate", task_id]) == 0
    assert main(["merge", task_id]) == 0
    assert (root / "hud" / "demo.txt").exists()
    events = [json.loads(l)["event"] for l in (state_dir(root) / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events == ["task.new", "dispatch.start", "dispatch.end", "gate", "merge"]
