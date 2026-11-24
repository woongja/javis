import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from channels.telegram.bot import TelegramBot
from core import cli
from core.gateway import Request, handle
from teams import brain
from tests.test_telegram import CHAT, TOKEN, FakeEngine, update
from tests.test_worklog_store import SAMPLE
from worklog import Item, Store
from worklog import search as search_module


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path))
    return Store.open(tmp_path)


def test_forget_ids_cleans_up_search_and_delivery(store, tmp_path, monkeypatch, capsys):
    question = "discardedquestion marker beyond the forty-character preview"
    answer = "discardedanswer"
    record_id = store.save(
        replace(SAMPLE, channel="telegram", request=question, items=(Item(answer, ()),), update_key="40:101"),
        chunks=["discardeddelivery"],
    )
    reference_id = store.save(replace(SAMPLE, kind="recall", ref_id=record_id))
    store.set_offset(41)
    cursor = store.get_offset()
    assert store.search(answer)[1][0].id == record_id

    assert cli.main(["forget", str(record_id), "9999", "--yes"]) == 0
    output = capsys.readouterr().out
    assert f"\task\t{question[:40]}" in output
    assert question not in output and answer not in output
    assert "9999\t없음" in output
    assert output.endswith("삭제 1건 · 건너뜀 1건\n")

    assert store.get(record_id) is None
    assert store.get(reference_id) == replace(SAMPLE, id=reference_id, kind="recall")
    assert store.get_offset() == cursor
    with closing(sqlite3.connect(tmp_path / "javis.db")) as db:
        assert db.execute("SELECT count(*) FROM deliveries").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM search_trigram WHERE rowid = ?", (record_id,)).fetchone() == (0,)
    assert cli.main(["search", answer]) == 0
    assert "일치 없음" in capsys.readouterr().out

    engine = FakeEngine()
    sends = []
    updates = [update(id=41, text=f"기록 검색 {answer}"), update(id=42, text=f"출처 {record_id}")]

    def request(method, payload):
        if method == "getUpdates":
            return updates
        assert method == "sendMessage"
        sends.append(payload["text"])

    bot = TelegramBot(TOKEN, CHAT, Store.open(tmp_path), engine)
    assert bot._fresh_offset() == 41
    monkeypatch.setattr(bot, "_request", request)
    bot.poll_once()
    assert "일치 없음" in sends[0]
    assert "회수할 답변 기록을 찾지 못했습니다." in sends[1]
    assert len(sends) == 2 and engine.calls == []

    new_id = store.save(replace(SAMPLE, request="freshquestion", update_key="43:101"), chunks=["freshdelivery"])
    assert [row.id for row in store.search("freshquestion")[1]] == [new_id]
    restarted = TelegramBot(TOKEN, CHAT, Store.open(tmp_path), engine)
    monkeypatch.setattr(restarted, "_request", request)
    assert restarted._resume_deliveries() == {new_id}
    assert sends[-1] == "freshdelivery"


def test_forget_protects_active_approval_records(store, capsys):
    now = datetime.fromisoformat(SAMPLE.created_at)
    approval = store.create_telegram_note_proposal(
        owner="owner", message_id=7, update_key="proposal", now=now, payload="sample note",
        delivery=lambda *_: ["proposal reply"],
    )
    proposal_id = store.by_update_key("proposal").id
    assert cli.main(["forget", str(proposal_id), "--yes"]) == 0
    assert "진행 중인 승인과 연결됨" in capsys.readouterr().out
    assert store.get_approval(approval.id) == approval

    executor = (1, "example-executor")
    decision = store.decide_approval(
        approval.id, actor="owner", channel="telegram", approve=True, now=now,
        update_key="decision", executor=executor, delivery=lambda _: ["decision reply"],
    )
    assert decision.status == "claimed"
    decision_id = store.by_update_key("decision").id
    assert cli.main(["forget", str(decision_id), "--yes"]) == 0
    output = capsys.readouterr().out
    assert "진행 중인 승인과 연결됨" in output
    assert output.endswith("삭제 0건 · 건너뜀 1건\n")
    assert store.get_approval(approval.id) == decision.approval
    assert store.finish_approval(approval.id, ok=True, executor=executor, chunks=["finished reply"])
    assert store.by_update_key("decision").reason == "done"
    assert cli.main(["forget", str(decision_id), str(proposal_id), "--yes"]) == 0
    assert store.get(decision_id) is store.get(proposal_id) is None
    assert store.get_approval(approval.id).status == "done"


def test_forget_preserves_pending_cli_note_proposal(store, monkeypatch, capsys):
    monkeypatch.setattr(brain, "available", lambda: (True, ""))
    now = datetime.fromisoformat(SAMPLE.created_at)
    reply = handle(Request("cli", "/note sample note"), store, FakeEngine(), now)
    assert reply.kind == "note"
    assert cli.main(["forget", str(reply.record_id), "--yes"]) == 0
    assert "진행 중인 승인과 연결됨" in capsys.readouterr().out
    assert store.get(reply.record_id) is not None


def test_forget_before_uses_local_midnight_and_only_shows_counts(store, capsys):
    midnight = datetime(2026, 11, 22).astimezone(UTC)
    before, at, after = [
        store.save(replace(SAMPLE, request="privatequestion", created_at=(midnight + delta).isoformat()))
        for delta in (timedelta(microseconds=-1), timedelta(), timedelta(microseconds=1))
    ]
    for count in (1, 0):
        assert cli.main(["forget", "--before", "2026-11-22", "--yes"]) == 0
        assert capsys.readouterr().out.splitlines() == [f"삭제 대상 {count}건", f"삭제 {count}건 · 건너뜀 0건"]
    assert store.get(before) is None
    assert store.get(at) is not None and store.get(after) is not None


@pytest.mark.parametrize(("tty", "reply", "code"), [(False, None, 1), (True, "n", 0), (True, "y", 0)])
def test_forget_confirmation(store, monkeypatch, capsys, tty, reply, code):
    record_id = store.save(SAMPLE)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: tty)
    prompt = Mock(return_value=reply)
    monkeypatch.setattr("builtins.input", prompt)

    assert cli.main(["forget", str(record_id)]) == code
    output = capsys.readouterr()
    if tty:
        prompt.assert_called_once()
    else:
        prompt.assert_not_called()
        assert "--yes" in output.err
    if reply == "y":
        assert store.get(record_id) is None
        assert "삭제 1건" in output.out
    else:
        assert store.get(record_id) is not None
        assert "삭제 0건" in output.out


@pytest.mark.parametrize("args", [[], ["1", "--before", "2026-11-22"], ["--before", "20261122"],
                                 ["--before", "2026-02-30"], ["9223372036854775808"]])
def test_forget_rejects_invalid_arguments_before_opening_store(monkeypatch, args):
    monkeypatch.setattr(cli, "data_dir", Mock(side_effect=AssertionError("data directory accessed")))
    with pytest.raises(SystemExit) as error:
        cli.main(["forget", *args])
    assert error.value.code == 2


def test_forget_rechecks_record_after_preview(store, monkeypatch, capsys):
    record_id = store.save(SAMPLE)
    targets = store.forget_targets([record_id])
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    def confirm(_prompt):
        assert store.forget(targets) == (1, [], True, ())
        assert store.save(replace(SAMPLE, request="replacement question")) == record_id
        return "y"

    monkeypatch.setattr("builtins.input", confirm)
    assert cli.main(["forget", str(record_id)]) == 0
    assert store.get(record_id).request == "replacement question"
    assert "확인 후 기록이 변경됨" in capsys.readouterr().out


def test_forget_scrubs_database_and_wal_including_inactive_fts(store, tmp_path, monkeypatch):
    path = tmp_path / "javis.db"
    wal = path.with_name("javis.db-wal")
    question = "uniquequestionmarker"
    answer = "uniqueanswermarker"
    chunks = "uniquedeliverymarker"
    store.save(replace(SAMPLE, request="retainedquestion"))
    with closing(sqlite3.connect(path)) as observer:
        record_id = store.save(
            replace(SAMPLE, request=question, items=(Item(answer, ()),)), chunks=[chunks],
        )
        observer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert question.encode() in path.read_bytes()
        monkeypatch.setattr(search_module, "_detect_tokenizer", lambda _: "unicode61")
        Store.open(tmp_path)
        assert question.encode() in wal.read_bytes()

        assert store.forget(store.forget_targets([record_id])) == (1, [], True, ())
        for marker in (question, answer, chunks):
            assert marker.encode() not in path.read_bytes()
        assert wal.stat().st_size == 0
        for table in ("search_trigram", "search_unicode61"):
            assert observer.execute(f"SELECT rowid FROM {table}").fetchall() == [(1,)]
    assert [row.id for row in store.search("retainedquestion")[1]] == [1]


@pytest.mark.parametrize("tokenizer", [None, "unicode61"])
def test_forget_warns_about_inaccessible_fts_tables(store, tmp_path, monkeypatch, capsys, tokenizer):
    record_id = store.save(replace(SAMPLE, request="indexedquestion"), chunks=["queued reply"])
    monkeypatch.setattr(search_module, "_detect_tokenizer", lambda _: "unicode61")
    Store.open(tmp_path)  # Populate both tables before switching to the limited runtime.
    monkeypatch.setattr(search_module, "_detect_tokenizer", lambda _: tokenizer)

    connect = sqlite3.connect

    class LimitedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith(("DELETE FROM search_trigram ", "DELETE FROM search_unicode61 ")):
                if tokenizer is None:
                    raise sqlite3.OperationalError("no such module: fts5")
                if sql.startswith("DELETE FROM search_trigram "):
                    raise sqlite3.OperationalError("no such tokenizer: trigram")
            return super().execute(sql, parameters)

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, **kwargs, factory=LimitedConnection))
    assert cli.main(["forget", str(record_id), "--yes"]) == 1
    output = capsys.readouterr()
    assert output.out.endswith("삭제 1건 · 건너뜀 0건\n")
    assert "정리 못 함" in output.err and "search_trigram" in output.err
    assert ("search_unicode61" in output.err) == (tokenizer is None)
    assert "indexedquestion" not in output.err

    assert store.get(record_id) is None
    with closing(connect(tmp_path / "javis.db")) as db:
        assert db.execute("SELECT count(*) FROM deliveries").fetchone() == (0,)
        assert db.execute("SELECT rowid FROM search_trigram").fetchall() == [(record_id,)]
        remaining_unicode = [(record_id,)] if tokenizer is None else []
        assert db.execute("SELECT rowid FROM search_unicode61").fetchall() == remaining_unicode


def test_forget_reports_blocked_wal_checkpoint_and_can_retry(store, tmp_path, monkeypatch, capsys):
    from worklog import store as store_module

    monkeypatch.setattr(store_module, "_BUSY_TIMEOUT", 0.01)
    record_id = store.save(SAMPLE)
    with closing(sqlite3.connect(tmp_path / "javis.db")) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT id FROM records").fetchall()
        assert cli.main(["forget", str(record_id), "--yes"]) == 1
        printed = capsys.readouterr()
        assert "삭제 1건" in printed.out and "WAL" in printed.err
        assert store.get(record_id) is None
    assert cli.main(["forget", str(record_id), "--yes"]) == 0
    assert "삭제 0건" in capsys.readouterr().out
