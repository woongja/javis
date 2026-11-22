"""Command-line interface for the local javis gateway."""

import argparse
import ctypes
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from channels.telegram.bot import TelegramBot, TelegramError
from harness import HarnessError
from harness.paths import env_value, repo_root
from worklog import Approval, Store

from .approval import ApprovalService, is_interrupted
from .config import data_dir, load_settings
from .engine import ClaudeEngine
from .gateway import Request, handle
from .recall import question_preview


def _count(value: str) -> int:
    try:
        count = int(value)
    except ValueError:
        count = -1
    if not 0 <= count <= 2**63 - 1:
        raise argparse.ArgumentTypeError("N must be a nonnegative integer within the SQLite range")
    return count


def _telegram_settings() -> tuple[str, int] | None:
    checkout = Path(__file__).resolve().parents[1]
    if not (checkout / ".git").exists():
        try:
            checkout = repo_root()
        except HarnessError:
            checkout = None
    values = []
    for name in ("JAVIS_TELEGRAM_TOKEN", "JAVIS_TELEGRAM_CHAT_ID"):
        value = os.environ.get(name, "").strip()
        if not value and checkout is not None:
            try:
                value = env_value(checkout, name, env={}) or ""
            except (OSError, ValueError):
                pass
        values.append(value)

    token, chat = values
    try:
        chat_id = int(chat)
    except ValueError:
        print("JAVIS_TELEGRAM_CHAT_ID", file=sys.stderr)
        chat_id = None
    if not token:
        print("JAVIS_TELEGRAM_TOKEN", file=sys.stderr)
    return (token, chat_id) if token and chat_id is not None else None


def _os_user() -> str:
    if os.name == "nt":
        from ctypes import wintypes

        get_username = ctypes.WinDLL("advapi32", use_last_error=True).GetUserNameW
        get_username.argtypes = (wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        get_username.restype = wintypes.BOOL
        username = ctypes.create_unicode_buffer(257)  # UNLEN plus terminator
        size = wintypes.DWORD(len(username))
        if not get_username(username, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return username.value

    import pwd

    return pwd.getpwuid(os.geteuid()).pw_name


_UNKNOWN = "결과 미상 — 확인 필요"
_DECISION_MESSAGES = {
    "rejected": "거절했습니다.",
    "expired": "만료된 승인입니다.",
    "not_found": "승인 ID를 찾을 수 없습니다.",
    "forbidden": "이 승인을 결정할 수 없습니다.",
    "already_decided": "이미 결정된 승인입니다.",
}


def _approval_message(message: str, *, ok: bool = False) -> int:
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def _approval_preview(text: str) -> str:
    return "".join(char if char.isprintable() else "?" for char in question_preview(text))


def _approval_summary(approval: Approval) -> str:
    if approval.kind == "note.create":
        return _approval_preview(approval.payload or "")
    try:
        manifest = json.loads(approval.payload or "")
    except ValueError:
        return "가져오기"
    if not isinstance(manifest, dict):
        return "가져오기"
    destination = manifest.get("dest")
    summary = _approval_preview(destination) if isinstance(destination, str) else "가져오기"
    files = manifest.get("files")
    return f"{summary} ({len(files)}개 파일)" if isinstance(files, list) else summary


def _list_pending(store: Store, actor: str) -> int:
    pending = [
        row for row in store.list_approvals("pending") if (row.owner, row.allowed_channel) == (actor, "cli")
    ]
    interrupted = [row for row in store.list_approvals("executing") if is_interrupted(row)]
    for row in pending + interrupted:
        expires = datetime.fromisoformat(row.expires_at).astimezone()
        line = f"{row.id}\t{row.kind}\t{_approval_summary(row)}\t{expires:%Y-%m-%d %H:%M}"
        print(f"{line}\t{_UNKNOWN}" if row.status == "executing" else line)
    return 0


def _decide(store: Store, id: str, actor: str, *, approve: bool) -> int:
    service = ApprovalService(store)
    if approve:
        row = store.get_approval(id)
        if row and (row.status, row.owner, row.allowed_channel) == ("pending", actor, "cli"):
            # The kind is fixed at creation; decide still checks the row atomically.
            if row.kind not in service.executors:
                return _approval_message(f"{row.kind}: 실행기 미등록")
    decision = service.decide(id, actor, "cli", approve)
    if decision.status != "claimed":
        return _approval_message(_DECISION_MESSAGES[decision.status], ok=decision.status == "rejected")

    claim = decision.approval
    assert claim is not None
    try:
        ok = service.executors[claim.kind](claim)
        if service.finish(id, ok):
            return _approval_message("실행을 완료했습니다." if ok else "실행에 실패했습니다.", ok=ok)
    except (Exception, KeyboardInterrupt):
        # The executor may already have published its result.
        pass
    return _approval_message(f"{id}\t{_UNKNOWN}")


def _resolve(store: Store, id: str, *, ok: bool) -> int:
    row = store.get_approval(id)
    if row is None:
        return _approval_message(_DECISION_MESSAGES["not_found"])
    if not is_interrupted(row):
        return _approval_message("중단이 확인된 실행만 마감할 수 있습니다.")
    assert row.executor_pid is not None and row.executor_started_at is not None
    # Match the same executing claim observed before probing the process.
    if not store.finish_approval(id, ok=ok, executor=(row.executor_pid, row.executor_started_at)):
        return _approval_message(_DECISION_MESSAGES["already_decided"])
    return _approval_message("완료로 마감했습니다." if ok else "실패로 마감했습니다.", ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="javis", description="Ask javis, view the local worklog, and manage approvals.")
    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask", help="Ask a question or recall saved sources")
    ask.add_argument("text")
    log = commands.add_parser("log", help="Show recent worklog entries")
    log.add_argument("-n", metavar="N", type=_count, default=20)
    commands.add_parser("serve", help="Serve requests from Telegram")
    commands.add_parser("pending", help="List actionable approvals and interrupted runs")
    commands.add_parser("approve", help="Approve and execute a pending request").add_argument("id", metavar="ID")
    commands.add_parser("reject", help="Reject a pending request").add_argument("id", metavar="ID")
    resolve = commands.add_parser("resolve", help="Record the verified outcome of an interrupted run")
    resolve.add_argument("id", metavar="ID")
    resolve.add_argument("outcome", choices=("done", "failed"))
    args = parser.parse_args(argv)
    if args.command == "ask" and not args.text.strip():
        ask.error("text must not be empty")

    if args.command == "serve":
        try:
            telegram = _telegram_settings()
        except KeyboardInterrupt:
            return 0
        if telegram is None:
            return 1

    try:
        directory = data_dir()
    except (OSError, RuntimeError, ValueError):
        print(
            "javis: 데이터 폴더 설정 오류. JAVIS_DATA_DIR 또는 기본값 ~/.javis는 javis 저장소 밖에 있어야 합니다.",
            file=sys.stderr,
        )
        return 1

    if args.command in ("ask", "serve"):
        try:
            settings = load_settings()
        except (OSError, ValueError):
            print("javis: 엔진 설정을 읽지 못했습니다.", file=sys.stderr)
            return 1

    try:
        store = Store.open(directory)
        if args.command == "serve":
            try:
                token, chat_id = telegram
                TelegramBot(token, chat_id, store, ClaudeEngine(settings)).run()
            except TelegramError as error:
                print(f"Telegram {error.code}", file=sys.stderr)
                return 1
            except KeyboardInterrupt:
                return 0
            return 0
        if args.command == "log":
            for record in store.recent(args.n):
                local_time = datetime.fromisoformat(record.created_at).astimezone()
                print(f"{record.id}\t{local_time:%Y-%m-%d %H:%M}\t{question_preview(record.request)}\t{record.status}")
            return 0
        if args.command == "pending":
            return _list_pending(store, _os_user())
        if args.command in ("approve", "reject"):
            return _decide(store, args.id, _os_user(), approve=args.command == "approve")
        if args.command == "resolve":
            return _resolve(store, args.id, ok=args.outcome == "done")
        reply = handle(Request("cli", args.text), store, ClaudeEngine(settings))
        saved = store.get(reply.record_id)
        if saved is not None:
            print(reply.text)
            return 0 if saved.status == "ok" else 1
    except (OSError, sqlite3.Error):
        pass
    print("javis: 일지 처리에 실패했습니다.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
