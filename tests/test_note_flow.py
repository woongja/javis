from datetime import UTC, datetime

import pytest

from core import cli
from core.engine import ClaudeEngine
from tests.test_core_engine import fake_claude, success
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
