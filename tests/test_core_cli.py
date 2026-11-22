import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import approval, cli
from core.approval import ApprovalService
from worklog.store import Store

from tests.test_core_engine import fake_claude, success


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
        pytest.fail("preview opened the worklog or engine")

    monkeypatch.setattr(cli.Store, "open", forbidden)
    monkeypatch.setattr(cli, "ClaudeEngine", forbidden)
    assert cli.main(["brain", "import", str(source), "--dest", "imports"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "복사 예정 11건 · 충돌 1건" in output.out
    assert "합계 12건 · 140바이트" in output.out
    assert "nested/old.md → imports/nested/old.md (충돌로 건너뜀)" in output.out
    assert "외 2건" in output.out
    assert "승인 기능은 아직 없음(작업 10)" in output.out
    assert str(source) not in output.out
    assert str(vault) not in output.out
    assert "private note" not in output.out
    assert not data.exists()


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
    assert "일반 질문과 /web은 외부(Claude 구독·웹 검색)로 전송됩니다." in capsys.readouterr().out


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
    manifest = {"dest": "archive", "files": ["one.md", "two.md"]}
    imported = service.propose("brain.import", manifest, actor, "cli", now)
    expired = service.propose("note.create", "expired", actor, "cli", now - timedelta(days=2))
    phone = service.propose("note.create", "phone draft", "chat", "telegram", now + timedelta(seconds=1))
    service.propose("brain.import", manifest, actor, "telegram")
    service.propose("note.create", "other owner", "different-user", "cli")

    assert cli.main(["pending"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"{row.id}\t{row.kind}\t{summary}\t{datetime.fromisoformat(row.expires_at).astimezone():%Y-%m-%d %H:%M}"
        for row, summary in [
            (note, f"{cli.note_path(note)}\tDraft preview"),
            (imported, "archive (2개 파일)"),
            (phone, f"{cli.note_path(phone)}\tphone draft"),
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


def test_approve_without_executor_preserves_proposal(approvals, capsys):
    store, service, actor = approvals
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
