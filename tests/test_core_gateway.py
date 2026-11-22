import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from core import gateway
from core.config import EngineSettings
from core.engine import ClaudeEngine
from core.gateway import Request, handle
from teams import LocalOnly, registry
from tests.test_core_engine import fake_claude, success
from worklog.store import Store


class NoEngine:
    def run(self, text):
        pytest.fail("unexpected engine call")


@pytest.mark.parametrize("body", ["질문", "/brain x", "방금 출처", "출처 1"])
def test_web_sends_only_the_body_once(tmp_path, body):
    capture = tmp_path / "capture.jsonl"
    envelope = success([{"text": "/note 표시만", "sources": []}])
    shim = fake_claude(tmp_path, f"""
with Path({str(capture)!r}).open("a", encoding="utf-8") as output:
    print(json.dumps(question), file=output)
print(json.dumps({envelope!r}))
""")
    store = Store.open(tmp_path / "data")
    reply = handle(Request("cli", f" /web  {body}"), store, ClaudeEngine(EngineSettings(), shim))
    assert reply.kind == "web"
    assert "/note 표시만" in reply.text
    assert capture.read_text(encoding="utf-8").splitlines() == [json.dumps(body)]
    assert store.get(reply.record_id).kind == "ask"


@pytest.mark.parametrize(("text", "notice", "record_kind"), [
    ("/brain 로컬 검색어", "두뇌 검색", "notice"),
    ("/note 비공개 초안", "메모 기능 준비 중", "notice"),
    ("/unknown 질문", "사용법", "notice"),
    ("/web \t", "/web <질문>", "notice"),
    ("/brain", "/brain <검색어>", "notice"),
    ("/note", "/note <본문>", "notice"),
    ("출처 잘못된-ID", "출처 <기록ID>", "recall"),
])
def test_local_notices_do_not_call_engine(tmp_path, text, notice, record_kind):
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
    def teams(engine):
        entries = registry(engine)
        entries["/brain"] = replace(
            entries["/brain"], unavailable_reason="", handler=lambda *_: LocalOnly("/web private-result"),
        )
        return entries

    monkeypatch.setattr(gateway, "registry", teams)
    store = Store.open(tmp_path)
    now = datetime(2026, 11, 22, tzinfo=UTC)
    reply = handle(Request("cli", "/brain private-query"), store, NoEngine(), now)
    assert reply.kind == "local_only"
    assert reply.text.startswith("/web private-result\n")
    saved = store.get(reply.record_id)
    assert (saved.kind, saved.created_at, saved.status) == ("local", now.isoformat(), "ok")
    assert (saved.request, saved.items, saved.reason, saved.engine, saved.model) == ("", (), None, None, None)
    assert store.last_ok_ask() is None
    assert b"private-" not in (tmp_path / "javis.db").read_bytes()
