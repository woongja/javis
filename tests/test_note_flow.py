import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from brain import append as adapter
from brain.curate import NOTES, serialize_note
from brain.publish import pin_root
from core import cli
from core.curate import conversation_prompt
from core.engine import ClaudeEngine
from core.gateway import Request, handle
from tests.test_core_engine import fake_claude, recording_cli, success
from tests.test_curate_proposal import EXAMPLE, EXISTING, NOTE, append, create, write
from tests.test_telegram import CHAT, api, bot, owner_update
from worklog import Store


MARKER = "PrivateNoteBodySentinel"
BODY = f"  # 메모\r\n\r\n{MARKER}\n/brain 그대로  \n"


@pytest.fixture
def configured(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    data = tmp_path / "data"
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(vault))
    monkeypatch.setenv("JAVIS_DATA_DIR", str(data))
    monkeypatch.setattr("brain.env_value", lambda *_args, **_kwargs: None)
    capture = tmp_path / "stdin.jsonl"
    envelope = success([{"text": "control answer", "sources": []}])
    shim = fake_claude(tmp_path, f"""
with Path({str(capture)!r}).open("a", encoding="utf-8") as output:
    print(json.dumps(question), file=output)
print(json.dumps({envelope!r}))
""")
    monkeypatch.setattr(cli, "ClaudeEngine", lambda settings: ClaudeEngine(settings, shim))
    return vault, Store.open(data), capture


def assert_private(store, capture):
    with store._connect() as db:
        for query in ("SELECT * FROM records", "SELECT chunks FROM deliveries"):
            assert MARKER not in repr([tuple(row) for row in db.execute(query)])
    assert not capture.exists()


def assert_finished(store, proposal, status):
    row = store.get_approval(proposal.id)
    assert (row.status, row.payload, row.payload_hash) == (status, None, None)


def test_cli_note_reject_approve_and_repeat(configured, capsys):
    vault, store, capture = configured

    def propose():
        assert cli.main(["ask", f"/note {BODY}"]) == 0
        [draft] = store.list_approvals("pending")
        output = capsys.readouterr().out
        assert all(part in output for part in (draft.id, draft.expires_at, "승인하면 inbox에 새 노트 1개 생성"))
        assert MARKER not in output
        assert (draft.owner, draft.allowed_channel, draft.payload) == (cli._os_user(), "cli", BODY)
        assert list(vault.rglob("*.md")) == []
        assert_private(store, capture)
        return draft

    rejected = propose()
    assert cli.main(["reject", rejected.id]) == 0
    capsys.readouterr()
    assert list(vault.rglob("*.md")) == []
    assert_finished(store, rejected, "rejected")
    approved = propose()
    assert cli.main(["approve", approved.id]) == 0
    [note] = vault.rglob("*.md")
    assert note.parent == vault / "javis" / "inbox"
    assert note.read_bytes() == BODY.encode()
    assert capsys.readouterr().out.strip() == note.relative_to(vault).as_posix()
    assert cli.main(["approve", approved.id]) != 0
    assert list(vault.rglob("*.md")) == [note]
    assert_finished(store, approved, "done")
    assert_private(store, capture)


@pytest.mark.parametrize("decide_on_pc", [False, True])
def test_telegram_note_is_once_only_and_decidable_by_chat_or_pc(api, configured, capsys, decide_on_pc):
    vault, store, capture = configured
    engine = cli.ClaudeEngine(cli.load_settings())
    now = datetime.now(UTC)
    telegram = bot(api, store, engine, now=lambda: now)
    forwarded = owner_update(39, f"/note {BODY}", message_id=80)
    forwarded["message"]["forward_origin"] = {"type": "user"}
    api.updates(forwarded)
    api.reply()
    telegram.poll_once()
    assert store.list_approvals("pending", now=now) == []

    message = owner_update(40, f"/note\t{BODY}", message_id=81)
    api.updates(message, message)
    api.reply()
    telegram.poll_once()
    [draft] = store.list_approvals("pending", now=now)
    proposal_reply = api.sent()[-1]["text"]
    assert all(part in proposal_reply for part in (draft.id, draft.expires_at, "메시지 81", "inbox에 새 노트 1개"))
    assert MARKER not in proposal_reply
    assert (draft.owner, draft.allowed_channel, draft.payload) == (str(CHAT), "telegram", BODY)
    assert list(vault.rglob("*.md")) == []

    command = f"승인 {draft.id}"
    api.updates(owner_update(41, command, chat=CHAT + 1))
    telegram.poll_once()
    assert store.get_approval(draft.id) == draft
    assert list(vault.rglob("*.md")) == []

    if decide_on_pc:
        assert cli.main(["approve", draft.id]) == 0
        result = capsys.readouterr().out.strip()
    else:
        decision = owner_update(42, command)
        api.updates(decision)
        api.add("sendMessage", 503)

        def disconnect(_delay):
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            bot(api, store, engine, now=lambda: now, sleep=disconnect).poll_once()
        assert_finished(store, draft, "done")
        failed_send = api.sent()[-1]["text"]
        api.reply()
        api.updates(decision, decision)
        bot(api, store, engine, now=lambda: now).poll_once()
        result = api.sent()[-1]["text"]
        assert failed_send == result
    [note] = vault.rglob("*.md")
    assert result == f"javis/inbox/{now:%Y%m%d-%H%M%S}-{draft.id[:8]}.md"
    assert note.relative_to(vault).as_posix() == result
    assert note.read_bytes() == BODY.encode()
    assert_finished(store, draft, "done")
    assert_private(store, capture)


@pytest.mark.parametrize("blocked_by", ["conflict", "policy"])
def test_note_reports_failure_and_clears_the_payload(configured, capsys, blocked_by):
    vault, store, capture = configured
    assert cli.main(["ask", f"/note {BODY}"]) == 0
    capsys.readouterr()
    [draft] = store.list_approvals("pending")
    inbox = vault / "javis" / "inbox"
    if blocked_by == "conflict":
        inbox.mkdir(parents=True)
        created = datetime.fromisoformat(draft.created_at)
        existing = inbox / f"{created:%Y%m%d-%H%M%S}-{draft.id[:8]}.md"
        existing.write_bytes(b"existing bytes\xff")
    else:
        (vault / "BRAIN.md").write_bytes(b"\xff")
    assert cli.main(["approve", draft.id]) != 0
    error = capsys.readouterr().err
    assert ("conflict" if blocked_by == "conflict" else "BRAIN.md") in error
    assert MARKER not in error and str(vault) not in error
    if blocked_by == "conflict":
        assert list(inbox.iterdir()) == [existing]
        assert existing.read_bytes() == b"existing bytes\xff"
    else:
        assert not inbox.exists()
    assert_finished(store, draft, "failed")
    assert_private(store, capture)


def test_note_without_configured_brain_creates_no_approval(api, configured, monkeypatch, capsys):
    _, store, capture = configured
    monkeypatch.delenv("JAVIS_BRAIN_DIR")
    assert cli.main(["ask", f"/note {BODY}"]) != 0
    assert "JAVIS_BRAIN_DIR" in capsys.readouterr().out
    api.updates(owner_update(40, f"/note {BODY}", message_id=81))
    api.reply()
    bot(api, store, cli.ClaudeEngine(cli.load_settings())).poll_once()
    assert "JAVIS_BRAIN_DIR" in api.sent()[0]["text"]
    assert store.list_approvals("pending") == []
    assert_private(store, capture)


@pytest.mark.parametrize("decision", ["approve", "reject", "expire"])
def test_curate_cli_reviews_the_bytes_before_deciding(configured, tmp_path, monkeypatch, capsys, decision):
    vault, store, _ = configured
    write(vault, EXISTING, EXAMPLE)
    write(vault, "me/원본.md", "점검 ORIGINAL_ONLY".encode())
    response = {"ops": [create(), append("P2", conflicts=["점검일 확인"])]}
    engine, calls = recording_cli(tmp_path, {"items": [{"text": json.dumps(response)}], "used_ids": ["N1"]})
    monkeypatch.setattr(cli, "ClaudeEngine", lambda _: engine)
    writes = []

    def exchange(directory, root, approval_id=None, op=None):
        if op is not None:
            assert store.get_approval(approval_id).status == "executing"
            target = vault / op["path"]
            target.write_bytes(adapter.verified_append(target.read_bytes(), op, []))
            writes.append(op["path"])
            return "ok"

    monkeypatch.setattr(adapter, "exchange", exchange)
    question = "/정리 점검 장소는 모형실"
    assert cli.main(["ask", question]) == 0
    [draft] = store.list_approvals("pending")
    shown = capsys.readouterr().out
    for part in ("새 노트 1개 / 기존 노트에 사실·링크 추가 1개 / 목차 0개", EXISTING,
                 "출처: 점검 장소는 모형실", "- 사실: 추가 사실", "점검일 확인", draft.id,
                 draft.expires_at, "어댑터 연결됨"):
        assert part in shown
    assert cli.main(["pending"]) == 0 and capsys.readouterr().out == shown
    initial = {p: p.read_bytes() for p in vault.rglob("*.md")}
    assert list(initial.values()).count(EXAMPLE) == 1 and len(initial) == 2 and not writes
    sent = calls.read_bytes()
    (_, probe), (argv, body) = map(json.loads, sent.splitlines())
    assert not probe and argv[argv.index("--tools") + 1] == ""
    payload = json.loads(body)
    assert payload["input"] == "점검 장소는 모형실"
    [ref] = payload["references"]
    assert ref["id"] == "N1" and "모형실" in ref["excerpt"]
    assert all(private not in body for private in ("me/", "javis/wiki/", "ORIGINAL_ONLY"))
    assert store.telegram_pending_notes(str(CHAT), datetime.now(UTC)) == []
    assert not store.recent(1)[0].request
    if decision == "expire":
        with store._connect() as db:
            db.execute("UPDATE approvals SET expires_at = ? WHERE id = ?",
                       ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), draft.id))
        assert cli.main(["approve", draft.id]) == 1
        assert_finished(store, draft, "expired")
    else:
        assert cli.main([decision, draft.id]) == 0
        assert shown.strip() in capsys.readouterr().out
        assert_finished(store, draft, "done" if decision == "approve" else "rejected")
    if decision == "approve":
        assert (vault / EXISTING).read_bytes() == EXAMPLE + "- 사실: 추가 사실\n".encode()
        created, _ = json.loads(draft.payload)["ops"]
        assert (vault / created["path"]).read_bytes() == created["content"].encode()
        assert cli.main(["approve", draft.id]) == 1 and writes == [EXISTING]
    else:
        assert {p: p.read_bytes() for p in vault.rglob("*.md")} == initial and not writes
    assert calls.read_bytes() == sent


@pytest.mark.parametrize("padding", [0, 420])
def test_curate_uses_only_bounded_related_note_excerpts(configured, padding):
    vault, _, _ = configured
    for i in range(8):
        title = f"항목 {i}"
        note = replace(NOTE, title=title, facts=(f"{i}" + "가" * padding + f' 점검 "{i}"',))
        write(vault, NOTES + title + ".md", serialize_note(note))
    prompt = conversation_prompt("점검", pin_root(vault))
    refs = json.loads(prompt.payload)["references"]
    block = json.dumps({"references": refs}, ensure_ascii=False, separators=(",", ":"))
    assert 0 < len(refs) <= 6 and len(block.encode()) <= 4096
    if padding == 0:
        assert len(refs) == 6
    for ref in refs:
        assert "점검" in ref["excerpt"] and len(ref["excerpt"].encode()) <= 1024
        assert set(ref) == {"id", "type", "excerpt"}
    assert set(prompt.evidence) == {"conversation", *(ref["id"] for ref in refs)}


def test_curate_usage_and_phone_do_not_call_engine(api, configured, capsys):
    vault, store, capture = configured
    for text in ("/정리", "/정리  \t", "/정리 /web 질문", "/정리 /정리 본문"):
        assert cli.main(["ask", text]) == 1
        assert "사용법: /정리 <내용>" in capsys.readouterr().out
    engine = cli.ClaudeEngine(cli.load_settings())
    assert "PC 전용" in handle(Request("telegram", "/정리 점검"), store, engine).text
    api.updates(owner_update(60, "/정리 점검"))
    api.reply()
    bot(api, store, engine).poll_once()
    assert "PC" in api.sent()[-1]["text"]
    assert not store.list_approvals("pending") and not list(vault.iterdir()) and not capture.exists()
