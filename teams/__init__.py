"""Fixed team registry and the result types shared by channel adapters."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from core.engine import Engine, EngineResult


ReplyKind = Literal["web", "local_only", "notice"]


@dataclass(frozen=True)
class WebAnswer:
    result: "EngineResult"


@dataclass(frozen=True)
class LocalOnly:
    text: str
    status: Literal["ok", "error"] = "ok"


@dataclass(frozen=True)
class Notice:
    text: str


TeamResult = WebAnswer | LocalOnly | Notice


@dataclass(frozen=True)
class Team:
    name: str
    description: str
    usage: str
    handler: Callable[[str, str], TeamResult]
    unavailable_reason: str = ""

    def available(self) -> tuple[bool, str]:
        return not self.unavailable_reason, self.unavailable_reason


_BRAIN_UNAVAILABLE = "두뇌 검색이 아직 구현되거나 설정되지 않았습니다."
_NOTE_UNAVAILABLE = "메모 기능 준비 중입니다."


def _research(engine: "Engine", text: str, _channel: str) -> WebAnswer:
    return WebAnswer(engine.run(text))


def _placeholder(reason: str, _text: str, _channel: str) -> Notice:
    return Notice(reason)


def registry(engine: "Engine") -> dict[str, Team]:
    return {
        "/web": Team("research", "웹 질의", "/web <질문>", partial(_research, engine)),
        "/brain": Team(
            "brain", "로컬 두뇌 검색", "/brain <검색어>",
            partial(_placeholder, _BRAIN_UNAVAILABLE), _BRAIN_UNAVAILABLE,
        ),
        "/note": Team(
            "note", "메모 제안", "/note <본문>",
            partial(_placeholder, _NOTE_UNAVAILABLE), _NOTE_UNAVAILABLE,
        ),
    }
