"""Resolve source questions using the saved answers."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from worklog.store import Item, Record, Store

_GUIDE = "출처 <기록ID> [항목번호] 형식으로 요청해 주세요."
_TEMPORAL = re.compile(r"^(방금|어제)(?=\s|$)")
_ANAPHORIC = re.compile(r"그거|그 답|지난번|아까|이전 답|출처\s+다시|뭐였")
_ORDINAL = re.compile(r"([^\s]+)\s*번째|([+-]?\d+)\s*번(?![\d호])")
_KOREAN = dict(zip("첫 한 두 세 네 다섯 여섯 일곱 여덟 아홉 열".split(), (1, 1, *range(2, 11))))


@dataclass(frozen=True)
class Recalled:
    items: tuple[Item, ...] = ()
    ref_id: int | None = None
    first_number: int = 1
    reason: str | None = None
    message: str = ""


def question_preview(text: str) -> str:
    return " ".join(text.split())[:60]


def _number(value: str) -> int:
    if not re.fullmatch(r"[+-]?[0-9]+", value):
        return 0
    # SQLite record IDs are signed 64-bit integers.
    digits = value.lstrip("+-").lstrip("0") or "0"
    if len(digits) > 19:
        return 0
    return int(digits) if not value.startswith("-") else 0


def _from_record(record: Record | None, number: int | None) -> Recalled:
    if record is None:
        return Recalled(reason="not_found", message=f"회수할 답변 기록을 찾지 못했습니다. {_GUIDE}")
    if number is None:
        return Recalled(record.items, record.id)
    if not 1 <= number <= len(record.items):
        return Recalled(ref_id=record.id, reason="invalid_item", message=f"해당 항목 번호가 없습니다. {_GUIDE}")
    return Recalled((record.items[number - 1],), record.id, number)


def recall(text: str, store: Store, now: datetime | None = None) -> Recalled | None:
    text = text.strip()
    words = text.split()
    if not words or "출처" not in text:
        return None

    if words[0] == "출처":
        if len(words) not in (2, 3) or not words[1].isascii() or not words[1].isdecimal():
            return Recalled(reason="ambiguous_recall", message=_GUIDE)
        record_id = _number(words[1])
        record = store.get(record_id) if 0 < record_id <= 2**63 - 1 else None
        if record is not None and (record.kind != "ask" or record.status != "ok"):
            record = None
        return _from_record(record, None if len(words) == 2 else _number(words[2]))

    temporal = _TEMPORAL.match(text)
    if temporal is None:
        if _ANAPHORIC.search(text) or text in {"출처?", "출처는?", "출처가 뭐야?"}:
            return Recalled(reason="ambiguous_recall", message=_GUIDE)
        return None

    numbers = _ORDINAL.findall(text)
    if len(numbers) > 1:
        return Recalled(reason="ambiguous_recall", message=_GUIDE)
    number = None
    if numbers:
        ordinal, digit = numbers[0]
        number = _KOREAN.get(ordinal, _number(ordinal or digit))
    if temporal[1] == "방금":
        return _from_record(store.last_ok_ask(), number)

    today = now.date() if now is not None else datetime.now().date()
    candidates = store.ok_asks_on(today - timedelta(days=1), now.tzinfo if now is not None else None)
    if len(candidates) > 1:
        choices = "\n".join(f"{record.id}  {question_preview(record.request)}" for record in candidates)
        return Recalled(reason="ambiguous_recall", message=f"어제 답변이 여러 건 있습니다.\n{choices}\n{_GUIDE}")
    return _from_record(candidates[0] if candidates else None, number)
