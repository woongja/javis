"""Shared request, persistence and reply formatting for channel adapters."""

from dataclasses import dataclass
from datetime import UTC, datetime

from worklog.store import Item, Record, Store

from .engine import Engine
from .recall import recall

_FAILURES = {
    "timeout": "엔진 제한 시간이 지났습니다.",
    "quota": "엔진 사용 한도에 도달했습니다.",
    "configuration": "Claude CLI 설치와 구독 로그인을 확인해 주세요.",
    "invalid_output": "엔진 응답 형식이 올바르지 않습니다.",
    "output_limit": "엔진 응답이 크기 제한을 초과했습니다.",
}


@dataclass(frozen=True)
class Request:
    channel: str
    text: str
    processing_key: str | None = None


@dataclass(frozen=True)
class Reply:
    text: str
    record_id: int


def _render(items: tuple[Item, ...], *, sources_only: bool = False, start: int = 1) -> str:
    lines = []
    for number, item in enumerate(items, start):
        label = f"{number}." if sources_only else f"{number}. {item.text}"
        lines.append(label)
        lines.extend(f"   {source}" for source in item.sources or ("기록된 출처 없음",))
    return "\n".join(lines)


def handle(request: Request, store: Store, engine: Engine, now: datetime | None = None) -> Reply:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    recalled = recall(request.text, store, now)
    if recalled is not None:
        record = Record(
            id=None, created_at=timestamp, channel=request.channel, kind="recall",
            request=request.text, items=recalled.items, status="error" if recalled.reason else "ok",
            reason=recalled.reason, engine=None, model=None, duration_s=None,
            ref_id=recalled.ref_id, update_key=request.processing_key,
        )
        message = recalled.message or _render(recalled.items, sources_only=True, start=recalled.first_number)
    else:
        result = engine.run(request.text)
        record = Record(
            id=None, created_at=timestamp, channel=request.channel, kind="ask",
            request=request.text, items=result.items, status=result.status, reason=result.reason,
            engine="claude", model=result.model, duration_s=result.duration_s,
            ref_id=None, update_key=request.processing_key,
        )
        message = (
            _render(result.items) if result.status == "ok"
            else _FAILURES.get(result.reason, _FAILURES.get(result.status, "엔진 요청에 실패했습니다."))
        )

    record_id = store.save(record)
    return Reply(f"{message}\n\n기록 ID {record_id}", record_id)
