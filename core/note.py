"""Publish the approved note bytes to a server-generated inbox path."""

from datetime import UTC, datetime

from brain.publish import pin_configured_root, publish
from worklog import Approval

from .approval import ExecutionResult


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


NOTE_EXECUTORS = {"note.create": create_note}
