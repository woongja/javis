import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from brain.curate import Catalog, FileSnapshot
from core import approval
from core.approval import ApprovalService
from core.curate import build_prompt, build_proposal
from tests.test_curate_proposal import EXAMPLE, EXISTING, append, create
from worklog import CurateFileResult, Decision, Store


NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)
DEADLINE = NOW + timedelta(hours=24)


@pytest.fixture
def service(tmp_path):
    return ApprovalService(Store.open(tmp_path))


@pytest.fixture
def curate_payload(monkeypatch):
    monkeypatch.setattr(approval, "os_user", lambda: "owner")
    snapshot = FileSnapshot(EXAMPLE, (1, 3))
    catalog = Catalog((1, 2), None, "absent", (EXISTING,), True, {EXISTING: snapshot})
    start = EXAMPLE.index(b"- ", EXAMPLE.index(b"# "))
    prompt = build_prompt("new input", catalog, ranges=((EXISTING, start, EXAMPLE.index(b"\n", start)),))
    return build_proposal(prompt, {"ops": [create(), append(id="P2")]}).payload


def test_curate_requires_pc_owner_and_hides_guessed_ids(tmp_path, service, curate_payload):
    store = Store.open(tmp_path)
    for actor, channel in [("other", "cli"), ("owner", "telegram")]:
        with pytest.raises(ValueError):
            service.propose("brain.curate", curate_payload, actor, channel, NOW)
    proposal = service.propose("brain.curate", curate_payload, "owner", "cli", NOW)
    assert store.telegram_pending_notes("owner", NOW) == []
    for approve in (True, False):
        guessed = service.decide(proposal.id, "owner", "telegram", approve, NOW)
        assert guessed == service.decide("missing", "owner", "telegram", approve, NOW) == Decision("not_found")
        assert service.decide(proposal.id, "other", "cli", approve, NOW) == Decision("forbidden")
    assert store.get_approval(proposal.id) == proposal


def test_curate_rejects_invalid_file_plan_before_storing(tmp_path, service, curate_payload):
    spec = json.loads(curate_payload)
    create_op, append_op = spec["ops"]
    invalid_ops = [
        [create_op, create_op],
        [create_op | {"depends_on": ["P2"]}, append_op],
        [append_op | {"base_sha256": None}],
        [create_op | {"result_sha256": "bad"}],
        [create_op | {"path": f"{i}.md", "id": f"P{i}"} for i in range(1, 18)],
    ]
    invalid_payloads = [spec, curate_payload + " " * 65536]
    invalid_payloads.extend(json.dumps(spec | {"ops": ops}) for ops in invalid_ops)
    for payload in invalid_payloads:
        with pytest.raises(ValueError):
            service.propose("brain.curate", payload, "owner", "cli", NOW)
    assert Store.open(tmp_path).list_approvals("pending", now=NOW) == []


@pytest.mark.parametrize("approve", [True, False])
def test_curate_tamper_blocks_decision(tmp_path, service, curate_payload, approve):
    store = Store.open(tmp_path)
    proposal = service.propose("brain.curate", curate_payload, "owner", "cli", NOW)
    with store._connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE approvals SET payload_hash = 'wrong' WHERE id = ?", (proposal.id,))
        db.execute("DROP TRIGGER approvals_immutable_payload")
        db.execute("UPDATE approvals SET payload = payload || ' ' WHERE id = ?", (proposal.id,))
    assert service.decide(proposal.id, "owner", "cli", approve, NOW) == Decision("forbidden")
    assert store.get_approval(proposal.id).status == "pending"
    assert all(f.status is None for f in store.curate_file_results(proposal.id))


@pytest.mark.parametrize("action", ["approve", "reject", "list"])
def test_curate_rejection_and_expiry_clear_private_data(tmp_path, service, curate_payload, action):
    store = Store.open(tmp_path)
    proposal = service.propose("brain.curate", curate_payload, "owner", "cli", NOW)
    assert datetime.fromisoformat(proposal.expires_at) == DEADLINE
    if action == "list":
        assert store.list_approvals("pending", now=DEADLINE) == []
    elif action == "reject":
        assert service.decide(proposal.id, "owner", "cli", False, NOW) == Decision("rejected")
    else:
        assert service.decide(proposal.id, "owner", "cli", True, DEADLINE) == Decision("expired")
    row = store.get_approval(proposal.id)
    assert row.status == ("rejected" if action == "reject" else "expired")
    assert row.payload is row.payload_hash is None and store.curate_file_results(row.id) == []
    assert service.decide(row.id, "owner", "cli", True, DEADLINE).approval is None


def test_curate_concurrent_claim_dispatches_exact_payload_once(tmp_path, service, curate_payload):
    payload = " " + curate_payload + "\n"
    proposal = service.propose("brain.curate", payload, "owner", "cli", NOW)
    assert proposal.payload_hash == hashlib.sha256(payload.encode()).hexdigest()
    executed = []
    barrier = Barrier(2, timeout=10)

    def decide(_):
        store = Store.open(tmp_path)

        def executor(claim):
            assert store.get_approval(claim.id) == claim
            executed.append(claim.payload)
            return True

        worker = ApprovalService(store, {"brain.curate": executor})
        barrier.wait()
        result = worker.decide(proposal.id, "owner", "cli", True, DEADLINE - timedelta(microseconds=1))
        if result.approval:
            worker.execute(result.approval)
        return result.status

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(decide, range(2))) == ["already_decided", "claimed"]
    assert executed == [payload]


@pytest.mark.parametrize("outcome", ["done", "conflict", "failed", "dependency_incomplete", "unknown"])
def test_curate_file_journal_and_batch_verdict(tmp_path, service, curate_payload, outcome):
    store = Store.open(tmp_path)
    id = service.propose("brain.curate", curate_payload, "owner", "cli", NOW).id
    assert not service.record_curate_result(id, "P1", "done")
    claim = service.decide(id, "owner", "cli", True, NOW).approval
    create_op, append_op = json.loads(claim.payload)["ops"]
    assert store.curate_file_results(id) == [
        CurateFileResult("P1", None, create_op["result_sha256"]),
        CurateFileResult("P2", append_op["base_sha256"], append_op["result_sha256"]),
    ]
    assert not store.record_curate_result(id, "P1", "failed", executor=(claim.executor_pid, "wrong"))
    assert not service.record_curate_result(id, "P3", "failed")
    assert service.record_curate_result(id, "P1", "done")
    assert not service.record_curate_result(id, "P1", "failed")
    assert not service.finish(id, True)
    assert Store.open(tmp_path).curate_file_results(id)[0].status == "done"
    assert service.record_curate_result(id, "P2", outcome)
    assert service.finish(id, outcome != "done")  # The persisted file outcomes determine the verdict.
    final = store.get_approval(id)
    assert final.status == ("done" if outcome == "done" else "failed")
    assert final.payload is final.payload_hash is None
    assert store.curate_file_results(id) == [
        CurateFileResult("P1", None, None, "done"), CurateFileResult("P2", None, None, outcome),
    ]
    assert store.curate_result_counts(id) == ({"done": 2} if outcome == "done" else {"done": 1, outcome: 1})
    assert not service.record_curate_result(id, "P2", "done")
    assert service.decide(id, "owner", "cli", True, NOW) == Decision("already_decided")


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
        "승인하면 inbox에 새 노트 1개 생성",
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
