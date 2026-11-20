"""Summarize completed worker and review runs from the ledger."""
from __future__ import annotations

import argparse
import json
from math import isfinite

from harness import HarnessError
from harness.paths import repo_root, state_dir


def _cmd_usage(args: argparse.Namespace) -> int:
    path = state_dir(repo_root()) / "ledger.jsonl"
    groups = {}
    skipped = 0
    try:
        with path.open("rb") as ledger:
            for line in ledger:
                try:
                    run = json.loads(line.decode("utf-8"))
                    if not isinstance(run, dict) or not isinstance(run.get("event"), str):
                        raise ValueError("invalid ledger record")
                    if run["event"] not in ("dispatch.end", "review.end"):
                        continue
                    duration = run.get("duration_s", 0)
                    if type(duration) not in (int, float) or not isfinite(duration) or duration < 0:
                        raise ValueError("invalid run duration")
                    group = run.get(args.by)
                    if args.by == "team":
                        group = (run.get("role") or "").partition(".")[0] or run.get("tool")
                    if group is not None and not isinstance(group, str):
                        raise ValueError("invalid group name")
                except (ValueError, AttributeError, OverflowError, RecursionError):
                    skipped += 1
                    continue
                runs, seconds, failed, quota = groups.get(group or "unknown", (0, 0.0, 0, 0))
                status = run.get("status")
                groups[group or "unknown"] = (
                    runs + 1, seconds + duration,
                    failed + (run.get("exit", 0) != 0 or status == "failed"),
                    quota + (status == "blocked:quota" or run.get("quota_hit") is True),
                )
    except FileNotFoundError:
        print("no runs yet")
        return 0
    except OSError as exc:
        raise HarnessError(f"could not read ledger: {exc}") from exc

    width = max(map(len, [args.by, "TOTAL", *groups]))
    print(f"{args.by:<{width}}  {'runs':>6}  {'minutes':>9}  {'failed':>6}  {'quota':>5}")
    total = tuple(sum(column) for column in zip(*groups.values())) if groups else (0, 0.0, 0, 0)
    for group, (runs, seconds, failed, quota) in [*sorted(groups.items()), ("TOTAL", total)]:
        print(f"{group:<{width}}  {runs:>6}  {seconds / 60:>9.1f}  {failed:>6}  {quota:>5}")
    if skipped:
        print(f"skipped {skipped} bad lines")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("usage", help="summarize worker and review runs")
    p.add_argument("--by", choices=("team", "model", "role", "task"), default="team")
    p.set_defaults(func=_cmd_usage)
