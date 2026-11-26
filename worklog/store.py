"""SQLite storage for request history, Telegram deliveries, and approvals."""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Literal

from brain.policy import relative_parts

from .search import RecordExcerpt, SearchIndex, SearchResult, SearchStatus


@dataclass(frozen=True)
class Item:
    text: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Record:
    id: int | None
    created_at: str
    channel: str
    kind: Literal["ask", "recall", "approval", "local", "notice", "context", "private"]
    request: str
    items: tuple[Item, ...]
    status: Literal["ok", "error", "timeout", "quota", "denied"]
    reason: str | None
    engine: str | None
    model: str | None
    duration_s: float | None
    ref_id: int | None
    update_key: str | None


ApprovalKind = Literal["note.create", "brain.import", "brain.curate"]
CurateFileStatus = Literal["done", "conflict", "failed", "dependency_incomplete", "unknown"]
ApprovalStatus = Literal["pending", "executing", "done", "failed", "rejected", "expired"]
DecisionStatus = Literal[
    "claimed", "rejected", "expired", "not_found", "forbidden", "already_decided",
    "pc_only", "executor_missing", "duplicate",
]


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

    def can_decide(self, actor: str, channel: str) -> bool:
        # The local CLI and the configured Telegram chat belong to the same private store.
        if channel == "cli" and self.allowed_channel == "telegram" and self.kind == "note.create":
            return True
        return (self.owner, self.allowed_channel) == (actor, channel)


@dataclass(frozen=True)
class Decision:
    status: DecisionStatus
    approval: Approval | None = None  # Present only for a committed claim.


@dataclass(frozen=True)
class CurateFileResult:
    op_id: str
    base_sha256: str | None  # NULL for an absence precondition.
    result_sha256: str | None
    status: CurateFileStatus | None = None


def _curate_files(payload: str) -> list[CurateFileResult]:
    """Extract the journal from the exact serialized v1 proposal, without reading the vault."""
    if not isinstance(payload, str) or len(payload.encode()) > 64 * 1024:
        raise ValueError("expected a serialized curate proposal of at most 64 KiB")
    spec = json.loads(payload)
    if (not isinstance(spec, dict) or type(spec.get("version")) is not int or spec["version"] != 1
            or not {"root_id", "policy_hash", "sources", "ops"} <= spec.keys()):
        raise ValueError("expected a version 1 curate proposal")
    ops = spec["ops"]
    if not isinstance(ops, list) or not 1 <= len(ops) <= 16:
        raise ValueError("expected 1 to 16 curate operations")
    files = []
    paths = set()
    ids = set()
    for op in ops:
        if not isinstance(op, dict):
            raise ValueError("expected a curate operation")
        op_id, path = op.get("id"), op.get("path")
        if (not isinstance(op_id, str) or not re.fullmatch(r"P[1-9][0-9]*", op_id) or op_id in ids
                or not isinstance(path, str) or not path or path in paths):
            raise ValueError("expected distinct curate operation IDs and paths")
        dependencies = op.get("depends_on")
        if (not isinstance(op.get("content"), str) or not isinstance(dependencies, list)
                or not all(isinstance(dep, str) and dep in ids for dep in dependencies)):
            raise ValueError("expected serialized content and earlier dependencies")
        digest = op.get("result_sha256")
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            raise ValueError("expected a result SHA-256")
        base = op.get("base_sha256")
        if op.get("kind") == "append":
            if not isinstance(base, str) or not re.fullmatch("[0-9a-f]{64}", base) or "file_id" not in op:
                raise ValueError("expected an append precondition")
        elif op.get("kind") != "create" or op.get("absent") is not True or base is not None:
            raise ValueError("expected a create precondition")
        files.append(CurateFileResult(op_id, base, digest))
        paths.add(path)
        ids.add(op_id)
    return files


@dataclass(frozen=True)
class ForgetTarget:
    id: int
    created_at: str | None
    kind: str | None
    preview: str | None = field(repr=False)
    update_key: str | None
    protected: bool


# Note proposal replies have no stored proposal key and share the approval's creation time and channel.
_FORGET_FIELDS = """
    records.created_at, records.kind, substr(records.request, 1, 40), records.update_key,
    EXISTS (
        SELECT 1 FROM approvals
        WHERE status IN ('pending', 'executing') AND (
            decision_update_key = records.update_key
            OR (
                approvals.kind = 'note.create' AND approvals.allowed_channel = records.channel
                AND records.kind = 'approval'
                AND records.reason = 'proposed' AND approvals.created_at = records.created_utc
            )
        )
    )
"""


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

_CREATE_APPROVALS = """
    CREATE TABLE {table} (
        id TEXT PRIMARY KEY NOT NULL,
        kind TEXT NOT NULL CHECK ({kind_check}),
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
    """
_APPROVAL_INDEX_AND_TRIGGER = (
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

_OLD_APPROVAL_KINDS = "kind IN ('note.create', 'brain.import')"
_MIGRATE_V1_TO_V2 = (
    _CREATE_APPROVALS.format(table="approvals", kind_check=_OLD_APPROVAL_KINDS),
    *_APPROVAL_INDEX_AND_TRIGGER,
)

_CREATE_RECORDS_NEW = """
    CREATE TABLE records_new (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        created_utc TEXT NOT NULL,
        channel TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ({record_kinds})),
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
"""
_REBUILD_RECORDS = (
    "INSERT INTO records_new SELECT * FROM records",
    # Rebuild the child table so foreign key enforcement can stay enabled.
    """
    CREATE TABLE deliveries_new (
        record_id INTEGER PRIMARY KEY REFERENCES records_new (id),
        chunks TEXT NOT NULL,
        chunk_count INTEGER NOT NULL,
        next_index INTEGER NOT NULL DEFAULT 0 CHECK (next_index BETWEEN 0 AND chunk_count),
        failure_reason TEXT
    )
    """,
    "INSERT INTO deliveries_new SELECT * FROM deliveries",
    "DROP TABLE deliveries",
    "DROP TABLE records",
    "ALTER TABLE records_new RENAME TO records",
    "ALTER TABLE deliveries_new RENAME TO deliveries",
    "CREATE INDEX records_created ON records (created_utc, id)",
    """
    CREATE INDEX records_ok_asks ON records (created_utc, id)
    WHERE kind = 'ask' AND status = 'ok'
    """,
)


_CREATE_TELEGRAM_PROPOSALS = """
    CREATE TABLE {table} (
        approval_id TEXT PRIMARY KEY NOT NULL REFERENCES {parent} (id),
        message_id INTEGER NOT NULL CHECK (message_id > 0)
    )
    """
_MIGRATE_V2_TO_V3 = (
    _CREATE_RECORDS_NEW.format(record_kinds="'ask', 'recall', 'approval'"),
    *_REBUILD_RECORDS,
    _CREATE_TELEGRAM_PROPOSALS.format(table="telegram_note_proposals", parent="approvals"),
)
_MIGRATE_V3_TO_V4 = (
    _CREATE_RECORDS_NEW.format(record_kinds="'ask', 'recall', 'approval', 'local', 'notice'"),
    *_REBUILD_RECORDS,
)
_MIGRATE_V4_TO_V5 = (
    """
    CREATE TABLE search_index (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        tokenizer TEXT CHECK (tokenizer IN ('trigram', 'unicode61'))
    )
    """,
    "INSERT INTO search_index VALUES (1, NULL)",
)
_MIGRATE_V5_TO_V6 = (
    _CREATE_RECORDS_NEW.format(record_kinds="'ask', 'recall', 'approval', 'local', 'notice', 'context', 'private'"),
    *_REBUILD_RECORDS,
    """
    CREATE TABLE record_citations (
        record_id INTEGER NOT NULL REFERENCES records (id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        note_path TEXT,
        cited_id INTEGER REFERENCES records (id) ON DELETE CASCADE,
        PRIMARY KEY (record_id, position),
        CHECK ((note_path IS NULL) != (cited_id IS NULL))
    )
    """,
    """
    CREATE TRIGGER deliveries_reject_sensitive BEFORE INSERT ON deliveries
    WHEN (SELECT kind FROM records WHERE id = NEW.record_id) IN ('context', 'private')
    BEGIN
        SELECT RAISE(ABORT, 'reply body cannot be queued for this record kind');
    END
    """,
)
_MIGRATE_V6_TO_V7 = (
    "CREATE TABLE telegram_question_attempts (update_key TEXT PRIMARY KEY NOT NULL, created_at TEXT NOT NULL)",
    """
    CREATE TRIGGER forget_telegram_question AFTER DELETE ON records
    WHEN OLD.update_key IS NOT NULL
    BEGIN
        DELETE FROM telegram_question_attempts WHERE update_key = OLD.update_key;
    END
    """,
)
_MIGRATE_V7_TO_V8 = (
    _CREATE_APPROVALS.format(table="approvals_new", kind_check=(
        _OLD_APPROVAL_KINDS + " OR (kind = 'brain.curate' AND allowed_channel = 'cli')"
    )),
    "INSERT INTO approvals_new SELECT * FROM approvals",
    _CREATE_TELEGRAM_PROPOSALS.format(table="telegram_note_proposals_new", parent="approvals_new"),
    "INSERT INTO telegram_note_proposals_new SELECT * FROM telegram_note_proposals",
    "DROP TABLE telegram_note_proposals",
    "DROP TABLE approvals",
    "ALTER TABLE approvals_new RENAME TO approvals",
    "ALTER TABLE telegram_note_proposals_new RENAME TO telegram_note_proposals",
    *_APPROVAL_INDEX_AND_TRIGGER,
    """
    CREATE TABLE curate_file_results (
        approval_id TEXT NOT NULL REFERENCES approvals (id),
        op_id TEXT NOT NULL,
        base_sha256 TEXT,
        result_sha256 TEXT,
        status TEXT CHECK (status IN ('done', 'conflict', 'failed', 'dependency_incomplete', 'unknown')),
        PRIMARY KEY (approval_id, op_id)
    )
    """,
    """
    CREATE TRIGGER curate_terminal AFTER UPDATE OF status ON approvals
    WHEN NEW.kind = 'brain.curate' AND NEW.status IN ('expired', 'rejected', 'done', 'failed')
    BEGIN
        DELETE FROM curate_file_results WHERE approval_id = NEW.id AND status IS NULL;
        UPDATE curate_file_results SET base_sha256 = NULL, result_sha256 = NULL WHERE approval_id = NEW.id;
    END
    """,
)
_MIGRATIONS = (
    _SCHEMA_V1, _MIGRATE_V1_TO_V2, _MIGRATE_V2_TO_V3, _MIGRATE_V3_TO_V4, _MIGRATE_V4_TO_V5,
    _MIGRATE_V5_TO_V6, _MIGRATE_V6_TO_V7, _MIGRATE_V7_TO_V8,
)
_SCHEMA_VERSION = len(_MIGRATIONS)


def _utc_timestamp(value: str, name: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a UTC ISO 8601 timestamp")
    return timestamp


def _approval_time(now: datetime | None) -> str:
    now = datetime.now(UTC) if now is None else now
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC).isoformat(timespec="microseconds")


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
        self._search_index: SearchIndex | None = None

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
            store._search_index = SearchIndex(db)
            store._search_index.synchronize(db)
        return store

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=_BUSY_TIMEOUT)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            yield db

    def save(
        self, record: Record, *, chunks: list[str] | None = None, cited_sources: Collection[str | int] = (),
    ) -> int:
        """Save atomically. For context answers pass only locally resolved, used paths/IDs in citation order."""
        with self._connect() as db:
            return self._save(db, record, chunks, cited_sources)

    def _save(
        self, db: sqlite3.Connection, record: Record, chunks: list[str] | None,
        cited_sources: Collection[str | int] = (),
    ) -> int:
        if record.kind in ("context", "private"):
            record = replace(record, request="", items=(), reason=None, engine=None, model=None, ref_id=None)
        elif cited_sources:
            raise ValueError("citations require a context record")
        created_at = _utc_timestamp(record.created_at, "created_at")
        created_utc = created_at.astimezone(UTC).isoformat(timespec="microseconds")
        items = json.dumps([(item.text, item.sources) for item in record.items])
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
        if record.kind == "context" and record.status == "ok":
            self._save_citations(db, record_id, cited_sources)
        if self._search_index is not None and record.kind == "ask" and record.status == "ok":
            self._search_index.add(db, record_id, record.request, (item.text for item in record.items))
        if chunks is not None:
            self._set_delivery(db, record_id, chunks)
        return record_id

    @staticmethod
    def _save_citations(db: sqlite3.Connection, id: int, sources: Collection[str | int]) -> None:
        seen = set()
        for position, source in enumerate(sources):
            if isinstance(source, str):
                source = "/".join(relative_parts(source))
                if not source.casefold().endswith(".md"):
                    raise ValueError("a citation requires a relative Markdown path")
                path, cited_id = source, None
            elif type(source) is int and 0 < source < 2**63:
                if not db.execute(
                    "SELECT 1 FROM records WHERE id = ? AND kind = 'ask' AND status = 'ok'", (source,)
                ).fetchone():
                    raise ValueError("a citation requires a successful web record")
                path, cited_id = None, source
            else:
                raise ValueError("invalid citation")
            if source not in seen:
                db.execute("INSERT INTO record_citations VALUES (?, ?, ?, ?)", (id, position, path, cited_id))
                seen.add(source)

    def cited_sources(self, id: int) -> list[str | int]:
        """Return cited note paths and public record IDs in their original order."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT coalesce(note_path, cited_id) FROM record_citations WHERE record_id = ? ORDER BY position",
                (id,),
            )
            return [row[0] for row in rows]

    def cited_paths(self, id: int) -> list[str]:
        return [source for source in self.cited_sources(id) if isinstance(source, str)]

    def save_telegram_approval_reply(
        self, key: str, now: datetime, outcome: str, chunks: list[str],
    ) -> None:
        with self._connect() as db:
            self._save_telegram_approval_reply(db, key, now, outcome, chunks)

    def _save_telegram_approval_reply(
        self, db: sqlite3.Connection, key: str, now: datetime | None, outcome: str, chunks: list[str],
    ) -> None:
        if not chunks:
            raise ValueError("an approval reply requires at least one delivery chunk")
        if outcome in {"proposed", "pending", "claimed", "rejected"}:
            status = "ok"
        else:
            status = "denied" if outcome == "forbidden" else "error"
        self._save(db, Record(
            id=None, created_at=_approval_time(now), channel="telegram", kind="approval",
            request="", items=(), status=status,
            reason=outcome, engine=None, model=None, duration_s=None, ref_id=None, update_key=key,
        ), chunks)

    @staticmethod
    def _has_update(db: sqlite3.Connection, key: str) -> bool:
        return db.execute("SELECT 1 FROM records WHERE update_key = ?", (key,)).fetchone() is not None

    def claim_telegram_question(self, key: str, now: datetime) -> bool:
        """Reserve the update before either context lookup or the first web call."""
        with self._connect() as db:
            return db.execute(
                """
                INSERT INTO telegram_question_attempts (update_key, created_at)
                SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM records WHERE update_key = ?)
                ON CONFLICT (update_key) DO NOTHING
                """,
                (key, _approval_time(now), key),
            ).rowcount == 1

    def save_telegram_question(
        self, record: Record, *, chunks: list[str] | None = None,
        cited_sources: Collection[str] = (), offset: int | None = None,
    ) -> int | None:
        """Commit the claimed result once, along with its cursor or resumable web chunks."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            claimed = db.execute(
                "SELECT 1 FROM telegram_question_attempts WHERE update_key = ?", (record.update_key,)
            ).fetchone()
            if claimed is None or self._has_update(db, record.update_key):
                return None
            record_id = self._save(db, record, chunks, cited_sources)
            if chunks is not None:
                # Public answers now belong to the durable chunk delivery worker.
                db.execute("DELETE FROM telegram_question_attempts WHERE update_key = ?", (record.update_key,))
            if offset is not None:
                self._set_offset(db, offset)
            return record_id

    def pending_telegram_questions(self) -> list[tuple[str, datetime]]:
        with self._connect() as db:
            return [(key, datetime.fromisoformat(created)) for key, created in db.execute(
                "SELECT update_key, created_at FROM telegram_question_attempts ORDER BY rowid"
            )]

    def finish_telegram_question(self, key: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM telegram_question_attempts WHERE update_key = ?", (key,))

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
        with self._connect() as db:
            return self._create_approval(db, kind, owner, allowed_channel, created_at, expires_at, payload)

    @staticmethod
    def _create_approval(
        db: sqlite3.Connection, kind: ApprovalKind, owner: str, allowed_channel: str,
        created_at: str, expires_at: str, payload: str,
    ) -> Approval:
        created = _utc_timestamp(created_at, "created_at")
        expires = _utc_timestamp(expires_at, "expires_at")
        if expires <= created:
            raise ValueError("expires_at must be after created_at")
        if kind == "brain.curate" and expires - created != timedelta(hours=24):
            raise ValueError("brain.curate expires after 24 hours")
        files = _curate_files(payload) if kind == "brain.curate" else []
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
        if files:
            db.executemany(
                """INSERT INTO curate_file_results (approval_id, op_id, base_sha256, result_sha256)
                   VALUES (?, ?, ?, ?)""",
                [(row["id"], f.op_id, f.base_sha256, f.result_sha256) for f in files],
            )
        return Approval(**dict(row))

    def create_telegram_note_proposal(
        self, *, owner: str, message_id: int, update_key: str, now: datetime, payload: str,
        delivery: Callable[[str, str], list[str]],
    ) -> Approval | None:
        """Commit the proposal and its delivery once per update. A replay uses the saved delivery."""
        if type(message_id) is not int or not 0 < message_id < 2**63 or not update_key:
            raise ValueError("a Telegram message ID and processing key are required")
        created = _approval_time(now)
        expires = (datetime.fromisoformat(created) + timedelta(hours=24)).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._has_update(db, update_key):
                return None
            approval = self._create_approval(db, "note.create", owner, "telegram", created, expires, payload)
            db.execute(
                "INSERT INTO telegram_note_proposals (approval_id, message_id) VALUES (?, ?)",
                (approval.id, message_id),
            )
            chunks = delivery(approval.id, approval.expires_at)
            self._save_telegram_approval_reply(db, update_key, now, "proposed", chunks)
            return approval

    def get_approval(self, id: str) -> Approval | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE id = ?", (id,)).fetchone()
        return Approval(**dict(row)) if row is not None else None

    def list_approvals(self, status: ApprovalStatus, *, now: datetime | None = None) -> list[Approval]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE approvals SET status = 'expired', payload = NULL, payload_hash = NULL
                WHERE status = 'pending' AND expires_at <= ?
                """,
                (_approval_time(now),),
            )
            rows = db.execute(
                "SELECT * FROM approvals WHERE status = ? ORDER BY created_at, id", (status,)
            ).fetchall()
        return [Approval(**dict(row)) for row in rows]

    def telegram_pending_notes(self, owner: str, now: datetime) -> list[tuple[int, str, str]]:
        """Return message ID, expiry, and approval ID for this chat's pending notes."""
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT message_id, expires_at, id FROM approvals
                JOIN telegram_note_proposals ON approval_id = id
                WHERE status = 'pending' AND allowed_channel = 'telegram' AND owner = ?
                    AND kind = 'note.create' AND expires_at > ?
                ORDER BY created_at, id
                """,
                (owner, _approval_time(now)),
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def decide_approval(
        self,
        id: str,
        *,
        actor: str,
        channel: str,
        approve: bool,
        now: datetime | None = None,
        update_key: str | None = None,
        executor: tuple[int, str] | None = None,
        available_kinds: Collection[ApprovalKind] | None = None,
        delivery: Callable[[DecisionStatus], list[str]] | None = None,
    ) -> Decision:
        if approve and executor is None:
            raise ValueError("approval requires an executor identity")
        if delivery is not None and (channel != "telegram" or not update_key):
            raise ValueError("a Telegram delivery requires a processing key")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if delivery is not None and update_key is not None and self._has_update(db, update_key):
                return Decision("duplicate")
            decision = self._decide_approval(
                db, id, actor, channel, approve, now, update_key, executor, available_kinds,
            )
            if delivery is not None and update_key is not None:
                self._save_telegram_approval_reply(db, update_key, now, decision.status, delivery(decision.status))
            return decision

    @staticmethod
    def _decide_approval(
        db: sqlite3.Connection, id: str, actor: str, channel: str, approve: bool,
        now: datetime | None, update_key: str | None, executor: tuple[int, str] | None,
        available_kinds: Collection[ApprovalKind] | None,
    ) -> Decision:
        row = db.execute(
            "SELECT * FROM approvals WHERE id = ? AND (kind != 'brain.curate' OR ? = 'cli')",
            (id, channel),
        ).fetchone()
        if row is None:
            return Decision("not_found")
        if channel == "telegram" and row["kind"] == "brain.import":
            return Decision("pc_only")
        approval = Approval(**dict(row))
        if not approval.can_decide(actor, channel):
            return Decision("forbidden")
        if row["status"] == "expired":
            return Decision("expired")
        if row["status"] != "pending":
            return Decision("already_decided")

        payload = row["payload"]
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest() if isinstance(payload, str) else None
        timestamp = _approval_time(now)
        if row["expires_at"] <= timestamp:
            updated = db.execute(
                """
                UPDATE approvals SET status = 'expired', payload = NULL, payload_hash = NULL,
                    decision_update_key = ?
                WHERE id = ? AND status = 'pending' AND expires_at <= ?
                """,
                (update_key, id, timestamp),
            )
            if updated.rowcount == 1:
                return Decision("expired")
            raise sqlite3.DatabaseError("approval expiration did not update one row")
        if digest is None or digest != row["payload_hash"]:
            return Decision("forbidden")
        if approve and available_kinds is not None and row["kind"] not in available_kinds:
            return Decision("executor_missing")

        pid, started_at = executor if approve else (None, None)
        updated = db.execute(
            """
            UPDATE approvals SET status = ?, executor_pid = ?, executor_started_at = ?,
                decision_update_key = ?, payload = CASE WHEN ? THEN payload END,
                payload_hash = CASE WHEN ? THEN payload_hash END
            WHERE id = ? AND status = 'pending' AND owner = ? AND allowed_channel = ?
                AND payload_hash = ? AND expires_at > ?
            RETURNING *
            """,
            (
                "executing" if approve else "rejected", pid, started_at, update_key,
                approve, approve, id, approval.owner, approval.allowed_channel, digest, timestamp,
            ),
        )
        rows = updated.fetchall()
        if updated.rowcount != 1:
            raise sqlite3.DatabaseError("approval decision did not update one row")
        return Decision("claimed", Approval(**dict(rows[0]))) if approve else Decision("rejected")

    def curate_file_results(self, id: str) -> list[CurateFileResult]:
        """Read the local journal in manifest order. Only statuses survive finalization."""
        with self._connect() as db:
            return [CurateFileResult(**dict(row)) for row in db.execute(
                """SELECT op_id, base_sha256, result_sha256, status FROM curate_file_results
                   WHERE approval_id = ? ORDER BY rowid""", (id,),
            )]

    def record_curate_result(
        self, id: str, op_id: str, status: CurateFileStatus, *, executor: tuple[int, str],
    ) -> bool:
        """Commit one verified outcome once, on behalf of the process holding the claim."""
        if status is None:
            raise ValueError("a file outcome is required")
        with self._connect() as db:
            return db.execute(
                """
                UPDATE curate_file_results SET status = ?
                WHERE approval_id = ? AND op_id = ? AND status IS NULL AND EXISTS (
                    SELECT 1 FROM approvals WHERE id = curate_file_results.approval_id
                    AND status = 'executing' AND executor_pid = ? AND executor_started_at = ?
                )
                """,
                (status, id, op_id, *executor),
            ).rowcount == 1

    def curate_result_counts(self, id: str) -> dict[CurateFileStatus, int]:
        """Return the local per-status totals, excluding files still awaiting an outcome."""
        with self._connect() as db:
            return dict(db.execute(
                """SELECT status, COUNT(*) FROM curate_file_results
                   WHERE approval_id = ? AND status IS NOT NULL GROUP BY status""", (id,),
            ))

    def finish_approval(
        self, id: str, *, ok: bool, executor: tuple[int, str], chunks: list[str] | None = None,
    ) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # A batch verdict comes from its journal. Keep unfinished claims recoverable.
            counts = dict(db.execute(
                """SELECT f.status, COUNT(*) FROM approvals a
                   LEFT JOIN curate_file_results f ON f.approval_id = a.id
                   WHERE a.id = ? AND a.kind = 'brain.curate' GROUP BY f.status""",
                (id,),
            ))
            if None in counts:
                return False
            if counts:
                ok = counts.keys() == {"done"}
            status = "done" if ok else "failed"
            row = db.execute(
                """
                UPDATE approvals SET status = ?, payload = NULL, payload_hash = NULL
                WHERE id = ? AND status = 'executing' AND executor_pid = ? AND executor_started_at = ?
                RETURNING decision_update_key
                """,
                (status, id, *executor),
            ).fetchone()
            if row is None:
                return False
            if chunks is not None:
                record = db.execute(
                    "UPDATE records SET status = ?, reason = ? WHERE update_key = ? RETURNING id",
                    ("ok" if ok else "error", status, row["decision_update_key"]),
                ).fetchone()
                if record is None:
                    raise sqlite3.DatabaseError("approval decision reply is missing")
                # Replace the committed acknowledgement before the poller drains the queue.
                db.execute(
                    """
                    UPDATE deliveries SET chunks = ?, chunk_count = ?
                    WHERE record_id = ? AND next_index = 0 AND failure_reason IS NULL
                    """,
                    (json.dumps(chunks), len(chunks), record["id"]),
                )
            return True

    def get(self, id: int) -> Record | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM records WHERE id = ?", (id,)).fetchone()
        return _read_record(row) if row is not None else None

    def forget_targets(
        self, ids: Collection[int], *, before: datetime | None = None,
    ) -> list[ForgetTarget]:
        with self._connect() as db:
            if before is None:
                return self._forget_by_id(db, ids)
            rows = db.execute(
                f"""
                SELECT records.id, {_FORGET_FIELDS} FROM records
                WHERE created_utc < ? ORDER BY created_utc, id
                """,
                (_approval_time(before),),
            )
            return [ForgetTarget(*row) for row in rows]

    @staticmethod
    def _forget_by_id(db: sqlite3.Connection, ids: Collection[int]) -> list[ForgetTarget]:
        rows = db.execute(
            f"""
            SELECT wanted.value, {_FORGET_FIELDS} FROM json_each(?) AS wanted
            LEFT JOIN records ON records.id = wanted.value ORDER BY wanted.key
            """,
            (json.dumps(list(dict.fromkeys(ids))),),
        )
        return [ForgetTarget(*row) for row in rows]

    def forget(self, targets: list[ForgetTarget]) -> tuple[int, list[ForgetTarget], bool, tuple[str, ...]]:
        """Return the deleted count, changed targets, WAL status, and unclean FTS tables."""
        confirmed = {
            target.id: target for target in targets if target.created_at is not None and not target.protected
        }
        with self._connect() as db:
            db.execute("PRAGMA secure_delete = ON")
            db.execute("BEGIN IMMEDIATE")
            deleted = []
            skipped = []
            for current in self._forget_by_id(db, confirmed):
                # IDs can be reused after deletion. Recheck the preview and approval link under the write lock.
                if current == confirmed[current.id]:
                    deleted.append(current.id)
                else:
                    skipped.append(current)
            unclean_indexes = ()
            if deleted:
                ids = json.dumps(deleted)
                unclean_indexes = SearchIndex.remove(db, ids)
                db.execute("DELETE FROM deliveries WHERE record_id IN (SELECT value FROM json_each(?))", (ids,))
                db.execute("UPDATE records SET ref_id = NULL WHERE ref_id IN (SELECT value FROM json_each(?))", (ids,))
                db.execute("DELETE FROM records WHERE id IN (SELECT value FROM json_each(?))", (ids,))

            # Commit the secure deletions, then TRUNCATE the WAL to remove its older page images.
            db.commit()
            try:
                busy = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            except sqlite3.Error:
                return len(deleted), skipped, False, unclean_indexes
            return len(deleted), skipped, busy == 0, unclean_indexes

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

    def search(self, query: str, limit: int = 20) -> tuple[SearchStatus, list[SearchResult]]:
        """Return newest matching web records. Degraded search matches whole tokens for longer terms."""
        if self._search_index is None:
            return ("unavailable" if query.split() else "usage"), []
        with self._connect() as db:
            return self._search_index.search(db, query, limit)

    def context_excerpts(self, terms: tuple[str, ...]) -> list[RecordExcerpt] | None:
        """Read matching public passages in one snapshot. None means full FTS is unavailable."""
        if self._search_index is None:
            return None
        with self._connect() as db:
            return self._search_index.context_excerpts(db, terms)

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
