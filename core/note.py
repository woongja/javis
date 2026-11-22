"""Execute approved brain writes."""

import json
from datetime import UTC, datetime

from brain import _display
from brain.importer import copy_manifest
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
