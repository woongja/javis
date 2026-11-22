import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from threading import Barrier

import pytest

from tests.test_worklog_store import SAMPLE
from worklog import Store


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
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(2,)]
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
    with closing(sqlite3.connect(v1_db)) as db, db:
        db.execute("UPDATE schema_version SET version = 3")
    with pytest.raises(sqlite3.DatabaseError, match="unsupported worklog schema version"):
        Store.open(v1_db.parent)
    with closing(sqlite3.connect(v1_db)) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(3,)]
        assert db.execute("SELECT name FROM sqlite_master WHERE name = 'approvals'").fetchall() == []
