# 83. 자비스 개발 하네스 설계서 (v0.1, 2026-11-20)

> 상태: **승인됨 (2026-11-20)**. 저장소: javis(코드), woongs-brain(두뇌, private).
> 배경 조사(2026-11): 코딩 에이전트·협업·하네스 동향, Hermes Agent 동향 — 개인 분석 노트에 보관.

## 1. 의도와 이유

새 자비스 저장소를 **총괄 1명 + Claude 팀 + Codex 팀**이 나눠서 만든다. 하네스는 이 팀들이 일하는 틀이다. 정해 두는 것은 다음과 같다.
- 누가 어떤 모델과 강도로 일하는가
- 일을 어떻게 넘기는가
- 무엇을 통과해야 끝나는가
- 무엇은 절대 못 하는가

이유는 두 가지다.
1. WorkOffice는 규칙이 글로만 있고 구조가 계속 바뀌어서 복잡해졌다. 이번에는 **규칙을 실행되는 장치로** 고정한다.
2. 사용자가 곧 이직해서 **개인 맥으로 옮긴다.** 그래서 이 PC의 전역 설정(`~/.claude`의 OMC·ECC·rtk, Codex `config.toml`) 없이도 **저장소만으로** 똑같이 돌아가야 한다.

**범위**: 개발 하네스만 다룬다. 자비스 자체가 돌아가는 틀(게이트웨이·승인·taint·두뇌 쓰기)은 다음 설계서에서 다룬다. 다만 작업 기록 형식은 나중에 WORK LOG 화면이 그대로 쓸 수 있게 맞춘다.

## 2. 목표 / 비목표 / 실패 조건

**목표**
- G1. 깨끗한 Windows나 macOS에서 다음 순서로 같은 결과가 나온다: `git clone` → `uv run harness doctor` → doctor가 요구하는 로그인만 하면 끝.
- G2. 역할별 모델과 강도는 **파일 하나(`harness/roles.toml`)**가 원본이다. 바꿀 때도 그 파일만 고친다.
- G3. 총괄은 **어떤 도구 세션이든** 될 수 있다(Claude Code든 Codex든). 계정 사정에 따라 Fable과 GPT-6.2 Astra를 오간다.
- G4. 넘겨주기, 검사, 교차 리뷰, 커밋 권한을 **스크립트와 훅이 강제**한다. 글로만 쓴 규칙에 기대지 않는다.
- G5. 모든 작업 실행을 기록한다(역할·모델·시간·결과). Claude와 Codex의 실제 사용량을 비교할 근거로 쓴다.

**비목표**
- 범용 하네스 제품이나 플러그인 배포. 안정된 뒤 떼어낼 수는 있지만 지금은 하지 않는다.
- 자비스 런타임 기능(Telegram, 두뇌 읽기·쓰기).
- 리눅스 서버나 클라우드 실행. 고려는 하되 검증 대상은 아니다.

**실패 조건** (기능은 돌아가도 이러면 실패다)
- 이 PC의 전역 설정에 기대서, 맥에서 처음 돌릴 때 깨진다.
- API 키가 있어야 돌아간다. 구독 로그인만 쓴다는 원칙을 어긴 것이다.
- 하네스 자체가 거대해져서 자비스보다 하네스를 고치는 시간이 더 길다.
- 에이전트가 비밀값, vault, 다른 작업 폴더에 손댈 수 있다.

## 3. 제약

| 제약 | 내용 |
|---|---|
| OS | Windows 11과 macOS(Apple Silicon) 둘 다. 셸 스크립트(.ps1/.sh)는 쓰지 않고 **Python 3.12 + uv**로 통일 |
| 과금 | 구독 로그인만 쓴다: `claude` CLI 로그인, Codex ChatGPT 로그인. API 키는 쓰지 않는다 |
| 총괄 모델 | 기본은 Claude Fable 5.2 high. **현재 계정은 Max가 아니라 Fable을 쓸 수 없으므로 GPT-6.2 Astra high로 대체**. 사용자가 곧 Max로 다시 로그인할 예정 |
| 커밋 | 총괄만 커밋한다(사용자 결정) |
| Codex 쓰기 | 작업별 전용 폴더 안에서만 `-s workspace-write`를 쓴다. ⚠ **사용자 최종 확인 필요** |
| 비밀값 | 에이전트에게 비밀값, `.env`, vault, 메일 데이터를 넘기지 않는다 |
| 현재 설치 버전 (11-20 실측) | codex-cli **0.164.0**(GPT-6.2는 0.171.0부터 공식 카탈로그에 있음 → 업데이트 필요할 수 있음), Claude Code 2.1.329, uv 0.11.18 |

## 4. 접근 방식 (선택 완료)

| 안 | 내용 | 장단점 | 결정 |
|---|---|---|---|
| **A. 저장소 내장형** | 지침·역할·훅·스크립트를 전부 저장소 안에 둔다 | clone만 하면 끝, 버전이 코드와 함께 묶임. 다른 프로젝트에서 쓰려면 복사해야 함 | **채택** |
| B. 플러그인형 | 하네스를 별도 저장소의 Claude Code 플러그인으로 | 여러 프로젝트에서 재사용 가능. 저장소 2개를 맞춰야 하고, Codex 쪽은 감싸지 못함, 너무 이른 범용화 | 기각 |
| C. 전역 도구 위에 설정만 | OMC·ECC·codex-plugin-cc를 그대로 사용 | 할 일이 가장 적음. 전역 설치에 기대고 변경도 빨라서 이식성 목표에 정면으로 어긋남 | 기각 |
| (참고) Hermes Agent 위에 | doc82 참고 | Claude 구독은 추가 크레딧으로만 됨 | 기각. 아이디어만 가져옴 |

## 5. 설계

### 5.1 저장소 구조
```
jarvis/
  AGENTS.md              # 유일한 지침 원본(목표 100줄 이하, 목차형). Codex가 바로 읽음
  CLAUDE.md              # 한 줄: @AGENTS.md  (Claude Code가 가져와 읽음)
  .gitattributes         # * text=auto eol=lf  (줄바꿈 OS 차이 제거)
  .env.example           # 비밀값 이름만. 실제 .env는 gitignore
  pyproject.toml         # harness 패키지 + 명령 등록 (uv)
  harness/
    roles.toml           # 역할 → 도구·모델·강도·대체 순서 (원본)
    cli.py               # uv run harness <명령>
    guards.py            # 금지 규칙 판정(경로·명령). 훅·git 훅이 같은 함수를 부름
    hooks/               # 각 도구 훅의 진입점(얇게, guards.py만 부름)
    tests/               # 하네스 자체 테스트(pytest)
  .claude/
    settings.json        # 이 프로젝트 전용 훅·권한
    agents/*.md          # Claude 팀 서브에이전트(roles.toml에서 생성)
  .agents/skills/        # 표준 SKILL.md 원본 (Codex가 읽는 위치)
  .githooks/             # pre-commit 등. git config core.hooksPath=.githooks (doctor가 설정)
  .handoff/<task-id>/    # 팀 사이에 넘기는 파일 (커밋됨, 작업 이력)
  .harness/              # 실행 기록·상태 (gitignore)
  .github/workflows/harness.yml   # windows-latest + macos-latest에서 하네스 테스트
```

### 5.2 역할 정의 — `harness/roles.toml`
사용자가 확정한 표를 그대로 옮긴다. 총괄에만 대체 순서를 둔다.
```toml
[lead]            # 총괄
candidates = [
  { tool = "claude", model = "claude-fable-5-2", effort = "high" },
  { tool = "codex",  model = "gpt-6.2-astra",    effort = "high" },  # Max 아닐 때
]
[claude.dev_hard]   tool="claude" model="claude-opus-5-5"   effort="high"
[claude.dev]        tool="claude" model="claude-opus-5-5"   effort="medium"
[claude.simple]     tool="claude" model="claude-sonnet-5-5" effort="low"
[claude.reviewer]   tool="claude" model="claude-opus-5-5"   effort="medium"  # Codex 코드 리뷰
[codex.dev]         tool="codex"  model="gpt-6.2-sol"       effort="medium"
[codex.dev_hard]    tool="codex"  model="gpt-6.2-astra"     effort="high"
[codex.visual]      tool="codex"  model="gpt-6.2-sol"       effort="low"     # 화면 대조
[codex.reviewer]    tool="codex"  model="gpt-6.2-sol"       effort="high"    # Claude 코드 리뷰
[ownership]
claude = ["core/", "brain/", "security/", "channels/telegram/"]
codex  = ["hud/", "worklog/", "workshop/", "teams/"]
```
- **총괄 선택**: `harness doctor`가 위에서부터 실제로 쓸 수 있는지 확인한다(짧은 시험 호출). 쓸 수 있는 첫 번째를 `.harness/local.toml`에 기록한다. Max로 다시 로그인하면 doctor를 한 번 더 돌리면 된다. 수동으로 고정하려면 `JARVIS_LEAD=codex`.
- **역할 → 실제 호출**:
  - Claude: `claude -p --model <m> --effort <e> --setting-sources project,local ...`
  - Codex: `codex exec -m <m> -c model_reasoning_effort=<e> --ignore-user-config -C <작업폴더> -s workspace-write ...`
  - `--setting-sources`와 `--ignore-user-config`로 **이 PC의 전역 설정을 끊어서**, 맥의 깨끗한 환경과 똑같이 만든다(11-20 CLI 도움말로 옵션이 있는 것 확인).
- `.claude/agents/*.md`는 roles.toml을 보고 `harness sync`가 만든다. 손으로 고치지 않는다.

### 5.3 총괄은 도구에 매이지 않는다
- 총괄 세션이 Claude Code든 Codex든 같은 AGENTS.md를 읽고, 같은 `harness` 명령으로 일한다. 하네스의 동작은 셸 명령으로만 노출해서 두 도구가 똑같이 부를 수 있게 한다.
- 총괄이 하는 일: 작업을 정하고 명세를 쓴다(무엇·왜·완료 기준). 디스패치하고, 검사 결과를 판정하고, 커밋한다. "어떻게"는 쓰지 않는다(doc80 원칙).

### 5.4 작업 한 건의 흐름
```
harness task new "<제목>" --owner claude|codex
   → .handoff/T012/spec.md 생성(템플릿: 무엇·왜·완료 기준·건드릴 경로·금지)
harness dispatch T012 --role codex.dev
   → git worktree ../jarvis-wt/T012 (브랜치 task/T012)
   → 그 폴더에서만 실행, 결과는 .handoff/T012/result.md
harness gate T012
   → 완료 기준을 기계로 확인: 테스트·린트·타입·(UI면) 스크린샷 대조
   → .handoff/T012/gate.json
harness review T012
   → 만든 쪽의 반대 팀 리뷰어 자동 선택(Codex가 만들면 claude.reviewer)
   → .handoff/T012/review.md  (의견만으로는 통과 못 함. gate가 통과해야 함)
harness merge T012            # 총괄만. gate와 review가 통과해야 merge·커밋
```
- 넘겨주기는 작업당 4번까지다(spec → result → review → 수정 1회). 넘으면 `harness`가 멈추고 총괄에게 알린다.
- 작업 상태(`.harness/tasks/T012.json`)는 단계마다 저장한다. 세션이 끊겨도 `harness resume T012`로 이어서 한다(Hermes "재시작에도 살아남는 작업" 아이디어).
- 실행마다 시간 상한과 턴 상한을 둔다(roles.toml 기본값, 작업별로 덮어쓰기 가능).

### 5.5 강제 장치 (글이 아니라 코드로)
판정 함수는 `harness/guards.py` 하나에 모은다. 다음 세 곳이 같은 함수를 부른다.
1. Claude Code PreToolUse 훅(`.claude/settings.json` → `uv run harness hook claude-pre`)
2. Codex 훅(0.164에서 지원 범위 확인 필요. 안 되면 3번만으로 막는다)
3. git pre-commit(`.githooks/pre-commit` → `uv run harness hook git-pre-commit`). 어떤 도구를 쓰든 마지막에 막는 장치다.

시작할 때 규칙(실패가 실제로 날 곳만, 나머지는 사고가 나면 추가한다):
| # | 규칙 | 이유 |
|---|---|---|
| R1 | `.env*`, `secrets/`, vault 경로, `~/.claude`, `~/.codex` 읽기·쓰기 금지 | 비밀값과 두뇌 보호 |
| R2 | 작업은 자기 worktree 밖에 쓸 수 없다 | 팀끼리 충돌 방지 |
| R3 | 자기 팀 소유 경로(ownership) 밖 수정은 경고, gate에서 실패 | 역할 분리 |
| R4 | `git commit/push/merge`는 `harness merge`만 할 수 있다(환경 표식 확인) | 커밋은 총괄만 |
| R5 | 하위 실행에 넘기는 환경변수는 허용 목록만 | Hermes GHSA-p22p 반면교사 |
| R6 | 무인 실행(dispatch)은 Claude `permissions.denyInsteadOfPrompting`을 켠다 | 승인 대기로 멈추지 않게(doc81) |

규칙마다 주석으로 **만든 이유(사고·근거)와 만료 조건**을 적는다(doc80 관리 원칙).

### 5.6 기록 (관측)
- `.harness/ledger.jsonl`에 실행 한 건마다 한 줄씩 남긴다: `{ts, task, role, tool, model, effort, start, end, exit, turns, tokens?, cost_hint?}`. 토큰은 CLI가 알려줄 때만 넣는다.
- `harness usage`로 팀별, 모델별, 작업별 합계를 본다. 1단계가 끝나면 Claude와 Codex 사용량을 비교한다(G5).
- 이벤트 이름(`JARVIS/TEAM/WRITE/APPROVE…`)과 필드는 나중에 WORK LOG HUD가 그대로 읽을 수 있게 맞춘다.

### 5.7 스킬
- 원본은 `.agents/skills/<name>/SKILL.md`(표준 형식)다.
- Claude Code는 `.claude/skills/`를 읽는다. 그런데 Windows 심볼릭 링크는 개발자 모드가 있어야 만들 수 있어서 **링크 대신 `harness sync`가 복사**한다. 복사본은 gitignore하고, doctor가 원본과 다른지 검사한다.
- 에이전트가 스킬을 고치자고 **제안하는 것까지만** 허용한다(`.handoff/.../skill-proposal.md`). 반영은 사람이 승인해야 한다.

### 5.8 `harness doctor` (이식성의 핵심)
점검 항목: Python/uv, git, node(HUD용), `claude` 버전과 로그인, `codex` 버전과 로그인, **Codex가 GPT-6.2를 쓸 수 있는지 시험 호출**(0.164면 업데이트 안내), 총괄 후보 확인, `core.hooksPath` 설정, `.env`에 필요한 키가 있는지(값은 출력하지 않음), 스킬 동기화 상태, 줄바꿈 설정.
결과는 ✅/⚠/✗로 보여주고, 고치는 명령을 함께 안내한다. `--fix`를 주면 안전한 것(훅 경로, sync)만 자동으로 고친다.

### 5.9 이식성 검증
- 하네스 테스트는 GitHub Actions에서 **windows-latest와 macos-latest** 둘 다 돈다. 실제 claude·codex 호출은 가짜 실행 파일로 대체한다.
- 경로는 전부 `pathlib`을 쓰고 절대 경로는 금지한다. 셸 분기(`if windows`)는 `harness/platform.py` 한 곳에만 둔다.

### 5.10 코드와 두뇌 분리 (2026-11-20 사용자 결정)
- **jarvis 코드 저장소 = public**, **두뇌(Obsidian vault) = 별도 private 저장소**.
- 코드는 두뇌 위치를 `JARVIS_BRAIN_DIR` 설정 하나로만 안다. 두뇌를 읽고 쓰는 일은 `brain/` 모듈만 하고, 쓰기 규칙은 두뇌 쪽 `BRAIN.md`가 원본이다. 개발과 테스트는 `fixtures/brain-sample/`(가짜 두뇌)로 한다.
- **두뇌 = 새 private vault `woongs-brain`** (2026-11-20 사용자 결정). 기존 논문 연구용 vault는 연결하지 않는다(읽기 전용으로도 쓰지 않음). 두뇌는 처음부터 새로 구성한다.
- **public이 되기 위한 조건** (하네스가 강제):
  - P1. `.handoff/`(작업 명세와 결과)에는 개인 맥락이 섞이기 쉽다. 그래서 공개 저장소에 커밋하지 않는다. 작업 이력은 **두뇌 저장소의 `jarvis-dev/handoff/`**에 보관한다. 코드 저장소에는 gitignore된 로컬 사본만 둔다.
  - P2. pre-commit에 **비밀값·개인정보 검사**를 넣는다(gitleaks류와 이메일·전화·절대 경로 `C:\Users\…`·`/Users/…` 패턴 차단). 금지 규칙 **R7**로 추가한다.
  - P3. `.env`, `.harness/`, `local.toml`, 두뇌 경로는 gitignore한다. 예시 파일에는 이름만 적는다.
  - P4. 팀 모듈(릴스 등)에 들어 있는 계정 이름, 채널 ID, 운영 데이터는 설정이나 두뇌 쪽에 두고, 코드에는 넣지 않는다.
  - P5. 처음에는 **private으로 시작**한다. 1단계가 끝나면 히스토리 전체를 검사(gitleaks 전체 이력)하고, 통과하면 public으로 전환한다. 한번 public으로 푼 기록은 되돌릴 수 없으므로 이 순서를 지킨다.

## 6. 짓는 사람에게 맡기는 것
- CLI 프레임워크 선택(typer나 argparse 등), 내부 모듈 나누기
- `.handoff` 템플릿의 정확한 문구
- gate가 부를 검사 명령의 구체 구성(Python/TS 도구 선택은 자비스 본체 설계에 맞춤)
- ledger 필드 이름 세부
- Codex 훅의 실제 지원 여부를 확인해서 R1~R4를 Codex 쪽에도 붙일지 결정. 못 붙이면 git 훅과 gate가 대신 막는다.

## 7. 빌더 점검 뒤 Claude가 정한 것 (사용자 확인용)

다른 에이전트가 이 문서만 보고 점검했고, 그 질문 9개 중 7개는 아래처럼 Claude가 정했다.

| # | 질문 | Claude의 결정 |
|---|---|---|
| C1 | 1차 범위가 너무 넓다 | **1단계(MVP)**에 넣는 것: `doctor`, `task new`, `dispatch`, `gate`, `merge`, ledger 기록, 가드 R1·R2·R4·R5(Claude 훅 + git 훅), Windows·macOS CI. **2단계**로 미루는 것: 자동 `review` 선택, `resume`, `usage` 집계, 스킬 `sync`, R3·R6, 스크린샷 대조. 하네스 1단계는 대략 코드 1,000줄 이하를 목표로 한다(실패 조건 "하네스 비대화" 방지) |
| C2 | gate가 무엇을 검사하나 (본체 스택이 미정) | gate는 **설정 기반**이다. `.handoff/<id>/spec.md`의 "완료 기준" 블록에 적힌 명령을 실행하고, 종료 코드로 판정한다. 하네스는 언어를 모른다. 본체 스택(Python 코어와 TS HUD로 예상)이 정해지면 명령만 바꿔 넣는다 |
| C3 | 총괄은 누가 띄우나 | **사람이 연 대화형 세션**(Claude Code나 Codex)이 총괄이다. 하네스는 총괄을 띄우지 않는다. doctor가 하는 일은 "지금은 어느 도구로 총괄 세션을 열어라"를 판단해 안내하는 것까지다 |
| C4 | 모델 ID·버전 | Codex는 **0.171.0 이상을 필수**로 한다. doctor가 업데이트 명령을 안내한다. 모델 ID는 `claude-fable-5-2`, `claude-opus-5-5`, `claude-sonnet-5-5`, `gpt-6.2-sol`, `gpt-6.2-astra`이고, doctor의 시험 호출로 실제 유효한지 확인한다 |
| C5 | `.handoff`와 "커밋은 총괄만"의 충돌 | 작업자는 커밋하지 않는다. 결과는 worktree 안의 gitignore된 `.task/result.md`에 쓴다. 하네스가 이 파일을 메인의 `.handoff/<id>/`로 모은다. 작업 브랜치의 코드 변경과 `.handoff`는 **`harness merge`가 squash 커밋 1개**로 묶는다 |
| C6 | 구독 한도나 동시 실행 제한에 걸리면 | 자동으로 재시도하지 않는다. 작업을 `blocked:quota`로 표시하고 ledger에 기록한 뒤 총괄에게 알린다. 1단계 동시 실행은 **도구당 1개**다 |
| C7 | 스크린샷 대조 기준 | 2단계로 미룬다. HUD 설계서에서 기준 이미지, 허용 오차, 렌더러를 정한다 |

## 8. 열린 질문 (사용자 결정)
1. ✅ **2026-11-20 사용자 결정: (a) 허용.** Codex는 작업 worktree 안에서 `workspace-write`로 구현한다. 1단계 doctor에서 읽기 제한 옵션을 확인하고, 있으면 강화한다. (아래는 결정 당시 기록)
   **Codex 권한 범위.** `workspace-write`는 *쓰기*를 작업 폴더 안으로 막아 준다. 하지만 *읽기*는 기본적으로 디스크 전체가 열려 있다(vault, 다른 앱의 개인 데이터 포함, ⚠ 0.171의 읽기 제한 옵션은 확인 필요). 비밀값 `.env`는 worktree에 체크아웃되지 않아서 같은 폴더에는 없다. 그래도 절대 경로로 다른 폴더를 읽을 수는 있다.
   - (a) 이대로 허용한다. Codex에는 비밀값이 들어 있지 않은 worktree만 주고, 사후 감시는 ledger로 한다.
   - (b) 읽기 제한 옵션이 확인될 때까지 Codex는 `read-only`로 리뷰만 하고, Codex 팀 구현은 보류한다.
   - Claude 추천은 **(a) + 1단계 doctor에서 읽기 제한 옵션 확인 후 강화**다.
2. ✅ 저장소: `woongja/javis`(코드, private으로 시작 → 1단계 후 public 검토), `woongja/woongs-brain`(두뇌, private).
