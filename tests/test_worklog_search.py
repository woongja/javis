import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime

import pytest

from core.gateway import Request, handle
from tests.test_core_recall import NoEngine
from tests.test_worklog_store import SAMPLE
from worklog import Item, SearchResult, Store
from worklog import search as search_module
from worklog.store import _MIGRATIONS, _SCHEMA_VERSION


WEB_RECORD = replace(SAMPLE, request="회의 요약", items=(Item("지난 회의에서 결정한 내용", ()),))


def test_migrates_v4_and_backfills_web_history(tmp_path):
    path = tmp_path / "javis.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        for migration in _MIGRATIONS[:4]:
            for statement in migration:
                db.execute(statement)
        db.execute("INSERT INTO schema_version VALUES (4)")

    old = Store(path)
    original = replace(WEB_RECORD, request="요약", update_key="migration-key")
    record_id = old.save(original, chunks=["legacy delivery", "remaining"])
    old.mark_sent(record_id, 1, offset=8)
    old.save(replace(WEB_RECORD, kind="recall", request="oldrecallmarker"))

    store = Store.open(tmp_path)
    assert store.search("회의") == ("ok", [SearchResult(record_id, "2026-11-21", "요약")])
    assert store.search("oldrecallmarker") == ("ok", [])
    assert store.by_update_key("migration-key") == replace(original, id=record_id)
    assert store.pending_deliveries() == [(record_id, ["legacy delivery", "remaining"], 1)]
    assert store.get_offset()[0] == 8
    with store._connect() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == _SCHEMA_VERSION
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_new_records_are_indexed_atomically(tmp_path):
    store = Store.open(tmp_path)
    with pytest.raises(TypeError, match="JSON serializable"):
        store.save(replace(WEB_RECORD, request="rollbackmarker"), chunks=[object()])
    assert store.recent(1) == []
    with store._connect() as db:
        assert db.execute("SELECT rowid FROM search_trigram").fetchall() == []

    original = replace(WEB_RECORD, request="새  질문\n" + "긴" * 100, update_key="once")
    record_id = store.save(original, chunks=["successful delivery"])
    preview = "새 질문 " + "긴" * 74 + "…"
    assert store.search("회의") == ("ok", [SearchResult(record_id, "2026-11-21", preview)])
    assert store.save(replace(original, request="duplicatemarker")) == record_id
    assert store.search("duplicatemarker") == ("ok", [])


def test_combines_terms_and_orders_and_caps_results(tmp_path):
    store = Store.open(tmp_path)
    store.save(replace(WEB_RECORD, request="일정 요청"))
    store.save(replace(WEB_RECORD, request="가 다음 일정", items=(Item("회의에서 다른 내용", ()),)))
    newest = store.save(replace(WEB_RECORD, request="가 일정 요청", created_at="2026-11-22T00:00:00Z"))
    tied = [store.save(replace(WEB_RECORD, request="가 일정 요청")) for _ in range(21)]
    expected = [newest, *reversed(tied)]

    assert store.search(" \t\n") == ("usage", [])
    assert store.search("unmatchedkeyword") == ("ok", [])
    for limit in (1, 20, 999, 0, -1):
        status, hits = store.search("가  일정\t회의 결정한", limit)
        assert status == "ok"
        assert [hit.id for hit in hits] == expected[:max(0, min(limit, 20))]
    assert store.search("가 일정 회의 결정한")[1] == store.search("가 일정 회의 결정한", 20)[1]


@pytest.mark.parametrize("term", ['a"bc', '"abc"', "abc*", "OR", "NEAR", "-abc", "(abc)", "*", "-", '"'])
def test_fts_syntax_is_bound_as_literal_text(tmp_path, term):
    store = Store.open(tmp_path)
    record_id = store.save(replace(SAMPLE, request=term, items=()))
    store.save(replace(SAMPLE, request="abc ordinary words", items=()))
    assert [hit.id for hit in store.search(term)[1]] == [record_id]


def test_indexes_only_successful_web_questions_and_answer_text(tmp_path):
    store = Store.open(tmp_path)
    hidden = []
    for kind in ("recall", "local", "notice", "approval"):
        request, body = f"{kind}requestmarker", f"{kind}bodymarker"
        store.save(replace(SAMPLE, kind=kind, request=request, items=(Item(body, ()),)))
        hidden.extend((request, body))
    for status in ("error", "timeout", "quota", "denied"):
        request, body = f"{status}requestmarker", f"{status}bodymarker"
        store.save(replace(SAMPLE, status=status, request=request, items=(Item(body, ()),)))
        hidden.extend((request, body))

    record_id = store.save(
        replace(SAMPLE, request="publicrequest", items=(Item("publicanswer", ("https://example.org/sourcemarker",)),)),
        chunks=["chunkmarker"],
    )
    store.create_telegram_note_proposal(
        owner="proposalownermarker", message_id=87654321, update_key="proposal-test",
        now=datetime.fromisoformat(SAMPLE.created_at), payload="approvalpayloadmarker",
        delivery=lambda *_: ["proposalreplymarker"],
    )
    hidden.extend(("sourcemarker", "chunkmarker", "proposalownermarker", "87654321",
                   "approvalpayloadmarker", "proposalreplymarker"))

    assert [hit.id for hit in store.search("publicrequest publicanswer")[1]] == [record_id]
    for term in hidden:
        assert store.search(term) == ("ok", []), term


def test_switches_tokenizers_and_rebuilds(tmp_path, monkeypatch):
    store = Store.open(tmp_path)
    original = store.save(replace(WEB_RECORD, request="회의 회의에서"))
    assert store.search("의에서")[0] == "ok"
    assert [hit.id for hit in store.search("의에서")[1]] == [original]

    with monkeypatch.context() as patch:
        patch.setattr(search_module, "_detect_tokenizer", lambda db: "unicode61")
        degraded = Store.open(tmp_path)
        assert degraded.search("의에서") == ("degraded", [])
        assert [hit.id for hit in degraded.search("회의에서 회의")[1]] == [original]
        added = degraded.save(replace(WEB_RECORD, request="회의에서 후속"))
        assert [hit.id for hit in degraded.search("회의에서")[1]] == [added, original]

    restored = Store.open(tmp_path)
    assert restored.search("의에서")[0] == "ok"
    assert [hit.id for hit in restored.search("의에서")[1]] == [added, original]


@pytest.mark.parametrize("already_indexed", [False, True])
def test_unavailable_preserves_saves_and_recall_then_backfills(tmp_path, monkeypatch, already_indexed):
    if already_indexed:
        Store.open(tmp_path).save(WEB_RECORD)
    with monkeypatch.context() as patch:
        patch.setattr(search_module, "_detect_tokenizer", lambda db: None)
        store = Store.open(tmp_path)
        record_id = store.save(SAMPLE, chunks=["reply"])
        assert store.search("release") == ("unavailable", [])
        assert store.search(" ") == ("usage", [])
        assert store.get(record_id) == store.last_ok_ask() == replace(SAMPLE, id=record_id)
        assert store.pending_deliveries() == [(record_id, ["reply"], 0)]
        answer = handle(Request("cli", f"출처 {record_id} 1"), store, NoEngine())
        assert SAMPLE.items[0].sources[0] in answer.text
        assert store.get(answer.record_id).ref_id == record_id

    restored = Store.open(tmp_path)
    assert restored.search("release") == ("ok", [SearchResult(record_id, "2026-11-21", SAMPLE.request)])
