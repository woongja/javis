"""SQLite storage for request history, Telegram deliveries, and approvals."""

import hashlib
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class Item:
    text: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Record:
    id: int | None
    created_at: str
    channel: str
    kind: Literal["ask", "recall"]
    request: str
    items: tuple[Item, ...]
    status: Literal["ok", "error", "timeout", "quota", "denied"]
    reason: str | None
    engine: str | None
    model: str | None
    duration_s: float | None
    ref_id: int | None
    update_key: str | None


ApprovalKind = Literal["note.create", "brain.import"]
ApprovalStatus = Literal["pending", "executing", "done", "failed", "rejected", "expired"]


@dataclass(frozen=True)
class Approval:
    id: str
    kind: ApprovalKind
    owner: str
    allowed_channel: str
    created_at: str
    expires_at: str
    status: ApprovalStatus
    payload: str | None = field(repr=False)
    payload_hash: str | None
    executor_pid: int | None
    executor_started_at: str | None
    decision_update_key: str | None


_BUSY_TIMEOUT = 5.0
_SCHEMA_V1 = (
    """
    CREATE TABLE records (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        created_utc TEXT NOT NULL,
        channel TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('ask', 'recall')),
        request TEXT NOT NULL,
        items TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('ok', 'error', 'timeout', 'quota', 'denied')),
        reason TEXT,
        engine TEXT,
        model TEXT,
        duration_s REAL,
        ref_id INTEGER,
        update_key TEXT UNIQUE
    )
    """,
    "CREATE INDEX records_created ON records (created_utc, id)",
    """
    CREATE INDEX records_ok_asks ON records (created_utc, id)
    WHERE kind = 'ask' AND status = 'ok'
    """,
    """
    CREATE TABLE deliveries (
        record_id INTEGER PRIMARY KEY REFERENCES records (id),
        chunks TEXT NOT NULL,
        chunk_count INTEGER NOT NULL,
        next_index INTEGER NOT NULL DEFAULT 0 CHECK (next_index BETWEEN 0 AND chunk_count),
        failure_reason TEXT
    )
    """,
    """
    CREATE TABLE telegram_cursor (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        offset INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)

_MIGRATE_V1_TO_V2 = (
    """
    CREATE TABLE approvals (
        id TEXT PRIMARY KEY NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('note.create', 'brain.import')),
        owner TEXT NOT NULL,
        allowed_channel TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'executing', 'done', 'failed', 'rejected', 'expired')),
        payload TEXT,
        payload_hash TEXT,
        executor_pid INTEGER,
        executor_started_at TEXT,
        decision_update_key TEXT,
        CHECK (
            (payload IS NOT NULL AND payload_hash IS NOT NULL)
            OR (payload IS NULL AND payload_hash IS NULL AND status NOT IN ('pending', 'executing'))
        )
    )
    """,
    "CREATE INDEX approvals_status_created ON approvals (status, created_at, id)",
    """
    CREATE TRIGGER approvals_immutable_payload
    BEFORE UPDATE OF payload, payload_hash ON approvals
    WHEN (NEW.payload IS NOT OLD.payload OR NEW.payload_hash IS NOT OLD.payload_hash)
        AND (NEW.payload IS NOT NULL OR NEW.payload_hash IS NOT NULL)
    BEGIN
        SELECT RAISE(ABORT, 'approval payload and hash are immutable');
    END
    """,
)

_MIGRATIONS = (_SCHEMA_V1, _MIGRATE_V1_TO_V2)
_SCHEMA_VERSION = len(_MIGRATIONS)


def _utc_timestamp(value: str, name: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a UTC ISO 8601 timestamp")
    return timestamp


def _restrict_database_permissions(path: Path) -> None:
    path.chmod(0o600)
    for suffix in ("-wal", "-shm"):
        try:
            path.with_name(path.name + suffix).chmod(0o600)
        except FileNotFoundError:
            # SQLite may remove these files when the last connection closes.
            pass


def _read_record(row: sqlite3.Row) -> Record:
    fields = dict(row)
    del fields["created_utc"]
    fields["items"] = tuple(
        Item(text, tuple(sources)) for text, sources in json.loads(fields["items"])
    )
    return Record(**fields)


class Store:
    """Open a separate connection for each operation."""

    def __init__(self, path: Path):
        self._path = path

    @classmethod
    def open(cls, data_dir: Path) -> "Store":
        # Windows keeps its existing filesystem permissions.
        data_dir.mkdir(mode=0o700 if os.name == "posix" else 0o777, parents=True, exist_ok=True)
        store = cls(data_dir.resolve() / "javis.db")
        if os.name == "posix":
            store._path.parent.chmod(0o700)
        with store._connect() as db:
            if os.name == "posix":
                # SQLite creates the DB inside the private directory before WAL starts.
                _restrict_database_permissions(store._path)
            deadline = time.monotonic() + _BUSY_TIMEOUT
            while True:
                try:
                    mode = db.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                    break
                except sqlite3.OperationalError as exc:
                    # Two processes can race to switch a fresh database to WAL.
                    if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            if mode != "wal":
                raise sqlite3.OperationalError("worklog database requires WAL mode")

            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
            row = db.execute("SELECT version FROM schema_version").fetchone()
            version = row["version"] if row is not None else 0
            if row is not None and not 1 <= version <= _SCHEMA_VERSION:
                raise sqlite3.DatabaseError("unsupported worklog schema version")
            if row is None:
                db.execute("INSERT INTO schema_version VALUES (0)")
            for next_version in range(version + 1, _SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS[next_version - 1]:
                    db.execute(statement)
                db.execute("UPDATE schema_version SET version = ?", (next_version,))
        return store

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=_BUSY_TIMEOUT)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            yield db

    def save(self, record: Record, *, chunks: list[str] | None = None) -> int:
        """Save a record and any initial delivery together. The DB assigns the ID."""
        created_at = _utc_timestamp(record.created_at, "created_at")
        created_utc = created_at.astimezone(UTC).isoformat(timespec="microseconds")
        items = json.dumps([(item.text, item.sources) for item in record.items])
        with self._connect() as db:
            inserted = db.execute(
                """
                INSERT INTO records (
                    created_at, created_utc, channel, kind, request, items, status,
                    reason, engine, model, duration_s, ref_id, update_key
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (update_key) DO NOTHING
                RETURNING id
                """,
                (
                    record.created_at, created_utc, record.channel, record.kind,
                    record.request, items, record.status, record.reason, record.engine,
                    record.model, record.duration_s, record.ref_id, record.update_key,
                ),
            ).fetchone()
            if inserted is None:
                return db.execute(
                    "SELECT id FROM records WHERE update_key = ?", (record.update_key,)
                ).fetchone()["id"]
            record_id = inserted["id"]
            if chunks is not None:
                self._set_delivery(db, record_id, chunks)
            return record_id

    def create_approval(
        self,
        *,
        kind: ApprovalKind,
        owner: str,
        allowed_channel: str,
        created_at: str,
        expires_at: str,
        payload: str,
    ) -> Approval:
        """Store a pending approval and hash the exact UTF-8 execution payload."""
        created = _utc_timestamp(created_at, "created_at")
        expires = _utc_timestamp(expires_at, "expires_at")
        if expires <= created:
            raise ValueError("expires_at must be after created_at")
        with self._connect() as db:
            row = db.execute(
                """
                INSERT INTO approvals (
                    id, kind, owner, allowed_channel, created_at, expires_at, payload, payload_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING *
                """,
                (
                    secrets.token_hex(16), kind, owner, allowed_channel,
                    created.isoformat(timespec="microseconds"),
                    expires.isoformat(timespec="microseconds"),
                    payload, hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                ),
            ).fetchone()
        return Approval(**dict(row))

    def get_approval(self, id: str) -> Approval | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE id = ?", (id,)).fetchone()
        return Approval(**dict(row)) if row is not None else None

    def list_approvals(self, status: ApprovalStatus) -> list[Approval]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM approvals WHERE status = ? ORDER BY created_at, id", (status,)
            ).fetchall()
        return [Approval(**dict(row)) for row in rows]

    def get(self, id: int) -> Record | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM records WHERE id = ?", (id,)).fetchone()
        return _read_record(row) if row is not None else None

    def by_update_key(self, key: str) -> Record | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM records WHERE update_key = ?", (key,)).fetchone()
        return _read_record(row) if row is not None else None

    def recent(self, n: int) -> list[Record]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM records ORDER BY created_utc DESC, id DESC LIMIT ?", (max(n, 0),)
            ).fetchall()
        return [_read_record(row) for row in rows]

    def last_ok_ask(self) -> Record | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM records WHERE kind = 'ask' AND status = 'ok'
                ORDER BY created_utc DESC, id DESC LIMIT 1
                """
            ).fetchone()
        return _read_record(row) if row is not None else None

    def ok_asks_on(self, day: date, tz: tzinfo | None = None) -> list[Record]:
        # The local date can only differ from the UTC date by one day.
        # Convert each candidate so local-time rules apply at its own instant.
        lower = date.fromordinal(max(day.toordinal() - 1, date.min.toordinal()))
        upper = date.fromordinal(min(day.toordinal() + 1, date.max.toordinal()))
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM records WHERE kind = 'ask' AND status = 'ok'
                AND created_utc BETWEEN ? AND ?
                ORDER BY created_utc, id
                """,
                (f"{lower}T00:00:00.000000+00:00", f"{upper}T23:59:59.999999+00:00"),
            ).fetchall()
        records = []
        for row in rows:
            try:
                local_day = datetime.fromisoformat(row["created_utc"]).astimezone(tz).date()
            except OverflowError:
                continue  # The local date lies outside datetime's supported years.
            if local_day == day:
                records.append(_read_record(row))
        return records

    def set_delivery(self, id: int, chunks: list[str]) -> None:
        with self._connect() as db:
            self._set_delivery(db, id, chunks)

    @staticmethod
    def _set_delivery(db: sqlite3.Connection, id: int, chunks: list[str]) -> None:
        db.execute(
            """
            INSERT INTO deliveries (record_id, chunks, chunk_count) VALUES (?, ?, ?)
            ON CONFLICT (record_id) DO UPDATE SET chunks = excluded.chunks,
                chunk_count = excluded.chunk_count, next_index = 0, failure_reason = NULL
            """,
            (id, json.dumps(chunks), len(chunks)),
        )

    def pending_deliveries(self) -> list[tuple[int, list[str], int]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT record_id, chunks, next_index FROM deliveries
                WHERE failure_reason IS NULL AND next_index < chunk_count ORDER BY record_id
                """
            ).fetchall()
        return [(id, json.loads(chunks), next_index) for id, chunks, next_index in rows]

    def mark_sent(self, id: int, next_index: int, offset: int | None = None) -> None:
        with self._connect() as db:
            updated = db.execute(
                "UPDATE deliveries SET next_index = ? WHERE record_id = ?", (next_index, id)
            )
            if updated.rowcount == 0:
                raise KeyError(id)
            if offset is not None:
                self._set_offset(db, offset)

    def mark_failed(self, id: int, reason: str, *, offset: int | None = None) -> None:
        with self._connect() as db:
            updated = db.execute(
                "UPDATE deliveries SET failure_reason = ? WHERE record_id = ?", (reason, id)
            )
            if updated.rowcount == 0:
                raise KeyError(id)
            if offset is not None:
                self._set_offset(db, offset)

    def get_offset(self) -> tuple[int, str] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT offset, updated_at FROM telegram_cursor WHERE singleton = ?", (1,)
            ).fetchone()
        return (row["offset"], row["updated_at"]) if row is not None else None

    def set_offset(self, offset: int) -> None:
        with self._connect() as db:
            self._set_offset(db, offset)

    @staticmethod
    def _set_offset(db: sqlite3.Connection, offset: int) -> None:
        db.execute(
            """
            INSERT INTO telegram_cursor VALUES (?, ?, ?)
            ON CONFLICT (singleton) DO UPDATE SET offset = excluded.offset,
                updated_at = excluded.updated_at
            """,
            (1, offset, datetime.now(UTC).isoformat()),
        )
