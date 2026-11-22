from dataclasses import replace
from email import policy
from email.parser import BytesParser

import pytest

from channels.telegram import bot as telegram
from core import gateway, voice
from core.engine import EngineResult
from core.voice import Audio, Provider
from teams import LocalOnly, Team, registry
from tests.test_telegram import CHAT, TOKEN, FakeEngine, api, bot, owner_update, store, update
from worklog.store import Item, Store

ANSWER = "synthetic answer body"
AUDIO = b"\x00synthetic audio bytes\xff"


@pytest.fixture
def speech(monkeypatch):
    class Speech:
        audio = Audio(AUDIO, "audio/ogg")
        error = None

        def __init__(self):
            self.created = 0
            self.calls = []

        def create(self):
            self.created += 1
            return self

        def synthesize(self, text):
            self.calls.append(text)
            if self.error is not None:
                raise self.error
            return self.audio

    fake = Speech()
    monkeypatch.setitem(voice.PROVIDERS, "local", Provider(tts=fake.create))
    return fake


@pytest.fixture
def enabled(tmp_path, speech):
    (tmp_path / "settings.toml").write_text("[voice]\nenabled = true\ntts = true\n", encoding="utf-8")
    return speech


def methods(api):
    return [method for method, _ in api.requests]


@pytest.mark.parametrize("settings", ["", "enabled = true\n", "tts = true\n"])
def test_voice_off_preserves_text(api, store, tmp_path, speech, settings):
    if settings:
        (tmp_path / "settings.toml").write_text("[voice]\n" + settings, encoding="utf-8")
    api.updates(update())
    api.reply()
    bot(api, store, FakeEngine(ANSWER)).poll_once()

    assert methods(api) == ["getUpdates", "sendMessage"]
    assert api.sent()[0]["text"] == f"1. {ANSWER}\n   기록된 출처 없음\n\n기록 ID 1"
    assert speech.created == 0 and speech.calls == []


@pytest.mark.parametrize(("media_type", "method", "field", "suffix"), [
    ("audio/ogg", "sendVoice", "voice", "ogg"),
    ("audio/mpeg", "sendDocument", "document", "mp3"),
    ("audio/wav", "sendDocument", "document", "wav"),
    ("audio/aiff", "sendDocument", "document", "aiff"),
])
def test_upload_follows_all_text_chunks(api, store, enabled, media_type, method, field, suffix):
    enabled.audio = Audio(AUDIO, media_type)
    answer = ANSWER + "a" * 4500
    item = update()
    api.updates(item, item)
    api.reply()
    api.reply()
    api.add(method)
    bot(api, store, FakeEngine(answer)).poll_once()

    assert methods(api) == ["getUpdates", "sendMessage", "sendMessage", method]
    assert enabled.created == 1 and enabled.calls == [answer]
    assert "".join(body["text"] for body in api.sent()) == f"1. {answer}\n   기록된 출처 없음\n\n기록 ID 1"
    content_type, data = api.requests[-1][1]
    parts = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + data
    )
    chat, attachment = parts.iter_parts()
    assert chat.get_param("name", header="Content-Disposition") == "chat_id"
    assert chat.get_payload(decode=True) == str(CHAT).encode("ascii")
    assert attachment.get_param("name", header="Content-Disposition") == field
    assert attachment.get_filename() == f"reply.{suffix}"
    assert attachment.get_content_type() == media_type
    assert attachment.get_payload(decode=True) == AUDIO


def test_only_successful_web_answers_are_eligible(api, store, enabled, monkeypatch):
    def teams(engine):
        entries = registry(engine)
        entries["/note"] = replace(entries["/note"], availability=None)
        entries["/local"] = Team("local", "", "/local <query>", lambda *_: LocalOnly("local result"))
        return entries

    monkeypatch.setattr(gateway, "registry", teams)

    class ErrorEngine:
        def run(self, text):
            assert text == "web error"
            return EngineResult("error", "timeout", (), None, 0)

    gateway.handle(gateway.Request("telegram", "query"), store, FakeEngine(ANSWER))
    api.updates(
        owner_update(40, "승인 대기"),
        update(id=41, text="기록 검색 query"),
        update(id=42, text="출처 1"),
        update(id=43, text="/brain query"),
        owner_update(44, "/note a draft", message_id=81),
        update(id=45, text="/unknown"),
        update(id=46, text="/local query"),
        update(id=47, text="/web web error"),
        update(id=48, chat=CHAT + 1),
        update(id=49, kind="group"),
    )
    for _ in range(7):
        api.reply()
    bot(api, store, ErrorEngine()).poll_once()

    assert methods(api) == ["getUpdates"] + ["sendMessage"] * 7
    assert enabled.created == 0 and enabled.calls == []
    assert store.get_offset()[0] == 50
    assert store.by_update_key("42:101").ref_id == 1
    assert store.by_update_key("46:101").kind == "local"


def dump(store):
    with store._connect() as db:
        return "\n".join(db.iterdump())


@pytest.mark.parametrize("failure", [None, "synthesis", "http", "redirect", "oversized"])
def test_voice_does_not_write_to_store_or_interrupt_polling(
    api, store, enabled, failure, monkeypatch, caplog, tmp_path,
):
    before_voice = []
    synthesize = enabled.synthesize

    def synthesize_after_delivery(text):
        assert store.pending_deliveries() == []
        assert store.get_offset()[0] == 41
        before_voice.append((dump(store), set(tmp_path.iterdir())))
        return synthesize(text)

    monkeypatch.setattr(enabled, "synthesize", synthesize_after_delivery)
    sensitive = f"{ANSWER} {AUDIO!r} {TOKEN}"
    if failure == "synthesis":
        enabled.error = RuntimeError(sensitive)
    if failure == "oversized":
        monkeypatch.setattr(telegram, "_MAX_VOICE_UPLOAD", 512)
        enabled.audio = Audio(b"a" * 513, "audio/ogg")
    api.updates(update())
    api.reply()
    if failure == "http":
        api.add("sendVoice", 503, {"ok": False, "description": sensitive})
    elif failure == "redirect":
        api.add("sendVoice", 302, headers={"Location": api.url + "/redirect-target"})
    elif failure is None:
        api.add("sendVoice")

    poller = bot(api, store, FakeEngine(ANSWER))
    poller.poll_once()

    expected = ["getUpdates", "sendMessage"]
    if failure not in {"synthesis", "oversized"}:
        expected.append("sendVoice")
    assert methods(api) == expected
    messages = [] if failure is None else [
        "Telegram voice synthesis failed" if failure == "synthesis" else "Telegram voice upload failed"
    ]
    assert caplog.messages == messages
    assert enabled.created == 1 and enabled.calls == [ANSWER]
    persisted = dump(store)
    assert before_voice == [(persisted, set(tmp_path.iterdir()))]
    assert AUDIO.hex() not in persisted.lower() and "synthetic audio bytes" not in persisted
    assert store.by_update_key("40:101").items == (Item(ANSWER, ()),)

    api.updates(update(), update(id=41, text="/brain"))
    api.reply()
    poller.poll_once()
    assert methods(api) == expected + ["getUpdates", "sendMessage"]
    assert store.get_offset()[0] == 42


def test_partial_restart_and_permanent_text_failure_skip_speech(api, store, enabled, tmp_path):
    engine = FakeEngine(ANSWER * 250)
    item = update()
    api.updates(item)
    api.reply()
    api.add("sendMessage", 503)

    def interrupted(_delay):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        bot(api, store, engine, sleep=interrupted).poll_once()
    assert store.pending_deliveries()[0][2] == 1

    api.reply()
    api.updates(item, update(id=41))
    api.reply()
    api.add("sendMessage", 403)
    reopened = Store.open(tmp_path)
    bot(api, reopened, engine).poll_once()

    assert methods(api) == [
        "getUpdates", "sendMessage", "sendMessage",
        "sendMessage", "getUpdates", "sendMessage", "sendMessage",
    ]
    assert api.sent()[1] == api.sent()[2]
    assert enabled.created == 0 and enabled.calls == []
    assert reopened.pending_deliveries() == []
    assert reopened.get_offset()[0] == 42
