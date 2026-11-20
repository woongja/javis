import pytest

from harness import ledger
from harness.cli import main
from harness.paths import state_dir


@pytest.mark.parametrize(("options", "group", "rows"), [
    ([], "team", ["claude 2 2.5 1 1", "codex 2 1.5 1 1"]),
    (["--by", "team"], "team", ["claude 2 2.5 1 1", "codex 2 1.5 1 1"]),
    (["--by", "model"], "model", ["model-a 2 2.5 0 1", "model-b 2 1.5 2 1"]),
    (["--by", "role"], "role", [
        "claude.reviewer 1 2.0 0 1", "codex.dev 2 1.5 1 1", "unknown 1 0.5 1 0",
    ]),
    (["--by", "task"], "task", ["T001 3 3.0 1 1", "T002 1 1.0 1 1"]),
])
def test_usage_groups_completed_runs(git_repo, monkeypatch, capsys, options, group, rows):
    monkeypatch.chdir(git_repo)
    ledger.append(git_repo, "dispatch.start", task="T001", role="codex.dev", model="model-a")
    ledger.append(git_repo, "dispatch.end", task="T001", role="codex.dev", model="model-a",
                  duration_s=30, exit=0, status="done")
    ledger.append(git_repo, "review.start", task="T001", role="claude.reviewer", model="model-a")
    ledger.append(git_repo, "review.end", task="T001", role="claude.reviewer", model="model-a",
                  duration_s=120, exit=0, quota_hit=True)
    ledger.append(git_repo, "dispatch.end", task="T001", tool="claude", model="model-b",
                  duration_s=30, exit=0, status="failed")
    ledger.append(git_repo, "dispatch.end", task="T002", role="codex.dev", model="model-b",
                  duration_s=60, exit=1, status="blocked:quota")
    with (state_dir(git_repo) / "ledger.jsonl").open("ab") as log:
        log.write(b'not json\n[]\n\xff\n{"event":"dispatch.end","duration_s":"invalid"}\n')

    assert main(["usage", *options]) == 0
    captured = capsys.readouterr()
    *table, footer = captured.out.splitlines()
    assert [" ".join(line.split()) for line in table] == [
        f"{group} runs minutes failed quota", *rows, "TOTAL 4 4.0 2 2",
    ]
    assert len({len(line) for line in table}) == 1
    assert footer == "skipped 4 bad lines"
    assert captured.err == ""


def test_usage_missing_ledger(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)

    assert main(["usage"]) == 0
    assert capsys.readouterr().out == "no runs yet\n"
