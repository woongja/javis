"""Single-poller Telegram channel with durable reply delivery."""

import http.client
import json
import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request as HTTPRequest, build_opener

from core.approval import ApprovalService, Executor
from core.engine import Engine
from core.gateway import Request, handle
from core.note import NOTE_EXECUTORS
from worklog.store import ApprovalKind, Record, Store

_API_BASE = "https://api.telegram.org"
_POLL_TIMEOUT = 25
_HTTP_TIMEOUT = 35
_CURSOR_LIFETIME = timedelta(days=1)
_CHUNK_LIMIT = 4000
# The gateway formats the footer before the real SQLite ID is available.
_DEFERRED_ID = 1000000000000000000
_ID_FOOTER = f"\n\n기록 ID {_DEFERRED_ID}"
_SAFE_ERROR = "응답을 처리하지 못했습니다."
_APPROVAL_ID = re.compile(r"[0-9a-fA-F]{32}")
_APPROVAL_USAGE = "사용법: 승인 대기 / 승인 <ID> / 거절 <ID>"
_PC_ONLY = "brain.import는 PC에서만 확인하고 결정할 수 있습니다."
_DECISION_REPLIES = {
    "claimed": "승인을 접수했습니다.",
    "rejected": "거절했습니다.",
    "expired": "만료된 승인입니다.",
    "not_found": "승인 ID를 찾을 수 없습니다.",
    "forbidden": "이 승인을 결정할 수 없습니다.",
    "already_decided": "이미 결정된 승인입니다.",
    "pc_only": _PC_ONLY,
    "executor_missing": "실행기 미등록 상태입니다.",
}


class TelegramError(Exception):
    def __init__(self, code: int):
        self.code = code
        super().__init__(str(code))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _DeferredSave:
    """Let the gateway read history and stage its record for an atomic delivery save."""

    def __init__(self, store: Store):
        self.store = store
        self.record: Record

    def __getattr__(self, name):
        return getattr(self.store, name)

    def save(self, record: Record) -> int:
        self.record = record
        return _DEFERRED_ID


def _split(text: str) -> list[str]:
    chunks = []
    start = size = 0
    for index, char in enumerate(text):
        units = 2 if ord(char) > 0xFFFF else 1
        if size + units > _CHUNK_LIMIT:
            chunks.append(text[start:index])
            start, size = index, 0
        size += units
    chunks.append(text[start:])
    return chunks


def _reply_chunks(text: str) -> list[str]:
    body = text.removesuffix(_ID_FOOTER)
    chunks = _split(body)
    if len(chunks[-1].encode("utf-16-le")) // 2 + len(_ID_FOOTER) <= _CHUNK_LIMIT:
        chunks[-1] += _ID_FOOTER
    else:
        chunks.append(_ID_FOOTER)
    return chunks


def _local_now() -> datetime:
    return datetime.now().astimezone()


class TelegramBot:
    def __init__(
        self, token: str, chat_id: int, store: Store, engine: Engine, *,
        api_base: str = _API_BASE,
        now: Callable[[], datetime] = _local_now,
        sleep: Callable[[float], None] = time.sleep,
        executors: Mapping[ApprovalKind, Executor] | None = None,
    ):
        self._token = token
        self._chat_id = chat_id
        self._store = store
        self._engine = engine
        self._approvals = ApprovalService(store, NOTE_EXECUTORS if executors is None else executors)
        self._api_base = api_base.rstrip("/")
        self._now = now
        self._sleep = sleep
        self._http = build_opener(ProxyHandler({}), _NoRedirect())
        saved = store.get_offset()
        self._cursor = (saved[0], datetime.fromisoformat(saved[1])) if saved else None

    def _response(self, method: str, payload: dict) -> tuple[int, dict]:
        request = HTTPRequest(
            f"{self._api_base}/bot{quote(self._token, safe=':')}/{method}",
            data=json.dumps(payload).encode("ascii"),
            headers={"Content-Type": "application/json"},
        )
        try:
            response = self._http.open(request, timeout=_HTTP_TIMEOUT)
        except HTTPError as error:
            response = error
        except (OSError, http.client.HTTPException, ValueError):
            return 0, {}

        with response:
            status = response.code
            try:
                body = json.loads(response.read())
            except (OSError, http.client.HTTPException, ValueError):
                body = {}
        return status, body if isinstance(body, dict) else {}

    def _request(self, method: str, payload: dict):
        backoff = 1
        while True:
            if method == "getUpdates":
                offset = self._fresh_offset()
                if offset is None:
                    payload.pop("offset", None)
                else:
                    payload["offset"] = offset
            status, body = self._response(method, payload)
            api_code = body.get("error_code")
            if type(api_code) is not int or not 400 <= api_code <= 599:
                api_code = 0
            codes = (status, api_code)
            if 300 <= status < 400:
                raise TelegramError(status)
            for fatal in (401, 409):
                if fatal in codes:
                    raise TelegramError(fatal)
            if 429 in codes:
                parameters = body.get("parameters")
                retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
                if type(retry_after) is int and retry_after >= 0:
                    self._sleep(max(1, retry_after))
                    continue
            elif status and all(code < 500 for code in codes):
                code = status if status >= 400 else api_code
                if code:
                    raise TelegramError(code)
                if 200 <= status < 300 and body.get("ok") is True:
                    result = body.get("result")
                    if method == "sendMessage" or isinstance(result, list):
                        return result
            self._sleep(random.uniform(backoff / 2, backoff))
            backoff = min(backoff * 2, 60)

    def _fresh_offset(self) -> int | None:
        if self._cursor is not None:
            offset, timestamp = self._cursor
            if self._now() - timestamp < _CURSOR_LIFETIME:
                return offset
        return None

    def _advance(self, offset: int) -> None:
        current = self._fresh_offset()
        if current is None or offset > current:
            self._store.set_offset(offset)
            self._cursor = (offset, self._now())

    def _resume_deliveries(self) -> None:
        for record_id, chunks, start in self._store.pending_deliveries():
            record = self._store.get(record_id)
            offset = int(record.update_key.split(":", 1)[0]) + 1
            created = datetime.fromisoformat(record.created_at)
            advance = None
            for index in range(start, len(chunks)):
                last = index == len(chunks) - 1
                text = chunks[index]
                if last and text.endswith(_ID_FOOTER):
                    text = text.removesuffix(str(_DEFERRED_ID)) + str(record_id)
                if self._token in text:
                    text = _SAFE_ERROR
                try:
                    self._request("sendMessage", {
                        "chat_id": self._chat_id, "text": text,
                        "link_preview_options": {"is_disabled": True},
                    })
                except TelegramError as error:
                    if error.code not in (400, 403):
                        raise
                    advance = self._delivery_offset(offset, created)
                    self._store.mark_failed(record_id, f"telegram_{error.code}", offset=advance)
                    break
                advance = self._delivery_offset(offset, created) if last else None
                self._store.mark_sent(record_id, index + 1, offset=advance)
            if advance is not None:
                self._cursor = (advance, self._now())

    def _delivery_offset(self, offset: int, created: datetime) -> int | None:
        # An expired update can still have a reply to send after Telegram resets its IDs.
        if self._now() - created >= _CURSOR_LIFETIME:
            return None
        current = self._fresh_offset()
        return offset if current is None or offset > current else None

    def _deny(self, key: str) -> None:
        self._store.save(Record(
            id=None, created_at=self._now().astimezone(UTC).isoformat(), channel="telegram",
            kind="ask", request="", items=(), status="denied", reason=None, engine=None,
            model=None, duration_s=None, ref_id=None, update_key=key,
        ))

    def _approval_chunks(self, text: str) -> list[str]:
        return [_SAFE_ERROR] if self._token in text else _reply_chunks(text + _ID_FOOTER)

    def _direct_owner(self, message: dict) -> bool:
        sender = message.get("from")
        return (
            isinstance(sender, dict) and type(sender.get("id")) is int
            and sender["id"] == self._chat_id and sender.get("is_bot") is False
            and not any(field.startswith("forward_") for field in message)
            and not message.get("is_automatic_forward")
        )

    def _handle_approval(self, message: dict, words: list[str], key: str) -> None:
        now = self._now()
        if not self._direct_owner(message):
            self._store.save_telegram_approval_reply(key, now, "forbidden", self._approval_chunks(
                "승인 명령은 설정된 개인 채팅에서 소유자가 직접 보낸 메시지만 처리합니다.",
            ))
        elif words == ["승인", "대기"]:
            notes = self._store.telegram_pending_notes(str(self._chat_id), now)
            lines = [f"승인 대기 {len(notes)}건"]
            lines.extend(
                f"메시지 {message_id} · note.create · 1건\n만료 {expires_at}\n승인 ID {id}"
                for message_id, expires_at, id in notes
            )
            lines.append(_PC_ONLY)
            self._store.save_telegram_approval_reply(key, now, "pending", self._approval_chunks("\n\n".join(lines)))
        elif len(words) != 2 or not _APPROVAL_ID.fullmatch(words[1]):
            self._store.save_telegram_approval_reply(key, now, "invalid_arguments", self._approval_chunks(_APPROVAL_USAGE))
        else:
            result = self._approvals.decide(
                words[1].lower(), str(self._chat_id), "telegram", words[0] == "승인", now, key,
                delivery=lambda status: self._approval_chunks(_DECISION_REPLIES[status]),
            )
            claim = result.approval
            if claim is not None:
                try:
                    outcome = self._approvals.execute(claim)
                    if outcome.message is None:
                        self._approvals.finish(claim.id, outcome.ok)
                    else:
                        chunks = _split(outcome.message) if self._token not in outcome.message else [_SAFE_ERROR]
                        self._approvals.finish(claim.id, outcome.ok, chunks=chunks)
                except Exception:
                    # The executor may have published its result. Keep the claim for local recovery.
                    pass
        self._resume_deliveries()

    def _handle_update(self, update) -> None:
        if not isinstance(update, dict):
            return
        update_id = update.get("update_id")
        if type(update_id) is not int or not 0 <= update_id < 2**63 - 1:
            return
        offset = update_id + 1
        message = update.get("message")
        date = message.get("date") if isinstance(message, dict) else None
        if type(date) is not int:
            self._advance(offset)
            return

        key = f"{update_id}:{date}"
        if self._store.by_update_key(key) is not None:
            self._resume_deliveries()
            self._advance(offset)
            return
        chat = message.get("chat")
        private_owner_chat = (isinstance(chat, dict) and chat.get("type") == "private"
                              and type(chat.get("id")) is int and chat["id"] == self._chat_id)
        text = message.get("text")
        words = text.split() if isinstance(text, str) else []
        if not private_owner_chat:
            self._deny(key)
            self._advance(offset)
            return
        if not words:
            self._advance(offset)
            return
        if self._token in text:
            self._deny(key)
            self._advance(offset)
            return
        if words[0] in {"승인", "거절"}:
            self._handle_approval(message, words, key)
            return

        message_id = message.get("message_id")

        def submit_note(body: str, now: datetime) -> None:
            self._approvals.propose_telegram_note(body, str(self._chat_id), message_id, key, now, self._approval_chunks)

        can_propose = self._direct_owner(message) and type(message_id) is int and 0 < message_id < 2**63
        staged = _DeferredSave(self._store)
        request = Request("telegram", text, key, submit_note if can_propose else None)
        reply = handle(request, staged, self._engine, self._now())
        if reply.kind == "note":
            self._resume_deliveries()
            return
        record = staged.record
        if reply.kind == "local_only":
            self._store.save(record)
            self._advance(offset)
            return
        fields = [reply.text, record.reason or "", record.model or ""]
        for item in record.items:
            fields.extend((item.text, *item.sources))
        if any(self._token in field for field in fields):
            record = replace(record, items=(), status="error", reason="invalid_output", model=None)
            chunks = [_SAFE_ERROR]
        else:
            chunks = _reply_chunks(reply.text)
        self._store.save(record, chunks=chunks)
        self._resume_deliveries()

    def poll_once(self) -> None:
        self._resume_deliveries()
        payload = {"timeout": _POLL_TIMEOUT, "allowed_updates": ["message"]}
        for update in self._request("getUpdates", payload):
            self._handle_update(update)

    def run(self) -> None:
        while True:
            self.poll_once()
