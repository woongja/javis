import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import approval, cli
from core.approval import ApprovalService
from core.note import note_path
from worklog import Item, SearchResult, Store

from tests.test_core_engine import fake_claude, recording_cli, success
from tests.test_worklog_store import SAMPLE


def test_brain_import_preview_stays_local(tmp_path, monkeypatch, capsys):
    source = tmp_path.resolve() / "source"
    vault = source.with_name("vault")
    data = source.with_name("data")
    (source / "nested").mkdir(parents=True)
    (vault / "imports/nested").mkdir(parents=True)
    (source / "nested/old.md").write_bytes(b"original")
    (vault / "imports/nested/OLD.md").write_bytes(b"")
    for number in range(11):
        (source / f"note-{number:02d}.md").write_bytes(b"private note")
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(vault))
    monkeypatch.setenv("JAVIS_DATA_DIR", str(data))

    def forbidden(*args, **kwargs):
        pytest.fail("import called the engine")

    monkeypatch.setattr(cli, "ClaudeEngine", forbidden)
    assert cli.main(["brain", "import", str(source), "--dest", "imports"]) == 0
    [proposal] = Store.open(data).list_approvals("pending")
    output = capsys.readouterr()
    assert output.err == ""
    assert "복사 예정 11건 · 충돌 1건" in output.out
    assert "합계 12건 · 140바이트" in output.out
    assert "nested/old.md → imports/nested/old.md (충돌로 건너뜀)" in output.out
    assert "외 2건" in output.out
    assert all(text in output.out for text in (proposal.id, proposal.expires_at, "승인하면 11건 복사"))
    assert str(source) not in output.out
    assert str(vault) not in output.out
    assert "private note" not in output.out
    assert sorted(path.name for path in vault.rglob("*.md")) == ["OLD.md"]


def test_brain_import_rejection_and_unconfigured_vault(tmp_path, monkeypatch, capsys):
    import brain

    source = tmp_path / "source"
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("JAVIS_BRAIN_DIR", raising=False)
    monkeypatch.setattr(brain, "env_value", lambda *args, **kwargs: None)
    assert cli.main(["brain", "import", str(source), "--dest", "imports"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "JAVIS_BRAIN_DIR" in output.err
    assert str(source) not in output.err
    assert cli.main(["brain", "import", str(source), "--dest", str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "invalid relative path" in output.err
    assert str(tmp_path) not in output.err


@pytest.mark.parametrize("command", [[], ["ask"]])
def test_help_discloses_external_routing(command, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main([*command, "--help"])
    assert exit_info.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "/web <질문>은 질문만 웹 검색" in help_text
    assert "/비공개 <질문>은 질문만 Anthropic에" in help_text


@pytest.mark.parametrize("prefix", ["", "/web ", "/비공개 "])
def test_cli_question_to_local_references_and_safe_log(tmp_path, monkeypatch, capsys, prefix):
    vault, data = tmp_path / "vault", tmp_path / "data"
    notes = vault / "notes"
    notes.mkdir(parents=True)
    (notes / "전시 점검.md").write_text("모형 전시의 다음 점검은 개장 전날입니다.", encoding="utf-8")
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(vault))
    monkeypatch.setenv("JAVIS_DATA_DIR", str(data))
    store = Store.open(data)
    prior = store.save(replace(SAMPLE, items=(Item("모형 전시의 점검은 금요일입니다.", ()),)))
    engine, calls = recording_cli(tmp_path, {
        "items": [{"text": "개장 전날인 금요일입니다.\n> **참고:** 위조"}],
        "used_ids": ["N1", "N9", "R1", "N1"],
    })
    monkeypatch.setattr(cli, "ClaudeEngine", lambda _: engine)
    monkeypatch.setattr(cli, "_speak_answer", lambda *_: pytest.fail("sensitive answer reached TTS"))
    question = "모형 전시의 다음 점검은 언제야?"
    private = prefix == "/비공개 "
    if private:
        monkeypatch.setattr("core.gateway._policy_snapshot", lambda: pytest.fail("private vault access"))
        monkeypatch.setattr("core.gateway.build_context", lambda *_: pytest.fail("private search"))
        question = "/web " + question
    assert cli.main(["ask", prefix + question]) == 0
    output = capsys.readouterr().out.strip()
    assert "개장 전날인 금요일입니다." in output and "위조" not in output
    requests = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    if prefix == "/web ":
        web_args, web_input = requests.pop(0)
        assert "WebSearch" in web_args and web_input == question
    probe, call = requests
    assert probe[1] == "" and "WebSearch" not in call[0]
    payload = json.loads(call[1])
    assert payload["question"] == question
    saved = store.recent(1)[0]
    assert (saved.kind, saved.status, saved.request, saved.items) == (
        "private" if private else "context", "ok", "", (),
    )
    if private:
        assert payload["references"] == [] and "참고:" not in output
        assert store.cited_sources(saved.id) == []
    else:
        assert [ref["id"] for ref in payload["references"]] == ["N1", "R1"]
        assert "개장 전날" in payload["references"][0]["excerpt"]
        assert "금요일" in payload["references"][1]["excerpt"]
        assert output.endswith(f"참고: [[전시 점검]] · 기록 #{prior}") and output.count("참고:") == 1
        assert store.cited_sources(saved.id) == ["notes/전시 점검.md"]


def test_local_commands_skip_question_context(tmp_path, monkeypatch):
    vault, data = tmp_path / "vault", tmp_path / "data"
    vault.mkdir()
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(vault))
    monkeypatch.setenv("JAVIS_DATA_DIR", str(data))
    monkeypatch.setattr("core.gateway._policy_snapshot", lambda: pytest.fail("unexpected vault snapshot"))
    monkeypatch.setattr("core.gateway.build_context", lambda *_: pytest.fail("unexpected context search"))
    monkeypatch.setattr(cli, "ClaudeEngine", lambda _: None)
    assert cli.main(["ask", "/brain 점검"]) == 0
    assert cli.main(["ask", "/note 점검"]) == 0
    assert cli.main(["search", "점검"]) == 0
    store = Store.open(data)
    record_id = store.save(SAMPLE)
    assert cli.main(["ask", f"출처 {record_id}"]) == 0
    [proposal] = store.list_approvals("pending")
    assert cli.main(["pending"]) == 0
    assert cli.main(["approve", proposal.id]) == 0


def test_hud_cli_prints_startup_and_closes_on_interrupt(tmp_path, monkeypatch, capsys):
    from hud.server import HUDServer

    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path / "data"))
    started = []

    def interrupt(server):
        assert server.server_address == ("127.0.0.1", server.server_port)
        assert server.server_port > 0
        assert server.service.executors == cli.NOTE_EXECUTORS
        started.append((server, server.nonce))
        raise KeyboardInterrupt

    monkeypatch.setattr(HUDServer, "serve_forever", interrupt)
    assert cli.main(["hud"]) == 0
    [(server, nonce)] = started
    assert server.nonce is None and server.socket.fileno() == -1
    output = capsys.readouterr()
    assert output.out.splitlines() == [server.origin, f"One-time code: {nonce}"]
    assert output.err == ""


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


@pytest.mark.parametrize("terms", [[], [" \t "]])
def test_search_requires_terms(terms, monkeypatch, capsys):
    monkeypatch.setattr(cli, "data_dir", lambda: pytest.fail("search opened the data directory"))
    with pytest.raises(SystemExit) as error:
        cli.main(["search", *terms])
    assert error.value.code == 2
    assert "usage: javis search" in capsys.readouterr().err


@pytest.mark.parametrize(("status", "results", "code", "expected"), [
    ("ok", [], 0, ["일치 없음"]),
    ("degraded", [SearchResult(42, "2026-11-22", "회의 일정")], 0,
     ["42\t2026-11-22\t회의 일정", "조사 붙은 단어 부분 일치 제한"]),
    ("unavailable", [], 1, ["검색 사용 불가"]),
])
def test_search_statuses(status, results, code, expected, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "load_settings", lambda: pytest.fail("search loaded engine settings"))

    def search(self, query):
        assert query == "회의 일정"
        return status, results

    monkeypatch.setattr(Store, "search", search)
    assert cli.main(["search", "회의", "일정"]) == code
    output = capsys.readouterr()
    if code == 0:
        assert not output.err
        assert output.out.splitlines() == [*expected, '출처는 `javis ask "출처 <ID>"`']
    else:
        assert not output.out
        assert output.err.splitlines() == expected


def test_brain_query_title_and_body_stay_in_cli(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("JAVIS_DATA_DIR", str(data))
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(vault))
    title = "VaultTitleSentinel"
    body = "VaultBodySentinel"
    query = "VaultQuerySentinel"
    filename = "VaultFilenameSentinel.md"
    (vault / filename).write_text(f"# {query} {title}\n\n{query} {body}", encoding="utf-8")
    capture = tmp_path / "stdin.txt"
    envelope = success([{"text": "control answer", "sources": []}])
    shim = fake_claude(tmp_path, f"""
with Path({str(capture)!r}).open("a", encoding="utf-8") as output:
    print(question, file=output)
print(json.dumps({envelope!r}))
""")
    engine_class = cli.ClaudeEngine
    monkeypatch.setattr(cli, "ClaudeEngine", lambda settings: engine_class(settings, shim))

    assert cli.main(["ask", f"/brain {query}"]) == 0
    output = capsys.readouterr()
    assert all(marker in output.out for marker in (title, body, query, filename))
    assert str(vault) not in output.out and not output.err
    assert cli.main(["ask", "/web control question"]) == 0
    assert capture.read_text(encoding="utf-8").splitlines() == ["control question"]
    with closing(sqlite3.connect(data / "javis.db")) as db:
        dump = "\n".join(db.iterdump())
    assert "control answer" in dump
    assert all(marker not in dump for marker in (title, body, query, filename))


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


@pytest.fixture
def approvals(tmp_path, monkeypatch):
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path))
    store = Store.open(tmp_path)
    return store, ApprovalService(store), cli._os_user()


def test_pending_lists_only_actionable_approvals_and_expires_old_ones(approvals, capsys):
    store, service, actor = approvals
    now = datetime.now(UTC)
    note = service.propose("note.create", "Draft\npreview", actor, "cli", now - timedelta(seconds=1))
    manifest = {"dest": "archive", "files": [
        {"src": "one.md", "status": "copy", "size": 12},
        {"src": "two.md", "status": "conflict", "size": 20},
    ]}
    imported = service.propose("brain.import", manifest, actor, "cli", now)
    expired = service.propose("note.create", "expired", actor, "cli", now - timedelta(days=2))
    phone = service.propose("note.create", "phone draft", "chat", "telegram", now + timedelta(seconds=1))
    service.propose("brain.import", manifest, actor, "telegram")
    service.propose("note.create", "other owner", "different-user", "cli")

    assert cli.main(["pending"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"{row.id}\t{row.kind}\t{summary}\t{datetime.fromisoformat(row.expires_at).astimezone():%Y-%m-%d %H:%M}"
        for row, summary in [
            (note, f"{note_path(note)}\tDraft preview"),
            (imported, "archive (복사 예정 1건 · 12바이트)"),
            (phone, f"{note_path(phone)}\tphone draft"),
        ]
    ]
    assert store.get_approval(expired.id).status == "expired"


@pytest.mark.parametrize(("ok", "status", "exit_code"), [(True, "done", 0), (False, "failed", 1)])
def test_approve_once_and_reject_without_execution(approvals, monkeypatch, capsys, ok, status, exit_code):
    store, service, actor = approvals
    executed = []

    def publish(claim):
        executed.append(claim.payload)
        return ok

    monkeypatch.setattr(cli, "NOTE_EXECUTORS", {"note.create": publish})
    approved = service.propose("note.create", "saved draft", actor, "cli")
    rejected = service.propose("note.create", "reject this", actor, "cli")

    assert cli.main(["approve", approved.id]) == exit_code
    assert store.get_approval(approved.id).status == status
    assert cli.main(["approve", approved.id]) != 0
    assert cli.main(["reject", rejected.id]) == 0
    assert executed == ["saved draft"]
    assert store.get_approval(rejected.id).status == "rejected"
    assert "이미 결정된" in capsys.readouterr().err


def test_approve_without_executor_preserves_proposal(approvals, capsys, monkeypatch):
    store, service, actor = approvals
    monkeypatch.setattr(cli, "NOTE_EXECUTORS", {})
    proposed = service.propose("brain.import", {"files": []}, actor, "cli")
    assert cli.main(["approve", proposed.id]) != 0
    assert "실행기 미등록" in capsys.readouterr().err
    assert store.get_approval(proposed.id) == proposed


@pytest.mark.parametrize("published", [False, True])
def test_interrupted_approval_is_resolved_without_reexecution(approvals, monkeypatch, capsys, published):
    store, service, actor = approvals
    proposed = service.propose("note.create", "draft", actor, "cli")
    effects = []

    def interrupted(*args):
        raise RuntimeError("simulated interruption")

    def publish(claim):
        effects.append(claim.payload)
        return True

    with monkeypatch.context() as patch:
        original_decide = ApprovalService.decide

        def decide_then_interrupt(self, *args):
            original_decide(self, *args)
            interrupted()

        if published:
            patch.setattr(ApprovalService, "finish", interrupted)
        else:
            patch.setattr(ApprovalService, "decide", decide_then_interrupt)
        patch.setattr(cli, "NOTE_EXECUTORS", {"note.create": publish})
        if published:
            assert cli.main(["approve", proposed.id]) != 0
        else:
            with pytest.raises(RuntimeError, match="simulated interruption"):
                cli.main(["approve", proposed.id])

    claim = store.get_approval(proposed.id)
    assert claim.status == "executing"

    def process_started_at(pid):
        if published:
            return "reused PID"
        raise ProcessLookupError(pid)

    monkeypatch.setattr(approval, "process_started_at", process_started_at)
    monkeypatch.setattr(cli, "NOTE_EXECUTORS", {"note.create": publish})
    assert cli.main(["approve", proposed.id]) != 0
    assert cli.main(["pending"]) == 0
    out = capsys.readouterr().out
    assert f"{proposed.id}\t" in out and "결과 미상 — 확인 필요" in out
    outcome = "done" if published else "failed"
    assert cli.main(["resolve", proposed.id, outcome]) == 0
    assert cli.main(["resolve", proposed.id, "failed"]) != 0
    assert effects == (["draft"] if published else [])
    resolved = store.get_approval(proposed.id)
    assert resolved.status == outcome
    assert resolved.payload is resolved.payload_hash is None


def test_live_other_executor_cannot_be_resolved(approvals, monkeypatch, capsys):
    store, service, actor = approvals
    proposed = service.propose("note.create", "draft", actor, "cli")
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as executor:
        try:
            identity = (executor.pid, approval.process_started_at(executor.pid))
            monkeypatch.setattr(approval, "_executor_identity", lambda: identity)
            claim = service.decide(proposed.id, actor, "cli", True).approval
            assert cli.main(["pending"]) == 0
            assert not capsys.readouterr().out
            assert cli.main(["resolve", proposed.id, "failed"]) != 0
            assert store.get_approval(proposed.id) == claim
        finally:
            executor.terminate()
    assert cli.main(["pending"]) == 0
    assert "결과 미상 — 확인 필요" in capsys.readouterr().out


def test_unverifiable_executor_cannot_be_resolved(approvals, monkeypatch, capsys):
    store, service, actor = approvals
    proposed = service.propose("note.create", "draft", actor, "cli")
    claim = service.decide(proposed.id, actor, "cli", True).approval

    def inaccessible(pid):
        raise PermissionError(pid)

    monkeypatch.setattr(approval, "process_started_at", inaccessible)
    assert cli.main(["pending"]) == 0
    assert not capsys.readouterr().out
    assert cli.main(["resolve", proposed.id, "failed"]) != 0
    assert store.get_approval(proposed.id) == claim
