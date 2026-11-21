"""Command-line interface for the local javis gateway."""

import argparse
import sqlite3
import sys

from worklog.store import Store

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="javis", description="Ask javis and view the local worklog.")
    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask", help="Ask a question or recall saved sources")
    ask.add_argument("text")
    log = commands.add_parser("log", help="Show recent worklog entries")
    log.add_argument("-n", metavar="N", type=_count, default=20)
    args = parser.parse_args(argv)
    if args.command == "ask" and not args.text.strip():
        ask.error("text must not be empty")

    try:
        directory = data_dir()
    except (OSError, RuntimeError, ValueError):
        print(
            "javis: 데이터 폴더 설정 오류. JAVIS_DATA_DIR 또는 기본값 ~/.javis는 javis 저장소 밖에 있어야 합니다.",
            file=sys.stderr,
        )
        return 1

    if args.command == "ask":
        try:
            settings = load_settings()
        except (OSError, ValueError):
            print("javis: 엔진 설정을 읽지 못했습니다.", file=sys.stderr)
            return 1

    try:
        store = Store.open(directory)
        if args.command == "log":
            for record in store.recent(args.n):
                print(f"{record.id}\t{record.created_at}\t{question_preview(record.request)}\t{record.status}")
            return 0
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
