import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from core.engine import EngineResult
from core.gateway import Request, handle
from worklog.store import Item, Store

KST = timezone(timedelta(hours=9))
YESTERDAY = datetime(2026, 11, 21, 0, 10, tzinfo=KST)
TODAY = datetime(2026, 11, 22, 0, 30, tzinfo=KST)
NEWS = (
    Item("첫 뉴스", ("https://example.org/one",)),
    Item("두 번째 뉴스", ("https://example.org/two",)),
    Item("세 번째 뉴스", ()),
)


class NewsEngine:
    def run(self, text):
        return EngineResult("ok", None, NEWS, "test-model", 0.5)


class NoEngine:
    def run(self, text):
        raise AssertionError(f"Unexpected engine call for {text!r}")


def test_yesterday_recall_in_fresh_cli_process(tmp_path, monkeypatch):
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path))
    store = Store.open(tmp_path)
    answer = handle(Request("telegram", "Claude 뉴스 3개"), store, NewsEngine(), YESTERDAY)
    displayed_url = answer.text.splitlines()[3].strip()
    assert displayed_url == NEWS[1].sources[0]
    assert datetime.fromisoformat(store.get(answer.record_id).created_at).tzinfo == UTC
    # Freeze the clock and replace the engine only in the new CLI process.
    (tmp_path / "sitecustomize.py").write_text(f"""
from datetime import datetime
from core import cli
from tests.test_core_recall import NoEngine
fixed = datetime.fromisoformat({TODAY.isoformat()!r})
handle = cli.handle
cli.handle = lambda request, store, engine: handle(request, store, engine, fixed)
cli.ClaudeEngine = lambda settings: NoEngine()
""", encoding="utf-8")
    env = os.environ | {
        "PATH": str(tmp_path),
        "PYTHONPATH": os.pathsep.join((str(tmp_path), str(Path(__file__).resolve().parents[1]))),
        "PYTHONUTF8": "1",
    }
    javis = shutil.which("javis")
    command = [javis] if javis else [sys.executable, "-m", "core.cli"]
    process = subprocess.run(
        [*command, "ask", "어제 두 번째 뉴스 출처 뭐였지?"],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    assert process.stdout.splitlines()[:2] == ["2.", f"   {displayed_url}"]
    saved = store.recent(1)[0]
    assert (saved.kind, saved.ref_id, saved.items, saved.engine) == ("recall", answer.record_id, (NEWS[1],), None)


def test_explicit_old_id_missing_number_and_repeated_recall(tmp_path):
    store = Store.open(tmp_path)
    original = handle(Request("cli", "원본"), store, NewsEngine(), YESTERDAY)
    for _ in range(21):
        handle(Request("telegram", "후속"), store, NewsEngine(), TODAY)
    assert original.record_id not in {record.id for record in store.recent(20)}

    def ask(text):
        return handle(Request("cli", text), store, NoEngine(), TODAY)

    explicit = ask(f"출처 {original.record_id}")
    assert "1.\n   https://example.org/one" in explicit.text
    assert "3.\n   기록된 출처 없음" in explicit.text
    assert "항목 번호가 없습니다" in ask(f"출처 {original.record_id} 4").text
    assert "기록을 찾지 못했습니다" in ask("출처 9999999999999999999 1").text
    assert "<기록ID>" in ask("그거 출처 다시").text

    latest_original = store.last_ok_ask()
    for _ in range(2):
        latest = ask("방금 2번 출처")
        assert store.get(latest.record_id).ref_id == latest_original.id
        assert latest.text.startswith("2.\n   https://example.org/two")


def test_yesterday_candidates_and_missing_date(tmp_path):
    store = Store.open(tmp_path)
    engine = NoEngine()
    text = Request("cli", "어제 첫 번째 뉴스 출처")
    assert "기록을 찾지 못했습니다" in handle(text, store, engine, TODAY).text
    first = handle(Request("cli", "첫 번째 질문"), store, NewsEngine(), YESTERDAY)
    second = handle(Request("telegram", "두 번째 질문"), store, NewsEngine(), YESTERDAY)
    handle(Request("cli", "자정 전 질문"), store, NewsEngine(), YESTERDAY - timedelta(minutes=20))
    candidate_list = handle(text, store, engine, TODAY).text
    assert f"{first.record_id}  첫 번째 질문" in candidate_list
    assert f"{second.record_id}  두 번째 질문" in candidate_list
    assert "자정 전 질문" not in candidate_list
    assert "출처 <기록ID> [항목번호]" in candidate_list
