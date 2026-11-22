"""Execute approved brain writes."""

import json
from datetime import UTC, datetime

from brain import _display
from brain.importer import copy_manifest
from brain.policy import relative_parts
from brain.publish import pin_configured_root, publish
from worklog import Approval, Store

from .approval import ApprovalService, ExecutionResult


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


def planned_copy(manifest: dict) -> tuple[int, int]:
    copies = [file["size"] for file in manifest["files"] if file["status"] == "copy"]
    return len(copies), sum(copies)


def approval_summary(approval: Approval) -> str:
    """Project an approval to its destination and, for imports, copy totals."""
    if approval.kind == "note.create":
        return note_path(approval)
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
    if approve:
        row = store.get_approval(id)
        if row and row.status == "pending" and row.can_decide(actor, "cli"):
            # The kind is fixed at creation; decide still checks the row atomically.
            if row.kind not in service.executors:
                return "executor_missing", row.kind
    decision = service.decide(id, actor, "cli", approve)
    if decision.status != "claimed":
        return decision.status, None

    claim = decision.approval
    assert claim is not None
    try:
        result = service.execute(claim)
        if service.finish(id, result.ok):
            return ("done" if result.ok else "failed"), result.message
    except (Exception, KeyboardInterrupt):
        # The executor may already have published its result.
        pass
    return "unknown", None
