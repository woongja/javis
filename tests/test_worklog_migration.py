import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from threading import Barrier

import pytest

from tests.test_worklog_store import SAMPLE
from worklog import Store
from worklog.store import _MIGRATIONS, _SCHEMA_VERSION


# Snapshot of the version 1 DDL, independent of Store.open and its migrations.
V1_SCHEMA = """
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version VALUES (1);
CREATE TABLE records (
    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, created_utc TEXT NOT NULL,
    channel TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('ask', 'recall')),
    request TEXT NOT NULL, items TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'error', 'timeout', 'quota', 'denied')),
    reason TEXT, engine TEXT, model TEXT, duration_s REAL, ref_id INTEGER,
    update_key TEXT UNIQUE
);
CREATE INDEX records_created ON records (created_utc, id);
CREATE INDEX records_ok_asks ON records (created_utc, id)
WHERE kind = 'ask' AND status = 'ok';
CREATE TABLE deliveries (
    record_id INTEGER PRIMARY KEY REFERENCES records (id),
    chunks TEXT NOT NULL, chunk_count INTEGER NOT NULL,
    next_index INTEGER NOT NULL DEFAULT 0 CHECK (next_index BETWEEN 0 AND chunk_count),
    failure_reason TEXT
);
CREATE TABLE telegram_cursor (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    offset INTEGER NOT NULL, updated_at TEXT NOT NULL
);
"""

V1_RECORD = replace(SAMPLE, id=27, channel="telegram", update_key="42:1795219200")
V1_CHUNKS = ["first", "second", "third"]
V1_CURSOR = (42, "2026-11-21T00:01:00+00:00")


@pytest.fixture
def v1_db(tmp_path):
    path = tmp_path / "javis.db"
    record = V1_RECORD
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        db.executescript(V1_SCHEMA)
        db.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id, record.created_at, "2026-11-21T00:00:00.000000+00:00",
                record.channel, record.kind, record.request,
                json.dumps([(item.text, item.sources) for item in record.items]),
                record.status, record.reason, record.engine, record.model,
                record.duration_s, record.ref_id, record.update_key,
            ),
        )
        db.execute(
            "INSERT INTO deliveries VALUES (?, ?, ?, ?, NULL)",
            (record.id, json.dumps(V1_CHUNKS), len(V1_CHUNKS), 1),
        )
        db.execute("INSERT INTO telegram_cursor VALUES (1, ?, ?)", V1_CURSOR)
    return path


def assert_migrated(path, store):
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(_SCHEMA_VERSION,)]
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('approvals', 'telegram_note_proposals') ORDER BY name"
        ).fetchall() == [("approvals",), ("telegram_note_proposals",)]
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.get(V1_RECORD.id) == V1_RECORD
    assert store.by_update_key(V1_RECORD.update_key) == V1_RECORD
    assert store.pending_deliveries() == [(V1_RECORD.id, V1_CHUNKS, 1)]
    assert store.get_offset() == V1_CURSOR
    assert store.list_approvals("pending") == []


def test_migrates_v1_and_preserves_worklog(v1_db):
    store = Store.open(v1_db.parent)
    assert_migrated(v1_db, store)
    assert store.save(replace(V1_RECORD, request="Duplicate"), chunks=["new"]) == V1_RECORD.id
    store.mark_sent(V1_RECORD.id, 3, offset=43)
    reopened = Store.open(v1_db.parent)
    assert reopened.get(V1_RECORD.id) == V1_RECORD
    assert reopened.pending_deliveries() == []
    assert reopened.get_offset()[0] == 43


def test_migrates_v3_and_preserves_delivery_and_approval(v1_db):
    with closing(sqlite3.connect(v1_db)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        for migration in _MIGRATIONS[1:3]:
            for statement in migration:
                db.execute(statement)
        db.execute("UPDATE schema_version SET version = 3")

    old = Store(v1_db)
    now = datetime.fromisoformat(V1_RECORD.created_at)
    approval_key = "43:1795219200"
    approval = old.create_telegram_note_proposal(
        owner="test-chat", message_id=7, update_key=approval_key, now=now,
        payload="Example note", delivery=lambda *_: ["Approval reply"],
    )
    approval_record = old.by_update_key(approval_key)
    old.mark_failed(approval_record.id, "test failure")

    store = Store.open(v1_db.parent)
    assert store.get(V1_RECORD.id) == store.by_update_key(V1_RECORD.update_key) == V1_RECORD
    assert store.pending_deliveries() == [(V1_RECORD.id, V1_CHUNKS, 1)]
    assert store.get_offset() == V1_CURSOR
    assert store.by_update_key(approval_key) == approval_record
    assert store.get_approval(approval.id) == approval
    assert store.telegram_pending_notes(approval.owner, now) == [(7, approval.expires_at, approval.id)]

    for kind in ("local", "notice"):
        record = replace(SAMPLE, kind=kind, request="", items=())
        record_id = store.save(record)
        assert store.get(record_id) == replace(record, id=record_id)
    assert store.last_ok_ask() == V1_RECORD

    with store._connect() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == _SCHEMA_VERSION
        failed_delivery = db.execute(
            "SELECT chunks, failure_reason FROM deliveries WHERE record_id = ?", (approval_record.id,)
        ).fetchone()
        assert (json.loads(failed_delivery["chunks"]), failed_delivery["failure_reason"]) == (
            ["Approval reply"], "test failure",
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.execute("UPDATE deliveries SET record_id = -1 WHERE record_id = ?", (V1_RECORD.id,))


def test_two_stores_can_migrate_the_same_v1_database(v1_db):
    ready = Barrier(2, timeout=5)

    def open_store():
        ready.wait()
        return Store.open(v1_db.parent)

    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = [workers.submit(open_store) for _ in range(2)]
        assert_migrated(v1_db, first.result())
        assert_migrated(v1_db, second.result())


def test_migration_failure_rolls_back_and_can_be_retried(v1_db):
    with closing(sqlite3.connect(v1_db)) as db, db:
        db.execute("CREATE INDEX approvals_status_created ON records (status)")
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        Store.open(v1_db.parent)
    with closing(sqlite3.connect(v1_db)) as db, db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(1,)]
        assert db.execute("SELECT name FROM sqlite_master WHERE name = 'approvals'").fetchall() == []
        db.execute("DROP INDEX approvals_status_created")
    assert_migrated(v1_db, Store.open(v1_db.parent))


def test_rejects_future_schema_version(v1_db):
    future_version = _SCHEMA_VERSION + 1
    with closing(sqlite3.connect(v1_db)) as db, db:
        db.execute("UPDATE schema_version SET version = ?", (future_version,))
    with pytest.raises(sqlite3.DatabaseError, match="unsupported worklog schema version"):
        Store.open(v1_db.parent)
    with closing(sqlite3.connect(v1_db)) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(future_version,)]
        assert db.execute("SELECT name FROM sqlite_master WHERE name = 'approvals'").fetchall() == []
