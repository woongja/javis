import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from core import cli
from worklog.store import Store

from tests.test_core_engine import fake_claude, success


def test_ask_stores_and_log_lists_answer(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path / "data"))
    texts = ["1. A", "**1. A** – b", "1) A", "답변", "2026년 뉴스", "2026. 11. 21. Anthropic 발표"]
    envelope = success([{"text": text, "sources": ["https://example.org/news"]} for text in texts])
    shim = fake_claude(tmp_path, f"print(json.dumps({envelope!r}))\n")
    # The same injection point used by the engine tests, through the real CLI.
    engine_class = cli.ClaudeEngine
    monkeypatch.setattr(cli, "ClaudeEngine", lambda settings: engine_class(settings, shim))
    now = datetime(2026, 11, 21, 16, 41, 49, 316525, tzinfo=UTC)
    handle = cli.handle
    monkeypatch.setattr(cli, "handle", lambda request, store, engine: handle(request, store, engine, now))
    assert cli.main(["ask", "오늘 뉴스"]) == 0
    out = capsys.readouterr().out
    [record] = Store.open(tmp_path / "data").recent(10)
    assert out.splitlines()[:12:2] == [
        "1. A",
        "2. **A** – b",
        "3. A",
        "4. 답변",
        "5. 2026년 뉴스",
        "6. 2026. 11. 21. Anthropic 발표",
    ]
    assert "https://example.org/news" in out and f"기록 ID {record.id}" in out
    assert [item.text for item in record.items] == texts
    assert (record.channel, record.status, record.model) == ("cli", "ok", "claude-sonnet-5-5")

    class LocalDateTime(datetime):
        def astimezone(self, tz=None):
            assert tz is None
            return super().astimezone(timezone(timedelta(hours=9)))

    monkeypatch.setattr(cli, "datetime", LocalDateTime)
    assert cli.main(["log", "-n", "1"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"{record.id}\t2026-11-22 01:41\t오늘 뉴스\tok"]
    assert Store.open(tmp_path / "data").get(record.id).created_at == now.isoformat()
    assert cli.main(["log", "-n", "0"]) == 0
    assert not capsys.readouterr().out


def test_ask_failure_is_logged_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.engine.shutil.which", lambda name: None)
    assert cli.main(["ask", "뉴스"]) != 0
    [record] = Store.open(tmp_path).recent(10)
    assert record.status == "error" and record.reason == "configuration"
    assert f"기록 ID {record.id}" in capsys.readouterr().out


def test_store_failure_emits_no_answer(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path / "data"))
    envelope = success([{"text": "unsaved", "sources": []}])
    shim = fake_claude(tmp_path, f"print(json.dumps({envelope!r}))\n")
    engine_class = cli.ClaudeEngine
    monkeypatch.setattr(cli, "ClaudeEngine", lambda settings: engine_class(settings, shim))

    def fail_save(self, record):
        raise sqlite3.OperationalError("fake failure")

    monkeypatch.setattr(Store, "save", fail_save)
    assert cli.main(["ask", "뉴스"]) != 0
    output = capsys.readouterr()
    assert not output.out and "일지 처리" in output.err


def test_data_dir_inside_checkout_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    in_repo = Path(__file__).resolve().parents[1] / "unused-data-directory"
    monkeypatch.setenv("JAVIS_DATA_DIR", str(in_repo))
    assert cli.main(["log"]) != 0
    assert not in_repo.exists()
    assert "JAVIS_DATA_DIR" in capsys.readouterr().err


def test_data_dir_inside_unrelated_git_repository_is_allowed(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    directory = tmp_path / "data"
    monkeypatch.setenv("JAVIS_DATA_DIR", str(directory))
    assert cli.main(["log"]) == 0
    assert directory.is_dir()


def test_default_data_dir_inside_checkout_is_refused(monkeypatch, capsys):
    checkout = Path(__file__).resolve().parents[1]
    monkeypatch.delenv("JAVIS_DATA_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: checkout)
    assert cli.main(["log"]) != 0
    assert "~/.javis" in capsys.readouterr().err
