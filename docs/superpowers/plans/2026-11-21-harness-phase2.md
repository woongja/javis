# javis 하네스 2단계 계획 (v0.1, 2026-11-21)

**전제:** 1단계 완료(2026-11-20, main 58bc1b8, CI Windows·macOS 초록, public 전환, ruleset: 삭제·force push 차단).
설계서: `docs/superpowers/specs/2026-11-20-harness-design.md` (G1–G5, 실패 조건, §7 C1 이월 목록).

## 0. 원칙
- 하네스는 수단이다. 설계서 실패 조건 "자비스보다 하네스를 고치는 시간이 더 길다"를 지킨다.
  → 2단계 예산: 하네스 코드 +400줄 이하(현재 971줄 → 1,400줄 상한). 넘으면 범위를 줄인다.
- 실제 사고·위험이 확인된 것만 넣는다(AGENTS.md "Add a rule only after a real failure").
- 2단계가 끝나면 **자비스 본체 1단계**(폰 요청 → 결과 → 일지 → 새 세션 출처 회수)로 넘어간다. 본체 작업이 필요로 하는 것만 하네스에 더한다.

## 1. 우선순위와 근거

| 순위 | 항목 | 왜 지금 | 근거 |
|---|---|---|---|
| **A1** | **작업자 두뇌 읽기 차단 (OS 수준)** | 실패 조건 위반이 실측됨: Codex 작업자는 worktree 밖 파일을 읽을 수 있다(2026-11-20 canary 실측, `sandbox_permissions=[]`도 동일). 두뇌 경로가 설정돼 이제 실제 위험. | 설계 §2 실패 조건 4, §8-1 |
| A2 | Codex 훅으로 R1/R2 적용 | Codex 0.171은 훅 지원(`--dangerously-bypass-hook-trust` 존재). 지금은 Codex 쪽 가드가 git pre-commit뿐. | 설계 §5.5-2 |
| A3 | `harness review` + gate가 리뷰 통과 요구 | 교차 리뷰가 글로만 존재(T002–T010 대부분 총괄 diff만). G4 "교차 리뷰를 스크립트가 강제". | G4, C1 |
| A4 | R3 소유 경로 검사(gate) | spec마다 "이번만 허용"을 손으로 적었다. spec의 `## 건드릴 경로`를 gate가 기계로 검사. | 설계 R3, G4 |
| B1 | merge 전 base 확인 | Claude·Codex 두 팀이 동시에 돌면 각자 초록인 작업이 합쳐서 깨질 수 있다. dispatch 시점 main OID 기록 → merge 때 main이 움직였으면 재gate 요구. | 최종 리뷰 이월 |
| B2 | `harness usage` | G5 Claude/Codex 사용량 비교. ledger는 이미 있다 → 집계만. | G5 |
| B3 | R6 무인 실행 승인 대기 방지 | Claude 작업자가 승인 대기로 멈추는지 실측 후, 멈추면 넣는다(지금 `acceptEdits`). | 설계 R6 |
| C | resume, 스킬 sync, 스크린샷 대조, lock 원자성 | 필요가 생기기 전엔 안 함. 스크린샷은 HUD 설계서에서, 스킬 sync는 `.agents/skills`가 생기면. | C1, C7 |

## 2. 항목별 완료 기준

### A1 작업자 두뇌 읽기 차단
- 조사 먼저(코드 작성 전): Codex Windows `elevated` 샌드박스가 별도 로컬 계정으로 돈다면, 두뇌 폴더에 그 계정 **읽기 거부 ACL**을 거는 방식. macOS는 Codex seatbelt 정책 확인.
  Claude 작업자는 Claude Code `sandbox` 설정(파일시스템 읽기 거부)으로 Bash 하위 프로세스까지 막을 수 있는지 확인.
- `harness doctor`가 "작업자 계정으로 두뇌 읽기 시도 → 실패"를 실측하는 점검을 갖는다(canary 파일, 값 출력 없음).
- 완료: Windows에서 Codex·Claude 작업자 둘 다 두뇌 canary 읽기 실패. macOS는 CI에서 가능한 범위 + 수동 1회.
- ⚠ 사용자 승인 필요 지점: 시스템 ACL 변경(관리자 권한)이 필요하면 그 명령은 사용자가 실행.

### A2 Codex 훅
- Codex 훅 설정을 저장소 로컬로 두고(`--ignore-user-config`와 공존 확인), `harness hook codex-pre`가 같은 `guards.py`를 부른다.
- 완료: Codex 작업자가 `.env` 읽기 시도 → 차단 로그. 훅 신뢰(trust) 처리 방식 문서화.

### A3 리뷰 강제
- `harness review <id>`: 작업 owner의 반대 팀 `*.reviewer` 역할을 **read-only**로 실행, 결과를 `.handoff/<id>/review.md`(판정 줄 `VERDICT: pass|changes`)로.
- gate: 변경이 `harness/`·보안 경로를 건드리거나 50줄 이상이면 `review.md`의 pass를 요구. 문서·소규모는 면제(2026-11-20 총괄 기준).
- 완료: 리뷰 없는 하네스 변경은 gate 실패, 리뷰 pass 후 통과.

### A4 R3 소유 경로
- spec `## 건드릴 경로`의 경로 목록 밖 변경 → gate 실패(파일 목록 출력). `roles.toml [ownership]`은 기본값, spec이 좁히거나 넓힌다.
- 완료: 범위 밖 파일 1개 수정한 작업이 gate에서 실패.

### B1 base 확인
- dispatch가 `base_oid` 저장, merge가 `main != base_oid`이고 변경 파일이 겹치면 "re-gate 필요"로 거부. 안 겹치면 통과.

### B2 usage
- `harness usage [--by team|model|task]`: ledger 합계(실행 수, 시간, 실패·quota). 토큰은 CLI가 줄 때만.

## 3. 순서와 담당
1. A1 조사(총괄 실측) → 방식 확정 → 구현 작업(Claude 팀 소유: `security/` 성격, 하네스 쪽은 Codex)
2. A2 → A4 → A3 (A3는 A4의 경로 정보 재사용)
3. B1, B2 (작음, 묶어서 1작업 가능)
4. 2단계 종료 판정: 위 완료 기준 실측 + CI 초록 + 하네스 1,400줄 이하
5. 자비스 본체 1단계 설계서 작성으로 이동

## 4. 열린 질문 (사용자)
- A1에서 OS ACL 변경이 필요할 때 실행 주체(사용자 관리자 터미널).
- 2단계 전체를 건너뛰고 본체부터 갈지 — 총괄 추천은 **A1만 먼저, 나머지는 본체 작업과 병행**.

## 5. 진행 결과 (2026-11-21)

- A1 T012·T013 완료. `harness doctor`에 `brain-isolation` 점검을 추가했고, Codex 샌드박스 계정의 읽기 거부 ACL은 사용자 승인 후 적용한다. Claude 샌드박스 `denyRead`는 Windows에서 지원되지 않아 Windows에서는 Codex 작업자만 사용한다.
- A2 T014 완료. 명령줄에서 `-c hooks.PreToolUse`와 `--dangerously-bypass-hook-trust`를 전달한다. 저장소의 `.codex/hooks.json`은 로드되지 않는다. Windows 훅은 PowerShell에서 실행하며, 훅 오류 시 차단한다(fail-closed).
- A3 T017 완료. Claude 리뷰어는 `--restricted --safe-mode --strict-mcp-config --tools Read,Grep,Glob`과 `permissions.deny`를 적용해 실행한다.
- A4 T016 완료.
- B1·B2 T018 완료.
- B3 보류(실측 전).
- 문서 T015·T019.
- 남은 위험. gate 명령은 작업자 코드를 총괄 권한으로 실행한다(격리 미구현). Claude 작업자의 PreToolUse 훅은 worktree의 하네스 코드를 실행한다(Windows에서는 Claude 작업자 미사용). 셸 경로 검사는 휴리스틱이다.
- 하네스 1,314줄(상한 1,400).
