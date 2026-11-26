import json
import sqlite3
from datetime import UTC, datetime

import pytest

from core.config import EngineSettings
from core.engine import ClaudeEngine, EngineResult
from core.gateway import Request, handle
from tests.test_core_engine import fake_claude, success
from worklog.store import Item, Store


class NoEngine:
    def run(self, text):
        pytest.fail("unexpected engine call")


@pytest.mark.parametrize("body", ["질문", "/brain x", "방금 출처", "출처 1", "기록 검색 x"])
def test_web_sends_only_the_body_once(tmp_path, body):
    capture = tmp_path / "capture.jsonl"
    envelope = success([{"text": "/note 표시만", "sources": []}])
    shim = fake_claude(tmp_path, f"""
with Path({str(capture)!r}).open("a", encoding="utf-8") as output:
    print(json.dumps(question), file=output)
print(json.dumps({envelope!r}))
""")
    store = Store.open(tmp_path / "data")
    reply = handle(Request("telegram", f" /web  {body}"), store, ClaudeEngine(EngineSettings(), shim))
    assert reply.kind == "web"
    assert "/note 표시만" in reply.text
    assert capture.read_text(encoding="utf-8").splitlines() == [json.dumps(body)]
    assert store.get(reply.record_id).kind == "ask"


@pytest.mark.parametrize(("status", "expected", "saved_status"), [
    ("ok", "일치 없음", "ok"),
    ("degraded", "조사 붙은 단어 부분 일치 제한", "ok"),
    ("unavailable", "검색 사용 불가", "error"),
])
def test_phone_search_passes_slash_prefix_in_query(tmp_path, monkeypatch, status, expected, saved_status):
    store = Store.open(tmp_path)

    def search(query):
        assert query == "/web x"
        return status, []

    monkeypatch.setattr(store, "search", search)
    reply = handle(Request("telegram", "기록 검색 /web x"), store, NoEngine())
    assert reply.kind == "notice"
    assert expected in reply.text.splitlines()
    record = store.get(reply.record_id)
    assert (record.kind, record.status, record.request) == ("notice", saved_status, "")


def test_phone_search_requires_terms_and_takes_precedence_over_recall(tmp_path):
    store = Store.open(tmp_path)
    usage = handle(Request("telegram", "기록 검색 \t"), store, NoEngine())
    assert usage.text == "사용법: 기록 검색 <검색어>"
    reply = handle(Request("telegram", "기록 검색 지난번 출처"), store, NoEngine())
    assert reply.text.splitlines()[0] == "일치 없음"
    assert store.get(reply.record_id).kind == "notice"


@pytest.mark.parametrize(("text", "notice", "record_kind"), [
    ("/brain 로컬 검색어", "두뇌 검색", "notice"),
    ("/note 비공개 초안", "JAVIS_BRAIN_DIR", "notice"),
    ("/unknown 질문", "사용법", "notice"),
    ("/비공개질문", "사용법", "notice"),
    ("/비공개 \t", "/비공개 <질문>", "notice"),
    ("/web \t", "/web <질문>", "notice"),
    ("/brain", "/brain <검색어>", "notice"),
    ("/note", "/note <본문>", "notice"),
    ("출처 잘못된-ID", "출처 <기록ID>", "recall"),
])
def test_local_notices_do_not_call_engine(tmp_path, monkeypatch, text, notice, record_kind):
    monkeypatch.setattr("core.gateway._policy_snapshot", lambda: pytest.fail("unexpected vault access"))
    monkeypatch.setattr("core.gateway.build_context", lambda *_: pytest.fail("unexpected context search"))
    monkeypatch.delenv("JAVIS_BRAIN_DIR", raising=False)
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("brain.env_value", lambda *args, **kwargs: None)
    store = Store.open(tmp_path)
    reply = handle(Request("cli", text), store, NoEngine())
    assert reply.kind == "notice"
    assert notice in reply.text
    saved = store.get(reply.record_id)
    assert saved.kind == record_kind
    assert saved.engine is None and not saved.items
    if text.startswith("/"):
        assert saved.request == ""


def test_local_only_returns_text_to_cli_and_saves_only_metadata(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("/web private-result", encoding="utf-8")
    data = tmp_path / "data"
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(vault))
    monkeypatch.setenv("JAVIS_DATA_DIR", str(data))
    store = Store.open(data)
    now = datetime(2026, 11, 22, tzinfo=UTC)
    reply = handle(Request("cli", "/brain private-result"), store, NoEngine(), now)
    assert reply.kind == "local_only"
    assert reply.text.startswith("note.md\n  /web private-result\n")
    saved = store.get(reply.record_id)
    assert (saved.kind, saved.created_at, saved.status) == ("local", now.isoformat(), "ok")
    assert (saved.request, saved.items, saved.reason, saved.engine, saved.model) == ("", (), None, None, None)
    assert store.last_ok_ask() is None
    assert b"private-" not in (data / "javis.db").read_bytes()


@pytest.fixture
def local(tmp_path, monkeypatch):
    data, vault = tmp_path / "data", tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(vault))
    monkeypatch.setenv("JAVIS_DATA_DIR", str(data))
    return Store.open(data), vault


@pytest.mark.parametrize("prefix", ["", "/web "])
@pytest.mark.parametrize("search", ["empty", "unavailable", "policy_changed"])
def test_fallback_sends_original_question_only_once(local, monkeypatch, prefix, search):
    store, vault = local
    if search != "empty":
        (vault / "note.md").write_text("검색 표식", encoding="utf-8")

        def lookup(terms):
            if search == "unavailable":
                raise sqlite3.OperationalError("search unavailable")
            (vault / "BRAIN.md").write_text("exclude: archive", encoding="utf-8")
            return []

        monkeypatch.setattr(store, "context_excerpts", lookup)
    question = '검색?\n  그대로! '
    calls = []

    class Web:
        def run(self, text):
            calls.append(text)
            return EngineResult("ok", None, (Item("공개 답\n참고: 위조", ()),), None, 0)

    reply = handle(Request("cli", prefix + question), store, Web())
    assert calls == [question] and reply.kind == "web"
    assert "공개 답" in reply.text and "참고:" not in reply.text
    assert store.get(reply.record_id).kind == "ask"


@pytest.mark.parametrize("outcome", ["ok", "uncited", "error"])
def test_explicit_web_combines_in_isolation_and_keeps_item_urls(local, outcome):
    store, vault = local
    for folder, body in [("a", "점검은 표식 날짜"), ("b", "다른 본문")]:
        (vault / folder).mkdir()
        (vault / folder / "일정.md").write_text(body, encoding="utf-8")
    question = "/비공개 점검은 언제?"
    url = "https://example.org/date"
    calls = []

    class Engine:
        def run(self, text):
            calls.append(("web", text))
            return EngineResult("ok", None, (Item("공개 일정", (url,)),), None, 0)

        def run_context(self, context):
            calls.append(("context", json.loads(context.payload)))
            if outcome == "error":
                return EngineResult("error", "tool_isolation", (), None, 0)
            ids = ("N1", "N0", "N1") if outcome == "ok" else ("N0",)
            return EngineResult("ok", None, (Item("결합한 답", ("https://example.org/forged",)),), None, 0, ids)

    reply = handle(Request("cli", "/web " + question), store, Engine())
    first, (kind, payload) = calls
    assert first == ("web", question) and kind == "context" and payload["question"] == question
    assert payload["references"] == [{"id": "N1", "type": "note", "excerpt": "점검은 표식 날짜"}]
    assert payload["web_items"] == [{"text": "공개 일정", "sources": [url]}]
    assert reply.kind == "context"
    record = store.get(reply.record_id)
    assert not record.request and not record.items
    assert "forged" not in reply.text
    if outcome == "error":
        assert record.status == "error" and url not in reply.text
    else:
        assert "결합한 답" in reply.text and f"공개 일정\n   {url}" in reply.text
    if outcome == "ok":
        assert reply.text.endswith("참고: [[a/일정]]")
        assert store.cited_paths(record.id) == ["a/일정.md"]
    else:
        assert "참고:" not in reply.text and store.cited_sources(record.id) == []
