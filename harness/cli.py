"""`uv run harness <command>` entry point."""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from harness import HarnessError, __version__

# Each command module exposes register(subparsers) and sets args.func.
_REGISTRARS: list[str] = []


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness", description="javis development harness")
    parser.add_argument("--version", action="version", version=f"harness {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    for module_name in _REGISTRARS:
        module = __import__(f"harness.{module_name}", fromlist=["register"])
        module.register(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --version / --help / usage errors
        return int(exc.code or 0)
    func: Callable[[argparse.Namespace], int] | None = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0
    try:
        return func(args)
    except HarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
