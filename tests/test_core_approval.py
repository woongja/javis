import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from core import approval
from core.approval import ApprovalService
from worklog import Decision, Store


NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)
DEADLINE = NOW + timedelta(hours=24)


@pytest.fixture
def service(tmp_path):
    return ApprovalService(Store.open(tmp_path))


@pytest.mark.parametrize(("kind", "payload"), [
    ("unsupported", "draft"), ("note.create", " \n"), ("note.create", {}), ("brain.import", []),
])
def test_propose_rejects_invalid_arguments(service, kind, payload):
    with pytest.raises(ValueError):
        service.propose(kind, payload, "owner", "cli", NOW)


def test_propose_snapshots_import_payload(service):
    manifest = {"files": ["draft.md"]}
    proposal = service.propose("brain.import", manifest, "owner", "cli", NOW)
    manifest["files"].append("other.md")
    decision = service.decide(proposal.id, "owner", "cli", True, NOW)
    assert decision.status == "claimed"
    assert json.loads(decision.approval.payload) == {"files": ["draft.md"]}


def test_telegram_note_proposal_rolls_back_and_replays_saved_delivery(tmp_path, service):
    store = Store.open(tmp_path)
    text = " private draft\nwith original whitespace "
    key = "42:101"
    with store._connect() as db:
        db.execute("""CREATE TRIGGER fail_delivery BEFORE INSERT ON deliveries
                    BEGIN SELECT RAISE(ABORT, 'delivery failed'); END""")

    def propose(approval_service, reply_chunks):
        return approval_service.propose_telegram_note(text, "chat", 37, key, NOW, reply_chunks)

    with pytest.raises(sqlite3.IntegrityError, match="delivery failed"):
        propose(service, lambda reply: [reply])
    assert store.list_approvals("pending", now=NOW) == []
    assert store.recent(10) == store.pending_deliveries() == []

    with store._connect() as db:
        db.execute("DROP TRIGGER fail_delivery")
    proposal = propose(service, lambda reply: reply.splitlines())
    assert proposal.payload == text
    assert store.telegram_pending_notes("chat", NOW) == [(37, proposal.expires_at, proposal.id)]
    row = store.by_update_key(key)
    assert (row.kind, row.reason, row.request, row.items) == ("approval", "proposed", "", ())
    assert store.pending_deliveries() == [(row.id, [
        "메시지 37 · note.create · 1건", f"만료 {proposal.expires_at}", f"승인 ID {proposal.id}",
    ], 0)]
    store.mark_sent(row.id, 1)
    reopened = Store.open(tmp_path)
    assert propose(ApprovalService(reopened), lambda _: pytest.fail("reply regenerated")) is None
    assert reopened.list_approvals("pending", now=NOW) == [proposal]
    assert reopened.recent(10) == [row]
    assert reopened.pending_deliveries() == store.pending_deliveries()
    assert reopened.pending_deliveries()[0][2] == 1


@pytest.mark.parametrize("approve", [True, False])
def test_telegram_decision_rolls_back_when_delivery_save_fails(tmp_path, approve):
    store = Store.open(tmp_path)
    service = ApprovalService(store, {"note.create": lambda _: pytest.fail("executed before commit")})
    proposal = service.propose("note.create", "draft", "chat", "telegram", NOW)
    with store._connect() as db:
        db.execute("""CREATE TRIGGER fail_delivery BEFORE INSERT ON deliveries
                    BEGIN SELECT RAISE(ABORT, 'delivery failed'); END""")

    with pytest.raises(sqlite3.IntegrityError, match="delivery failed"):
        service.decide(proposal.id, "chat", "telegram", approve, NOW, "42:101", delivery=lambda status: [status])
    assert store.get_approval(proposal.id) == proposal
    assert store.recent(10) == store.pending_deliveries() == []

    with store._connect() as db:
        db.execute("DROP TRIGGER fail_delivery")
    result = service.decide(proposal.id, "chat", "telegram", approve, NOW, "42:101", delivery=lambda status: [status])
    assert result.status == ("claimed" if approve else "rejected")
    other = service.propose("note.create", "other draft", "chat", "telegram", NOW)
    replay = service.decide(
        other.id, "chat", "telegram", False, NOW, "42:101", delivery=lambda _: pytest.fail("reply regenerated"),
    )
    assert replay == Decision("duplicate")
    assert store.get_approval(other.id) == other
    assert len(store.pending_deliveries()) == 1


def test_decision_at_24_hour_boundary(tmp_path, service):
    early = service.propose("note.create", "early draft", "owner", "cli", NOW)
    late = service.propose("note.create", "late draft", "owner", "cli", NOW)
    assert datetime.fromisoformat(late.expires_at) == DEADLINE
    assert service.decide(
        early.id, "owner", "cli", True, DEADLINE - timedelta(microseconds=1),
    ).status == "claimed"
    assert service.decide(late.id, "owner", "cli", True, DEADLINE) == Decision("expired")
    assert service.decide(late.id, "owner", "cli", False, DEADLINE) == Decision("expired")
    expired = Store.open(tmp_path).get_approval(late.id)
    assert expired.status == "expired"
    assert expired.payload is expired.payload_hash is None


@pytest.mark.parametrize(("actor", "channel"), [("other", "cli"), ("owner", "telegram")])
def test_unauthorized_decision_leaves_state_unchanged(tmp_path, service, actor, channel):
    proposal = service.propose("note.create", "draft", "owner", "cli", NOW)
    assert service.decide(proposal.id, actor, channel, True, NOW) == Decision("forbidden")
    assert service.decide(proposal.id, actor, channel, False, DEADLINE) == Decision("forbidden")
    assert Store.open(tmp_path).get_approval(proposal.id) == proposal
    assert service.decide("missing", actor, channel, True, NOW) == Decision("not_found")


@pytest.mark.parametrize("approve", [True, False])
def test_inconsistent_stored_hash_blocks_decision(tmp_path, service, approve):
    proposal = service.propose("note.create", "draft", "owner", "cli", NOW)
    with closing(sqlite3.connect(tmp_path / "javis.db")) as db, db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE approvals SET payload_hash = 'wrong' WHERE id = ?", (proposal.id,))
        # Seed an inconsistent row directly to exercise the independent verification.
        db.execute(
            """
            INSERT INTO approvals (id, kind, owner, allowed_channel, created_at, expires_at, payload, payload_hash)
            SELECT 'inconsistent', kind, owner, allowed_channel, created_at, expires_at, payload, 'wrong'
            FROM approvals WHERE id = ?
            """,
            (proposal.id,),
        )
    assert service.decide("inconsistent", "owner", "cli", approve, NOW) == Decision("forbidden")
    assert Store.open(tmp_path).get_approval("inconsistent").status == "pending"


def test_replayed_approval_dispatches_stored_payload_once(tmp_path):
    store = Store.open(tmp_path)
    executed = []

    def executor(claim):
        assert store.get_approval(claim.id) == claim
        executed.append(claim.payload)
        return True

    service = ApprovalService(store, {"note.create": executor})
    proposal = service.propose("note.create", " draft\n", "owner", "telegram", NOW)
    results = []
    for _ in range(2):
        result = service.decide(proposal.id, "owner", "telegram", True, NOW, update_key="42:1795219200")
        results.append(result.status)
        if result.approval is not None:
            assert result.approval.decision_update_key == "42:1795219200"
            assert result.approval.executor_pid == os.getpid()
            assert result.approval.executor_started_at == approval.process_started_at(os.getpid())
            assert service.finish(proposal.id, service.executors[result.approval.kind](result.approval))
    assert results == ["claimed", "already_decided"]
    assert executed == [" draft\n"]


def test_rejection_records_decision_and_clears_payload(tmp_path, service):
    proposal = service.propose("note.create", "draft", "owner", "cli", NOW)
    assert service.decide(proposal.id, "owner", "cli", False, NOW, "decision") == Decision("rejected")
    assert service.decide(proposal.id, "owner", "cli", True, NOW) == Decision("already_decided")
    assert not service.finish(proposal.id, True)
    row = Store.open(tmp_path).get_approval(proposal.id)
    assert row.status == "rejected"
    assert row.decision_update_key == "decision"
    assert row.executor_pid is row.executor_started_at is None
    assert row.payload is row.payload_hash is None


def test_concurrent_approve_and_reject_have_one_winner(tmp_path, service):
    proposal = service.propose("note.create", "draft", "owner", "cli", NOW)
    barrier = Barrier(2)

    def decide(approve):
        worker = ApprovalService(Store.open(tmp_path))
        barrier.wait(timeout=10)
        return worker.decide(proposal.id, "owner", "cli", approve, NOW, update_key=str(approve))

    with ThreadPoolExecutor(max_workers=2) as workers:
        approval_result, rejection_result = workers.map(decide, [True, False])
    row = Store.open(tmp_path).get_approval(proposal.id)
    if approval_result.status == "claimed":
        assert rejection_result == Decision("already_decided")
        assert row == approval_result.approval
        assert row.decision_update_key == "True"
    else:
        assert approval_result == Decision("already_decided")
        assert rejection_result == Decision("rejected")
        assert row.status == "rejected"
        assert row.decision_update_key == "False"
        assert row.payload is row.payload_hash is None


@pytest.mark.parametrize(("ok", "status"), [(True, "done"), (False, "failed")])
def test_only_claiming_process_can_finish_and_clear_payload(tmp_path, service, monkeypatch, ok, status):
    proposal = service.propose("note.create", "draft", "owner", "cli", NOW)
    assert not service.finish(proposal.id, ok)
    claim = service.decide(proposal.id, "owner", "cli", True, NOW).approval
    store = Store.open(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(approval, "_executor_identity", lambda: (claim.executor_pid + 1, claim.executor_started_at))
        assert not service.finish(proposal.id, ok)
        patch.setattr(approval, "_executor_identity", lambda: (claim.executor_pid, "different start time"))
        assert not service.finish(proposal.id, ok)
        assert store.get_approval(proposal.id) == claim

    assert service.finish(proposal.id, ok)
    assert not service.finish(proposal.id, not ok)
    final = store.get_approval(proposal.id)
    assert final.status == status
    assert final.payload is final.payload_hash is None
