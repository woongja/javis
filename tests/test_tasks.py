import json

import pytest

from harness import HarnessError, ledger
from harness.cli import main
from harness.tasks import bump_handoff, load, new_task, save


def test_new_task_ids(git_repo):
    a = new_task(git_repo, "로그 패널 만들기", "codex")
    b = new_task(git_repo, "x", "claude")
    assert (a.id, b.id) == ("T001", "T002") and a.branch == "task/T001"
    assert a.status == "new" and a.handoffs == 0 and a.gate_tree is None
    spec = (git_repo / ".handoff/T001/spec.md").read_text(encoding="utf-8")
    assert "```gate" in spec and "## 완료 기준" in spec and "로그 패널 만들기" in spec


def test_owner_validated(git_repo):
    with pytest.raises(HarnessError, match="owner"):
        new_task(git_repo, "x", "gemini")


def test_roundtrip(git_repo):
    t = new_task(git_repo, "x", "codex")
    t.status = "done"
    save(git_repo, t)
    assert load(git_repo, t.id).status == "done"


def test_load_missing(git_repo):
    with pytest.raises(HarnessError, match="unknown task"):
        load(git_repo, "T999")


def test_handoff_limit(git_repo):
    t = new_task(git_repo, "x", "codex")
    for _ in range(4):
        bump_handoff(git_repo, t.id)
    with pytest.raises(HarnessError, match="handoff limit 4"):
        bump_handoff(git_repo, t.id)


def test_ledger_jsonl(git_repo):
    ledger.append(git_repo, "task.new", task="T001")
    line = json.loads((git_repo / ".harness/ledger.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert line["event"] == "task.new" and line["ts"].endswith("Z") and line["task"] == "T001"


def test_cli_task_new(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    assert main(["task", "new", "첫 작업", "--owner", "claude"]) == 0
    assert capsys.readouterr().out.strip() == "T001"
    events = [json.loads(l)["event"] for l in (git_repo / ".harness/ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events == ["task.new"]
