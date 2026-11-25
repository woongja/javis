import hashlib
import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import cli
from worklog import Item, Record, Store
from worklog.store import _MIGRATIONS, _SCHEMA_VERSION


SAMPLE = Record(
    id=None,
    created_at="2026-11-21T00:00:00Z",
    channel="cli",
    kind="ask",
    request="What did the release change?",
    items=(
        Item("Summary", ("https://example.org/release", "https://example.org/notes")),
        Item("Follow-up", ()),
    ),
    status="ok",
    reason=None,
    engine="example",
    model="example-model",
    duration_s=1.25,
    ref_id=None,
    update_key=None,
)

READER = """
import sys
from dataclasses import replace
from pathlib import Path
from tests.test_worklog_store import SAMPLE
from worklog import Store

record_id = int(sys.argv[2])
store = Store.open(Path(sys.argv[1]))
assert store.get(record_id) == replace(SAMPLE, id=record_id)
"""

WRITER = """
import sys
from dataclasses import replace
from pathlib import Path
from tests.test_worklog_store import SAMPLE
from worklog import Store

sys.stdin.readline()
store = Store.open(Path(sys.argv[1]))
writer = int(sys.argv[2])
for number in range(50):
    store.save(replace(SAMPLE, request=f"{writer}/{number}"))
"""


def test_round_trip_across_processes(tmp_path):
    data_dir = tmp_path / "nested" / "data"
    store = Store.open(data_dir)
    assert store.recent(10) == []
    assert store.get(1) is None
    assert store.last_ok_ask() is None
    assert store.get_offset() is None

    record_id = store.save(SAMPLE)
    assert store.get(record_id) == replace(SAMPLE, id=record_id)
    subprocess.run(
        [sys.executable, "-c", READER, str(data_dir), str(record_id)],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        timeout=15,
    )


def test_two_concurrent_processes_each_save_fifty_records(tmp_path):
    data_dir = tmp_path / "shared"
    processes = []
    try:
        for writer in range(2):
            processes.append(subprocess.Popen(
                [sys.executable, "-c", WRITER, str(data_dir), str(writer)],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.PIPE,
            ))
        for process in processes:
            process.stdin.write(b"start\n")
            process.stdin.close()
        for process in processes:
            assert process.wait(timeout=30) == 0
    finally:
        for process in processes:
            if process.stdin is not None:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
            process.wait()

    records = Store.open(data_dir).recent(101)
    assert len(records) == 100
    assert len({record.id for record in records}) == 100
    assert {record.request for record in records} == {
        f"{writer}/{number}" for writer in range(2) for number in range(50)
    }
    for record in records:
        assert record == replace(SAMPLE, id=record.id, request=record.request)


def test_update_key_deduplicates_without_replacing_record(tmp_path):
    store = Store.open(tmp_path)
    first = replace(SAMPLE, channel="telegram", update_key="42:1795219200")
    record_id = store.save(first, chunks=["first reply"])
    store.mark_sent(record_id, 1)
    assert store.save(replace(first, request="duplicate"), chunks=["new reply"]) == record_id
    assert store.recent(10) == [replace(first, id=record_id)]
    assert store.by_update_key(first.update_key) == replace(first, id=record_id)
    assert store.pending_deliveries() == []

    later = replace(first, update_key="42:1795910400")
    later_id = store.save(later)
    assert later_id != record_id
    assert store.by_update_key(later.update_key) == replace(later, id=later_id)
    assert store.by_update_key("42") is None
    assert [record.id for record in store.recent(2)] == [later_id, record_id]


def test_successful_ask_queries_use_timestamp_order_and_local_dates(tmp_path):
    store = Store.open(tmp_path)
    utc_plus_nine = timezone(timedelta(hours=9))
    day = date(2026, 11, 22)
    end = store.save(replace(SAMPLE, created_at="2026-11-22T14:59:59.999999Z"))
    before = store.save(replace(SAMPLE, created_at="2026-11-21T14:59:59.999999Z"))
    start = store.save(replace(SAMPLE, created_at="2026-11-21T15:00:00+00:00"))
    same_second = store.save(replace(SAMPLE, created_at="2026-11-21T15:00:00.1Z"))
    recall = store.save(replace(
        SAMPLE, created_at="2026-11-22T14:59:59.999999Z", kind="recall",
        engine=None, model=None, duration_s=None, ref_id=end,
    ))
    failed = store.save(replace(
        SAMPLE, created_at="2026-11-22T14:59:59.999999Z", status="timeout",
        reason="timeout", items=(),
    ))
    assert store.last_ok_ask().id == end
    assert store.get(recall).ref_id == end
    assert store.get(failed).items == ()
    assert [record.id for record in store.ok_asks_on(day, utc_plus_nine)] == [
        start, same_second, end,
    ]

    after = store.save(replace(SAMPLE, created_at="2026-11-22T15:00:00Z"))
    assert [record.id for record in store.ok_asks_on(day, utc_plus_nine)] == [
        start, same_second, end,
    ]
    assert [record.id for record in store.recent(7)] == [
        after, failed, recall, end, same_second, start, before,
    ]


def test_delivery_progress_and_cursor_commit_together(tmp_path):
    store = Store.open(tmp_path)
    record_id = store.save(SAMPLE)
    chunks = ["first", "second"]
    store.set_delivery(record_id, chunks)
    store.set_offset(20)
    assert store.pending_deliveries() == [(record_id, chunks, 0)]
    store.mark_sent(record_id, 1)
    assert store.pending_deliveries() == [(record_id, chunks, 1)]
    original_cursor = store.get_offset()
    assert original_cursor[0] == 20

    with pytest.raises(OverflowError):
        store.mark_sent(record_id, 2, offset=2**63)
    assert store.get_offset() == original_cursor
    assert store.pending_deliveries() == [(record_id, chunks, 1)]

    store.mark_sent(record_id, 2, offset=21)
    reopened = Store.open(tmp_path)
    assert reopened.pending_deliveries() == []
    offset, updated_at = reopened.get_offset()
    assert offset == 21
    assert datetime.fromisoformat(updated_at).tzinfo == UTC


def test_delivery_save_and_permanent_failure_are_atomic(tmp_path):
    store = Store.open(tmp_path)
    with pytest.raises(TypeError):
        store.save(SAMPLE, chunks=[object()])
    assert store.recent(10) == []

    record_id = store.save(SAMPLE, chunks=["reply"])
    assert store.pending_deliveries() == [(record_id, ["reply"], 0)]
    with pytest.raises(OverflowError):
        store.mark_failed(record_id, "forbidden", offset=2**63)
    assert store.pending_deliveries() == [(record_id, ["reply"], 0)]

    store.mark_failed(record_id, "forbidden", offset=10)
    assert store.pending_deliveries() == []
    assert store.get_offset()[0] == 10
    with closing(sqlite3.connect(tmp_path / "javis.db")) as db:
        assert db.execute(
            "SELECT failure_reason FROM deliveries WHERE record_id = ?", (record_id,)
        ).fetchone() == ("forbidden",)


def test_sensitive_records_keep_only_cited_sources_out_of_fts(tmp_path):
    store = Store.open(tmp_path)
    web_id = store.save(SAMPLE)
    paths = ["notes/second.md", "notes/first.md"]
    marker = "quotednotebodymarker"
    answer = replace(
        SAMPLE, request=f"question {marker}", items=(Item(f"verbatim excerpt {marker}", (marker,)),),
        reason=marker, engine=marker, model=marker, ref_id=web_id,
    )
    db_path = tmp_path / "javis.db"
    with closing(sqlite3.connect(db_path)) as db:
        # Keep a connection open so the committed WAL frames can be inspected.
        db.execute("SELECT count(*) FROM records").fetchone()
        context = store.save(replace(answer, kind="context"), cited_sources=[paths[0], web_id, paths[1], paths[0]])
        private = store.save(replace(answer, kind="private"), cited_sources=["notes/privateonly.md", web_id])
        uncited = store.save(replace(answer, kind="context"))
        for id, kind in [(context, "context"), (private, "private"), (uncited, "context")]:
            assert store.get(id) == replace(
                answer, id=id, kind=kind, request="", items=(), reason=None, engine=None, model=None, ref_id=None,
            )
        assert store.cited_sources(context) == [paths[0], web_id, paths[1]]
        assert store.cited_paths(context) == paths
        assert store.cited_sources(private) == store.cited_sources(uncited) == []
        for rebuild in (False, True):
            if rebuild:
                with db:
                    db.execute("UPDATE search_index SET tokenizer = NULL")
                store = Store.open(tmp_path)
            assert db.execute("SELECT rowid FROM search_trigram").fetchall() == [(web_id,)]
            for term in (marker, "second", "privateonly"):
                assert store.search(term) == ("ok", [])
                assert store.context_excerpts((term,)) == []
            # PASSIVE checkpoints committed pages without truncating the WAL.
            db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            for file in (db_path, tmp_path / "javis.db-wal"):
                data = file.read_bytes()
                assert data and marker.encode() not in data and b"privateonly" not in data


def test_citations_roll_back_and_replays_preserve_the_original(tmp_path):
    store = Store.open(tmp_path)
    record = replace(SAMPLE, kind="context", update_key="citation-test")
    path = "notes/used.md"
    with pytest.raises(ValueError, match="context record"):
        store.save(SAMPLE, cited_sources=[path])
    for invalid in ("../outside.md", "/absolute.md", "N1", 99):
        with pytest.raises(ValueError):
            store.save(record, cited_sources=[path, invalid])
        assert store.by_update_key(record.update_key) is None
    with pytest.raises(sqlite3.IntegrityError, match="cannot be queued"):
        store.save(record, cited_sources=[path], chunks=["private reply"])
    assert store.recent(1) == []
    id = store.save(record, cited_sources=[path])
    assert store.save(record, cited_sources=["notes/replayed.md"]) == id
    assert store.cited_paths(id) == [path]
    with pytest.raises(sqlite3.IntegrityError, match="cannot be queued"):
        store.set_delivery(id, ["private reply"])
    assert store.pending_deliveries() == []


def test_forget_removes_context_private_and_their_citations(tmp_path, monkeypatch):
    monkeypatch.setenv("JAVIS_DATA_DIR", str(tmp_path))
    store = Store.open(tmp_path)
    web_id = store.save(SAMPLE)
    path = "notes/used.md"
    context = store.save(replace(SAMPLE, kind="context"), cited_sources=[web_id, path])
    private = store.save(replace(SAMPLE, kind="private"))
    assert cli.main(["forget", str(web_id), "--yes"]) == 0
    assert store.cited_sources(context) == [path]
    assert cli.main(["forget", str(context), str(private), "--yes"]) == 0
    assert store.get(context) is store.get(private) is None
    assert store.cited_paths(context) == []


@pytest.fixture
def approval_fields():
    return {
        "kind": "note.create",
        "owner": "example-owner",
        "allowed_channel": "cli",
        "created_at": "2026-11-22T12:00:00Z",
        "expires_at": "2026-11-23T12:00:00Z",
        "payload": "Private draft 초안",
    }


def test_approval_round_trip_is_private(tmp_path, approval_fields):
    store = Store.open(tmp_path)
    first = store.create_approval(**approval_fields)
    second = store.create_approval(**(approval_fields | {
        "kind": "brain.import",
        "created_at": "2026-11-22T13:00:00+00:00",
    }))
    assert first.id != second.id
    assert len(bytes.fromhex(first.id)) == len(bytes.fromhex(second.id)) == 16
    assert first.kind == approval_fields["kind"]
    assert first.owner == approval_fields["owner"]
    assert first.allowed_channel == approval_fields["allowed_channel"]
    assert first.created_at == "2026-11-22T12:00:00.000000+00:00"
    assert first.expires_at == "2026-11-23T12:00:00.000000+00:00"
    assert first.status == "pending"
    assert first.payload == approval_fields["payload"]
    assert first.payload_hash == hashlib.sha256(approval_fields["payload"].encode()).hexdigest()
    assert first.executor_pid is first.executor_started_at is first.decision_update_key is None

    reopened = Store.open(tmp_path)
    assert reopened.get_approval(first.id) == first
    assert reopened.get_approval("missing") is None
    now = datetime.fromisoformat(second.created_at)
    assert reopened.list_approvals("pending", now=now) == [first, second]
    assert reopened.list_approvals("done", now=now) == []
    assert reopened.recent(10) == []
    assert reopened.pending_deliveries() == []


def test_approval_constraints(tmp_path, approval_fields):
    store = Store.open(tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        store.create_approval(**(approval_fields | {"kind": "ask"}))
    approval = store.create_approval(**approval_fields)
    with closing(sqlite3.connect(tmp_path / "javis.db")) as db, db:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            db.execute("UPDATE approvals SET status = 'unknown' WHERE id = ?", (approval.id,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE approvals SET payload = 'changed' WHERE id = ?", (approval.id,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE approvals SET payload_hash = 'changed' WHERE id = ?", (approval.id,))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            db.execute(
                "UPDATE approvals SET payload = NULL, payload_hash = NULL WHERE id = ?",
                (approval.id,),
            )
    assert store.get_approval(approval.id) == approval


def test_listing_expires_only_due_pending_approvals(tmp_path, approval_fields):
    store = Store.open(tmp_path)
    due = store.create_approval(**approval_fields)
    later = store.create_approval(**(approval_fields | {"expires_at": "2026-11-23T12:00:01Z"}))
    executing = store.create_approval(**approval_fields)
    deadline = datetime.fromisoformat(due.expires_at)
    assert store.decide_approval(
        executing.id, actor=executing.owner, channel=executing.allowed_channel,
        approve=True, now=deadline - timedelta(microseconds=1), executor=(123, "start"),
    ).status == "claimed"

    assert store.list_approvals("pending", now=deadline) == [later]
    expired, = store.list_approvals("expired", now=deadline)
    assert expired.id == due.id
    assert expired.payload is expired.payload_hash is None
    still_executing, = store.list_approvals("executing", now=deadline)
    assert still_executing.id == executing.id
    assert still_executing.payload == executing.payload


def test_upgrade_v2_preserves_approval_history_and_partial_delivery(tmp_path, approval_fields):
    path = tmp_path / "javis.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO schema_version VALUES (2)")
        for migration in _MIGRATIONS[:2]:
            for statement in migration:
                db.execute(statement)
        db.execute(
            """INSERT INTO approvals (id, kind, owner, allowed_channel, created_at, expires_at, payload, payload_hash)
               VALUES ('legacy', :kind, :owner, :allowed_channel, :created_at, :expires_at, :payload, :payload_hash)""",
            approval_fields | {"payload_hash": hashlib.sha256(approval_fields["payload"].encode()).hexdigest()},
        )
    old = Store(path)
    record = replace(SAMPLE, channel="telegram", update_key="42:101")
    record_id = old.save(record, chunks=["first", "second"])
    old.mark_sent(record_id, 1)
    old.set_offset(42)
    offset = old.get_offset()

    upgraded = Store.open(tmp_path)
    assert upgraded.by_update_key("42:101") == replace(record, id=record_id)
    assert upgraded.pending_deliveries() == [(record_id, ["first", "second"], 1)]
    assert upgraded.get_offset() == offset
    assert upgraded.get_approval("legacy").payload == approval_fields["payload"]
    assert upgraded.telegram_pending_notes(approval_fields["owner"], datetime.fromisoformat(approval_fields["created_at"])) == []
    upgraded.save_telegram_approval_reply("43:101", datetime.fromisoformat(record.created_at), "pending", ["reply"])
    with upgraded._connect() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == _SCHEMA_VERSION
        row = db.execute("SELECT kind, reason FROM records WHERE update_key = '43:101'").fetchone()
        assert tuple(row) == ("approval", "pending")
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE deliveries SET record_id = -1 WHERE record_id = ?", (record_id,))


@pytest.mark.skipif(os.name != "posix", reason="POSIX filesystem modes")
def test_private_database_permissions(tmp_path):
    data_dir = tmp_path / "private"
    db_path = data_dir / "javis.db"

    Store.open(data_dir)
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    data_dir.chmod(0o755)
    db_path.chmod(0o644)
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("SELECT * FROM schema_version").fetchall()
        sidecars = [data_dir / "javis.db-wal", data_dir / "javis.db-shm"]
        for path in sidecars:
            path.chmod(0o644)
        Store.open(data_dir)
        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
        for path in [db_path, *sidecars]:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
