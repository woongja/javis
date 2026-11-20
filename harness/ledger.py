"""Append-only run log (.harness/ledger.jsonl). Later read by the WORK LOG HUD."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from harness.paths import state_dir

EVENTS = ("task.new", "dispatch.start", "dispatch.end", "review.start", "review.end", "gate", "merge")


def append(root: Path, event: str, **fields) -> dict:
    record = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "event": event, **fields}
    path = state_dir(root) / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record
