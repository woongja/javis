"""Execute approved brain writes."""

import hashlib
import json
from collections import Counter
from contextlib import ExitStack
from datetime import UTC, datetime

from brain import _display, append, curate
from brain.curate import execute_creates, reconcile_file
from brain.importer import copy_manifest
from brain.policy import relative_parts
from brain.publish import pin_configured_root, publish
from worklog import Approval, Store

from .approval import ApprovalService, ExecutionResult, is_interrupted
from .config import data_dir
from .curate import _file_evidence


def note_path(approval: Approval) -> str:
    created = datetime.fromisoformat(approval.created_at).astimezone(UTC)
    return f"javis/inbox/{created:%Y%m%d-%H%M%S}-{approval.id[:8]}.md"


def create_note(claim: Approval) -> ExecutionResult:
    assert claim.status == "executing" and claim.payload is not None
    try:
        # Pin after the approval is claimed. The publisher rechecks the root identity.
        vault = pin_configured_root()
    except (OSError, ValueError):
        return ExecutionResult(False, "두뇌 폴더에 접근할 수 없습니다. JAVIS_BRAIN_DIR 설정을 확인하세요.")
    relative_path = note_path(claim)
    result = publish(vault, relative_path, claim.payload.encode("utf-8"))
    if result.status == "published":
        return ExecutionResult(True, relative_path)
    reason = result.reason
    if result.status == "conflict":
        reason = "같은 이름의 파일이 이미 있습니다."
    if reason is None:
        return ExecutionResult(False, f"메모 생성 실패 ({result.status})")
    return ExecutionResult(False, f"메모 생성 실패 ({result.status}) · {reason}")


def import_brain(claim: Approval) -> ExecutionResult:
    assert claim.status == "executing" and claim.payload is not None
    try:
        manifest = json.loads(claim.payload)
        counts, reason = copy_manifest(manifest)
    except (KeyError, TypeError, ValueError):
        return ExecutionResult(False, "가져오기 명세를 확인할 수 없습니다.")
    summary = " · ".join(f"{status} {counts[status]}" for status in ("published", "conflict", "changed", "failed"))
    message = f"{_display(manifest['dest'])} · {summary}"
    if counts["published"]:
        return ExecutionResult(True, message)
    return ExecutionResult(False, f"{message} · {reason or '게시된 파일이 없습니다.'}")


NOTE_EXECUTORS = {"note.create": create_note, "brain.import": import_brain}

_FILE_LABELS = {
    "done": "완료", "conflict": "충돌", "failed": "실패", "dependency_incomplete": "선행 미완료",
    "unapplied": "미적용", "unknown": "결과 미상",
}
_RECOVERY_HINT = "같은 ID로 승인하면 대조 결과를 마감합니다."


def _curate_report(ops, outcomes, disconnected=()) -> ExecutionResult:
    counts = Counter(outcomes.values())
    lines = [" · ".join(f"{label} {counts[key]}" for key, label in _FILE_LABELS.items()
                        if counts[key] or key in ("done", "conflict", "failed", "dependency_incomplete"))]
    for op in ops:
        status = outcomes[op["id"]]
        label = _FILE_LABELS[status]
        if op["id"] in disconnected:
            label += ": 추가 기능 미연결"
        lines.append(f"{_display(op['path'])} · {label}")
    return ExecutionResult(counts["done"] == len(ops), "\n".join(lines))


def _append_base(vault, op, spec, catalog):
    file = curate._target_snapshot(vault, op["path"])
    if file is None or list(file.identity) != op["file_id"]:
        raise append.Conflict("target replaced")
    evidence = set()
    if op["path"].startswith(curate.NOTES) and not op.get("sources"):
        raise ValueError("missing evidence for managed note")
    for key in op.get("sources", []):
        pin = spec["sources"][key]
        if "path" in pin:
            item, _ = _file_evidence(catalog, pin["path"], *pin["byte_range"])
            evidence.update(item.derived)
    append.verified_append(file.content, op, evidence, policy=catalog.policy)


def _execute_append(vault, claim, op, spec, approved, parents, pins):
    with ExitStack() as stack:
        root = curate._checked_root(stack, vault)
        stack.enter_context(curate._vault_lock(root))
        current = curate._catalog(vault, {p["path"] for p in pins.values()}, allow_missing=True)
        if not current.complete or current.policy_hash != spec["policy_hash"]:
            raise ValueError("policy or inventory changed")
        if curate._parent_ids(root, curate._wiki_parts(op["path"])) != parents:
            raise ValueError("parent changed")
        _append_base(vault, op, spec, current)
        curate._check_sources(pins, current, approved)
        reply = append.exchange(data_dir(), vault, claim.id, op)
        if reply in ("conflict", "rejected"):
            return "conflict" if reply == "conflict" else "failed"
        # The reply can be lost after Vault.process commits. Never resend.
        result = reconcile_file(vault, op, op["base_sha256"], op["result_sha256"])
        return "done" if result == "done" else "unknown"


def _mixed_files(claim, spec, disconnected):
    ops = spec["ops"]
    prepared = {}
    outcomes = {}
    try:
        vault = pin_configured_root()
        if spec["root_id"] != [vault.device, vault.inode]:
            raise ValueError("approved vault changed")
        with ExitStack() as stack:
            root = curate._checked_root(stack, vault)
            stack.enter_context(curate._vault_lock(root))
            approved = curate._catalog(vault, curate._source_paths(spec["sources"]), allow_missing=True)
            curate._validate_ops(spec, approved)
            used = set()
            for op in ops:
                key = op["id"]
                pins = curate._file_pins(op, spec["sources"])
                used.update(pins)
                try:
                    if op["kind"] == "append":
                        _append_base(vault, op, spec, approved)
                    curate._check_sources(pins, approved, approved)
                    prepared[key] = pins, curate._parent_ids(root, curate._wiki_parts(op["path"]))
                except append.Conflict:
                    outcomes[key] = "conflict"
                except (KeyError, OSError, ValueError):
                    outcomes[key] = "failed"
            other = {k: p for k, p in spec["sources"].items() if "path" in p and k not in used}
            curate._check_sources(other, approved, approved)
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        outcomes.update((op["id"], "failed") for op in ops)

    for op in ops:
        key = op["id"]
        if any(outcomes[d] != "done" for d in op["depends_on"]):
            outcomes[key] = "dependency_incomplete"
        elif key not in outcomes:
            pins, parents = prepared[key]
            try:
                if op["kind"] == "append":
                    outcomes[key] = _execute_append(vault, claim, op, spec, approved, parents, pins)
                else:
                    outcomes[key] = curate._create(vault, op, pins, approved, parents)
            except append.Disconnected:
                disconnected.add(key)
                outcomes[key] = "failed"
            except append.Conflict:
                outcomes[key] = "conflict"
            except (KeyError, OSError, ValueError):
                outcomes[key] = "failed"
        yield key, outcomes[key]


def _execute_curate(claim: Approval, service: ApprovalService) -> ExecutionResult:
    spec = json.loads(claim.payload)
    outcomes = {}
    disconnected = set()
    has_append = any(op["kind"] == "append" for op in spec["ops"])
    files = _mixed_files(claim, spec, disconnected) if has_append else execute_creates(spec)
    for op_id, status in files:
        if not service.record_curate_result(claim.id, op_id, status):
            raise RuntimeError("could not record file outcome")
        outcomes[op_id] = status
    return _curate_report(spec["ops"], outcomes, disconnected)


def _recovery_inputs(claim: Approval):
    if not claim.payload or hashlib.sha256(claim.payload.encode()).hexdigest() != claim.payload_hash:
        return {}, None
    spec = json.loads(claim.payload)
    ops = {op["id"]: op for op in spec["ops"]}
    try:
        vault = pin_configured_root()
        if spec["root_id"] != [vault.device, vault.inode]:
            vault = None
    except (OSError, ValueError):
        vault = None
    return ops, vault


def _recovery_outcomes(store, approval, ops, vault):
    for file in store.curate_file_results(approval.id):
        status = file.status
        if status is None:
            status = (reconcile_file(vault, ops[file.op_id], file.base_sha256, file.result_sha256)
                      if vault else "unknown")
        yield file, status


def _recover_curate(store: Store, claim: Approval) -> tuple[str, str | None]:
    # Only the PC owner reaches here after the recorded process has exited.
    executor = (claim.executor_pid, claim.executor_started_at)
    ops, vault = _recovery_inputs(claim)
    if not ops:
        return "unknown", None
    outcomes = {}
    for file, status in _recovery_outcomes(store, claim, ops, vault):
        if file.status is None:
            # The journal represents a confirmed unapplied operation as a failure.
            recorded = "failed" if status == "unapplied" else status
            if not store.record_curate_result(claim.id, file.op_id, recorded, executor=executor):
                return "already_decided", None
        outcomes[file.op_id] = status
    result = _curate_report(ops.values(), outcomes)
    if store.finish_approval(claim.id, ok=result.ok, executor=executor):
        return ("done" if result.ok else "failed"), result.message
    return "already_decided", None


def planned_copy(manifest: dict) -> tuple[int, int]:
    copies = [file["size"] for file in manifest["files"] if file["status"] == "copy"]
    return len(copies), sum(copies)


def approval_summary(approval: Approval) -> str:
    """Project an approval to its destination and, for imports, copy totals."""
    if approval.kind == "note.create":
        return note_path(approval)
    if approval.kind == "brain.curate":
        if is_interrupted(approval):
            ops, vault = _recovery_inputs(approval)
            if not ops:
                return "결과 미상"
            store = Store.open(data_dir())
            outcomes = {file.op_id: status for file, status in _recovery_outcomes(store, approval, ops, vault)}
            if outcomes.keys() != ops.keys():
                return "결과 미상"
            return f"{_curate_report(ops.values(), outcomes).message}\n{_RECOVERY_HINT}"
        if approval.status == "pending" and approval.payload:
            spec = json.loads(approval.payload)
            if any(op["kind"] == "append" for op in spec["ops"]):
                try:
                    vault = pin_configured_root()
                    if spec["root_id"] == [vault.device, vault.inode]:
                        append.exchange(data_dir(), vault)
                        return "위키 정리 · Obsidian 어댑터 연결됨"
                except (OSError, ValueError):
                    pass
                return "위키 정리 · Obsidian 어댑터 미연결"
        return "위키 정리"
    try:
        manifest = json.loads(approval.payload or "")
        destination = "/".join(relative_parts(manifest["dest"]))
        count, size = planned_copy(manifest)
    except (KeyError, TypeError, ValueError):
        return "가져오기"
    return f"{_display(destination)} (복사 예정 {count}건 · {size}바이트)"


def decide_on_pc(
    store: Store, service: ApprovalService, id: str, actor: str, *, approve: bool,
) -> tuple[str, str | None]:
    """Use the PC approval channel for both CLI and authenticated HUD decisions."""
    row = store.get_approval(id)
    if row and row.can_decide(actor, "cli"):
        if approve and row.status == "pending":
            # The kind is fixed at creation; decide still checks the row atomically.
            if row.kind != "brain.curate" and row.kind not in service.executors:
                return "executor_missing", row.kind
        if row.kind == "brain.curate" and is_interrupted(row):
            if approve:
                return _recover_curate(store, row)
            return "already_decided", _RECOVERY_HINT
    decision = service.decide(id, actor, "cli", approve)
    if decision.status != "claimed":
        return decision.status, None

    claim = decision.approval
    assert claim is not None
    try:
        if claim.kind == "brain.curate" and claim.kind not in service.executors:
            result = _execute_curate(claim, service)
        else:
            result = service.execute(claim)
        if service.finish(id, result.ok):
            return ("done" if result.ok else "failed"), result.message
    except (Exception, KeyboardInterrupt):
        # The executor may already have published its result.
        pass
    return "unknown", None
