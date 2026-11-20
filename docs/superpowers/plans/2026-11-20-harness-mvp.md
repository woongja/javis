# javis 하네스 1단계(MVP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Windows·macOS 어디서든 `git clone → uv run harness doctor`로 동작하는 개발 하네스 1단계(doctor·task new·dispatch·gate·merge·ledger·가드 R1/R2/R4/R5/R7·CI)를 만든다.

**Architecture:** 순수 Python 패키지 `harness/`(표준 라이브러리 위주) + `uv run harness <명령>` 진입점. 판정 로직(guards)은 순수 함수로 두고, Claude PreToolUse 훅과 git pre-commit 훅이 같은 함수를 부른다. 실제 `claude`/`codex` 호출은 `runner`가 만들고, 테스트에서는 가짜 실행 파일(환경변수로 경로 교체)로 대체한다.

**Tech Stack:** Python ≥3.12, uv, pytest, argparse, tomllib, git CLI, GitHub Actions(windows-latest, macos-latest).

**Spec:** `docs/superpowers/specs/2026-11-20-harness-design.md` (1단계 범위 = §7 C1)

## Global Constraints

- OS: Windows 11 + macOS(Apple Silicon). 셸 스크립트(.ps1/.sh) 금지. 유일한 예외는 `.githooks/pre-commit`(git이 sh로 실행하는 2줄짜리 shim).
- 경로는 전부 `pathlib`. OS 분기는 `harness/platform.py` 한 곳에만 둔다. 절대 경로를 하드코딩하지 않는다.
- 과금: 구독 로그인만 쓴다. 하위 프로세스 환경에서 `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` 등 허용 목록 밖 변수는 제거한다(R5).
- 역할 표(정확한 값): lead 후보 = `claude/claude-fable-5-2/high` → `codex/gpt-6.2-astra/high`; `claude.dev_hard`=claude-opus-5-5/high, `claude.dev`=claude-opus-5-5/medium, `claude.simple`=claude-sonnet-5-5/low, `claude.reviewer`=claude-opus-5-5/medium, `codex.dev`=gpt-6.2-sol/medium, `codex.dev_hard`=gpt-6.2-astra/high, `codex.visual`=gpt-6.2-sol/low, `codex.reviewer`=gpt-6.2-sol/high.
- 소유 경로: claude = `core/ brain/ security/ channels/telegram/`, codex = `hud/ worklog/ workshop/ teams/`.
- Claude 호출: `claude -p --model <m> --effort <e> --setting-sources project,local --permission-mode acceptEdits`(프롬프트는 stdin, cwd=worktree).
- Codex 호출: `codex exec -m <m> -c model_reasoning_effort="<e>" --ignore-user-config -C <worktree> -s workspace-write -`(프롬프트는 stdin).
- Codex 최소 버전: **0.171.0**.
- worktree: `<repo의 부모>/javis-wt/<task-id>`, 브랜치 `task/<task-id>`.
- 넘겨주기 상한: 작업당 4회. 동시 실행: 도구당 1개. 한도에 걸리면 자동 재시도하지 않고 `blocked:quota`로 표시.
- 커밋: 작업자는 커밋하지 않는다. 코드 커밋은 `harness merge`만 한다(squash 1개).
- **계획 단계 결정(설계 §5.10 P1과 §7 C5의 충돌 해소):** `.handoff/`는 공개 저장소에서 gitignore한다. merge는 **코드만** 커밋하고, handoff 폴더는 `JAVIS_BRAIN_DIR`가 설정돼 있으면 `<brain>/javis-dev/handoff/<id>/`로 **복사만** 한다(두뇌 저장소 커밋은 하네스가 하지 않는다).
- 하네스 1단계 코드(테스트 제외)는 약 1,000줄 이하로 맞춘다.
- 테스트 fixture(`git_repo`, `ctx`, `worker_ctx`, `fake_bins`, `git_repo_with_harness`, `dispatched_task`, `gated_task` 등)는 모두 `tests/conftest.py`에 두고, 처음 쓰는 Task에서 추가한다. `fake_bins`는 `JAVIS_CLAUDE_BIN`/`JAVIS_CODEX_BIN`을 `tests/fakes/fake_agent.py`를 실행하는 래퍼로 지정한다(Windows는 `.cmd`, macOS는 실행 권한 있는 스크립트).
- 공개 전환(설계 P5)은 이 계획의 범위 밖이다. 1단계를 마친 뒤 별도로 이력 전체를 검사한다.

## Review Focus

1. **경로에 공백·한글이 있는 경우**(예: OneDrive 바탕화면, 한글 작업 제목) → task id는 ASCII 슬러그로 만들고, 모든 subprocess는 리스트 인자로 부른다(Task 5·6 테스트).
2. **Windows의 `.cmd` shim**(npm으로 설치한 `codex.cmd`) → `shell=False`로 실행하려면 `shutil.which`로 찾은 전체 경로를 써야 한다(Task 1 `platform.find_exe` 테스트).
3. **CRLF로 저장된 spec.md**(Windows 편집기) → gate 블록 파싱이 `\r\n`에서도 같은 명령을 돌려준다(Task 7 테스트).
4. **dispatch 중간 종료로 남은 lock** → 기록된 PID가 죽어 있으면 오래된 lock으로 보고 회수한다(Task 6 테스트).
5. **gate가 끝난 뒤 worktree가 바뀐 경우, 또는 main이 더러운 경우의 merge** → 거부하고 이유를 출력한다(Task 8 테스트).

---

### Task 1: 뼈대 + 플랫폼 계층 + CI

**Files:**
- Create: `pyproject.toml`, `.gitattributes`, `.gitignore`, `.env.example`, `harness/__init__.py`, `harness/cli.py`, `harness/platform.py`, `harness/paths.py`, `.github/workflows/harness.yml`
- Test: `tests/test_platform.py`, `tests/test_cli.py`, `tests/conftest.py`

**Interfaces:**
- Produces:
  - `harness.cli.main(argv: list[str] | None = None) -> int`(argparse 하위 명령 등록부. 이후 Task들이 `register(subparsers)`로 붙임)
  - `harness.platform.find_exe(name: str, env_override: str | None = None) -> Path | None`(env_override 환경변수가 있으면 그 경로를 우선)
  - `harness.platform.is_windows() -> bool`
  - `harness.paths.repo_root(start: Path | None = None) -> Path`(`.git`이 있는 상위 폴더, 없으면 `HarnessError`)
  - `harness.paths.state_dir(root) -> Path`(`.harness/`), `handoff_dir(root, task_id) -> Path`(`.handoff/<id>/`), `worktree_dir(root, task_id) -> Path`(`root.parent/"javis-wt"/task_id`)
  - `harness.HarnessError(Exception)`
  - `tests/conftest.py` fixture `git_repo(tmp_path) -> Path`(git init + 첫 커밋된 임시 저장소)

- [ ] **Step 1: 테스트 작성**
```python
def test_version(capsys):
    assert main(["--version"]) == 0
    assert "harness" in capsys.readouterr().out

def test_find_exe_override(tmp_path, monkeypatch):
    fake = tmp_path / "fake tool.py"; fake.write_text("")
    monkeypatch.setenv("JAVIS_CLAUDE_BIN", str(fake))
    assert find_exe("claude", "JAVIS_CLAUDE_BIN") == fake

def test_find_exe_uses_which(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: r"C:\npm\codex.CMD")
    assert find_exe("codex").name.lower() == "codex.cmd"

def test_worktree_dir_outside_repo(git_repo):
    wt = worktree_dir(git_repo, "T001")
    assert wt.parent.name == "javis-wt" and git_repo not in wt.parents
```
- [ ] **Step 2: 실행해서 실패 확인**: `uv run pytest tests/test_cli.py tests/test_platform.py -q` → ImportError
- [ ] **Step 3: 구현.**
  - `pyproject.toml`: 프로젝트 이름 `javis-harness`, `requires-python = ">=3.12"`, `[project.scripts] harness = "harness.cli:main"`, dev 그룹에 pytest.
  - `.gitattributes`: `* text=auto eol=lf`.
  - `.gitignore`: `.env`, `.harness/`, `.handoff/`, `.task/`, `.claude/settings.local.json`, `__pycache__/`, `.venv/`.
  - `.env.example`: `JAVIS_BRAIN_DIR=`.
- [ ] **Step 4: CI 작성.** `.github/workflows/harness.yml`: matrix `[windows-latest, macos-latest]`, `astral-sh/setup-uv@v6`, `uv run pytest -q`.
- [ ] **Step 5: 통과 확인**: `uv run pytest -q` → PASS
- [ ] **Step 6: Commit**: `feat: harness skeleton, platform layer, CI matrix`

### Task 2: 역할 표

**Files:** Create `harness/roles.toml`, `harness/roles.py` · Test `tests/test_roles.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Role(name: str, tool: Literal["claude","codex"], model: str, effort: Literal["low","medium","high"])`
  - `load_roles(path: Path | None = None) -> dict[str, Role]`(키 예: `"codex.dev"`)
  - `lead_candidates(path=None) -> list[Role]`(순서 유지, name=`"lead"`)
  - `owner_of(rel_path: str, path=None) -> Literal["claude","codex"] | None`

- [ ] **Step 1: 테스트**
```python
def test_role_values():
    r = load_roles()
    assert r["codex.reviewer"] == Role("codex.reviewer", "codex", "gpt-6.2-sol", "high")
    assert r["claude.simple"].model == "claude-sonnet-5-5" and r["claude.simple"].effort == "low"

def test_lead_order():
    assert [c.model for c in lead_candidates()] == ["claude-fable-5-2", "gpt-6.2-astra"]

def test_invalid_effort_rejected(tmp_path):
    p = tmp_path / "r.toml"; p.write_text('[codex.dev]\ntool="codex"\nmodel="x"\neffort="ultra"\n')
    with pytest.raises(HarnessError, match="effort"): load_roles(p)

def test_owner_of():
    assert owner_of("hud/scene.ts") == "codex" and owner_of("brain/write.py") == "claude"
    assert owner_of("README.md") is None
```
- [ ] **Step 2: 실패 확인** → **Step 3: 구현**(tomllib, roles.toml은 Global Constraints 값을 그대로) → **Step 4: 통과 확인** → **Step 5: Commit** `feat: roles table`

### Task 3: 가드 (R1·R2·R4·R7, 순수 함수)

**Files:** Create `harness/guards.py` · Test `tests/test_guards.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Verdict(allowed: bool, rule: str | None, reason: str)`
  - `@dataclass(frozen=True) GuardContext(repo: Path, home: Path, brain_dir: Path | None, worktree: Path | None)`. `worktree`는 작업자로 실행 중일 때만 설정된다.
  - `GuardContext.from_env(repo: Path) -> GuardContext`(`JAVIS_BRAIN_DIR`, `JAVIS_WORKTREE` 읽음)
  - `check_path(path: Path, op: Literal["read","write"], ctx) -> Verdict`
  - `check_command(cmd: str, ctx) -> Verdict`
  - `@dataclass(frozen=True) Finding(rule: str, line: int, snippet: str)`; `scan_text(text: str) -> list[Finding]`

**규칙(정확한 값):**
- R1. 다음 경로는 읽기·쓰기를 모두 거부한다: `.env`·`.env.*`(단 `.env.example`은 허용), 저장소 안 `secrets/`, `brain_dir` 하위, `home/.claude`, `home/.codex`, `home/.ssh`. 경로는 `resolve()`한 뒤 비교하고, 대소문자를 무시한다(Windows).
- R2. `worktree`가 설정돼 있으면 그 밖으로 **쓰기**를 거부한다.
- R4. 다음 git 명령을 거부한다: `git commit`, `git push`, `git merge`, `git rebase`, `git reset --hard`, `--no-verify`. 단, 명령이 `harness merge` 또는 `uv run harness merge`로 시작하면 허용한다.
- R7. 다음 패턴을 찾아낸다: 이메일, `[A-Za-z]:\\Users\\[^\\\s<]+`, `/Users/[^/\s<]+/`, `/home/[^/\s<]+/`, `-----BEGIN [A-Z ]*PRIVATE KEY-----`, `sk-[A-Za-z0-9]{20,}`, `ghp_[A-Za-z0-9]{30,}`, `xox[bp]-`, `AKIA[0-9A-Z]{16}`. 같은 줄에 `harness:allow`가 있으면 그 줄은 예외로 둔다.

- [ ] **Step 1: 테스트**
```python
def test_env_blocked_example_allowed(ctx):
    assert not check_path(ctx.repo/".env", "read", ctx).allowed
    assert check_path(ctx.repo/".env.example", "read", ctx).allowed

def test_brain_and_home_blocked(ctx):
    assert check_path(ctx.brain_dir/"daily/x.md", "read", ctx).rule == "R1"
    assert check_path(ctx.home/".codex"/"auth.json", "read", ctx).rule == "R1"

def test_case_insensitive_env(ctx):
    assert not check_path(ctx.repo/".ENV", "write", ctx).allowed

def test_worker_write_outside_worktree(worker_ctx):
    assert check_path(worker_ctx.repo/"core/a.py", "write", worker_ctx).rule == "R2"
    assert check_path(worker_ctx.worktree/"core/a.py", "write", worker_ctx).allowed

@pytest.mark.parametrize("cmd", ["git commit -m x", "git push", "git -C . merge t", "git commit --no-verify"])
def test_git_blocked(cmd, ctx): assert check_command(cmd, ctx).rule == "R4"

def test_harness_merge_allowed(ctx): assert check_command("uv run harness merge T001", ctx).allowed

def test_scan_text():
    f = scan_text("path C:\\Users\\alice\\x\nok line\nme@example.com harness:allow")
    assert [x.line for x in f] == [1]
```
- [ ] **Step 2: 실패 확인** → **Step 3: 구현** → **Step 4: 통과 확인** → **Step 5: Commit** `feat: guard rules R1 R2 R4 R7`

### Task 4: 훅 연결 (Claude PreToolUse + git pre-commit)

**Files:** Create `harness/hooks.py`, `.claude/settings.json`, `.githooks/pre-commit` · Modify `harness/cli.py` · Test `tests/test_hooks.py`

**Interfaces:**
- Consumes: `check_path`, `check_command`, `scan_text`, `GuardContext.from_env`, `repo_root`
- Produces:
  - `claude_pre(payload: dict, ctx: GuardContext) -> tuple[int, str]`(허용 `(0,"")`, 차단 `(2, "<rule>: <reason>")`)
  - `git_pre_commit(repo: Path, env: Mapping[str,str]) -> tuple[int, str]`
  - CLI: `harness hook claude-pre`(stdin JSON → exit code, stderr에 이유), `harness hook git-pre-commit`, `harness hook scan-all`(추적 중인 파일 전체에 R7, CI용)

**정확한 동작:**
- `claude_pre`:
  - `tool_name`이 `Read|Write|Edit|MultiEdit|NotebookEdit`이면 `tool_input.file_path`를 check_path로 확인한다. `Write|Edit|...`은 op=write다.
  - `Bash`이면 `tool_input.command`를 check_command로 확인한다.
  - `Grep|Glob`이면 `tool_input.path`가 있을 때 read로 확인한다.
  - 그 외 도구는 허용한다.
- `git_pre_commit`:
  - 스테이징된 파일 경로를 R1로 확인한다.
  - `env.get("JAVIS_AGENT") == "1"`이고 `env.get("JAVIS_MERGE") != "1"`이면 R4로 거부한다.
  - 스테이징된 내용(`git diff --cached -U0`의 추가 줄)을 scan_text로 검사하고, 하나라도 걸리면 거부한다.
- `.claude/settings.json`:
  - `hooks.PreToolUse`: matcher는 `"Read|Write|Edit|MultiEdit|NotebookEdit|Bash|Grep|Glob"`, command는 `"uv run --quiet harness hook claude-pre"`.
  - `permissions.deny`: `["Read(./.env)", "Read(./.env.*)"]`(이중 방어).
- `.githooks/pre-commit`: `#!/bin/sh` 다음 줄에 `exec uv run --quiet harness hook git-pre-commit`.

- [ ] **Step 1: 테스트**
```python
def test_claude_pre_blocks_env(ctx):
    code, msg = claude_pre({"tool_name": "Read", "tool_input": {"file_path": str(ctx.repo/".env")}}, ctx)
    assert code == 2 and msg.startswith("R1")

def test_claude_pre_blocks_git_commit(ctx):
    assert claude_pre({"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}, ctx)[0] == 2

def test_claude_pre_allows_unknown_tool(ctx):
    assert claude_pre({"tool_name": "WebSearch", "tool_input": {}}, ctx) == (0, "")

def test_pre_commit_blocks_agent(git_repo):
    (git_repo/"a.txt").write_text("x"); run(["git","add","a.txt"], git_repo)
    assert git_pre_commit(git_repo, {"JAVIS_AGENT": "1"})[0] != 0
    assert git_pre_commit(git_repo, {"JAVIS_AGENT": "1", "JAVIS_MERGE": "1"})[0] == 0

def test_pre_commit_blocks_user_path(git_repo):
    (git_repo/"b.md").write_text("see /Users/bob/notes\n"); run(["git","add","b.md"], git_repo) <!-- harness:allow -->
    code, msg = git_pre_commit(git_repo, {}); assert code != 0 and "R7" in msg

def test_cli_claude_pre_stdin(monkeypatch, capsys, ctx_repo):  # stdin JSON → exit 2
    ...  # 위 env 케이스를 main(["hook","claude-pre"])로 한 번
```
- [ ] **Step 2: 실패 확인** → **Step 3: 구현** → **Step 4: 통과 확인**
- [ ] **Step 5: 수동 확인(이 PC)**: `git config core.hooksPath .githooks`를 설정한 뒤, 이 저장소에서 Claude Code 세션을 열고 `.env`를 Read하도록 요청한다. 차단 메시지 `R1:`이 보이면 통과다. 결과를 PR 설명에 적는다.
- [ ] **Step 6: Commit** `feat: claude and git hooks wired to guards`

### Task 5: 작업 상태 + ledger + `task new`

**Files:** Create `harness/tasks.py`, `harness/ledger.py`, `harness/templates/spec.md` · Modify `harness/cli.py` · Test `tests/test_tasks.py`, `tests/test_ledger.py`

**Interfaces:**
- Produces:
  - `ledger.append(root: Path, event: str, **fields) -> dict`: `.harness/ledger.jsonl`에 한 줄을 추가한다. 필드는 `ts`(ISO8601 UTC), `event`, 그리고 fields다. 반환값은 기록한 dict.
  - event 이름: `task.new`, `dispatch.start`, `dispatch.end`, `gate`, `merge`
  - `@dataclass Task(id: str, title: str, owner: Literal["claude","codex"], status: str, handoffs: int, branch: str, gate_tree: str | None)`
  - status 값: `new|running|done|blocked:quota|failed|gated|merged`
  - `tasks.new_task(root, title: str, owner) -> Task`: id는 `T` + 3자리 일련번호. `.handoff/<id>/spec.md`를 템플릿으로 만들고 `.harness/tasks/<id>.json`을 저장한다.
  - `tasks.load(root, task_id) -> Task`, `tasks.save(root, task: Task) -> None`(원자적 쓰기: 임시 파일 → `os.replace`)
  - `tasks.bump_handoff(root, task_id) -> Task`(4를 넘으면 `HarnessError("handoff limit 4")`)
  - CLI: `harness task new "<제목>" --owner claude|codex` → id를 출력
- 템플릿 `spec.md`의 섹션: `## 무엇`, `## 왜`, `## 건드릴 경로`, `## 금지`, `## 완료 기준`(안에 빈 ```` ```gate ```` 블록)

- [ ] **Step 1: 테스트**
```python
def test_new_task_ids(git_repo):
    a = new_task(git_repo, "로그 패널 만들기", "codex"); b = new_task(git_repo, "x", "claude")
    assert (a.id, b.id) == ("T001", "T002") and a.branch == "task/T001"
    assert "```gate" in (git_repo/".handoff/T001/spec.md").read_text(encoding="utf-8")

def test_handoff_limit(git_repo):
    t = new_task(git_repo, "x", "codex")
    for _ in range(4): bump_handoff(git_repo, t.id)
    with pytest.raises(HarnessError, match="handoff limit 4"): bump_handoff(git_repo, t.id)

def test_ledger_jsonl(git_repo):
    ledger.append(git_repo, "task.new", task="T001")
    line = json.loads((git_repo/".harness/ledger.jsonl").read_text().splitlines()[-1])
    assert line["event"] == "task.new" and line["ts"].endswith("Z")
```
- [ ] **Step 2: 실패 확인** → **Step 3: 구현**(모든 파일 I/O는 `encoding="utf-8"`) → **Step 4: 통과 확인** → **Step 5: Commit** `feat: task state, ledger, task new`

### Task 6: runner + `dispatch`

**Files:** Create `harness/runner.py`, `harness/dispatch.py`, `tests/fakes/fake_agent.py` · Modify `harness/cli.py` · Test `tests/test_runner.py`, `tests/test_dispatch.py`

**Interfaces:**
- Consumes: `Role`, `load_roles`, `find_exe`, `worktree_dir`, `handoff_dir`, tasks, ledger
- Produces:
  - `runner.safe_env(environ: Mapping[str,str], extra: Mapping[str,str]) -> dict[str,str]`
    - 허용 목록: `PATH, PATHEXT, HOME, USERPROFILE, USERNAME, USER, LOGNAME, APPDATA, LOCALAPPDATA, PROGRAMDATA, SYSTEMROOT, WINDIR, COMSPEC, TEMP, TMP, TMPDIR, LANG, LC_ALL, TERM, SHELL, XDG_CONFIG_HOME, XDG_DATA_HOME, CODEX_HOME`
    - 여기에 `JAVIS_*`와 extra를 더한다.
  - `runner.build_command(role: Role, worktree: Path) -> list[str]`(Global Constraints의 정확한 인자. 실행 파일은 `find_exe(tool, "JAVIS_<TOOL>_BIN")`)
  - `runner.run(cmd: list[str], prompt: str, cwd: Path, env: dict, timeout_s: int, log_path: Path) -> RunResult`
  - `@dataclass RunResult(exit_code: int, duration_s: float, quota_hit: bool, timed_out: bool)`
    - quota_hit 판정: 출력에 대소문자 무시로 `usage limit|rate limit|quota|too many requests` 중 하나가 있고, exit≠0
  - `runner.acquire_lock(root, tool) -> ContextManager`: `.harness/locks/<tool>.lock`에 PID를 기록한다. 기존 PID가 살아 있으면 `HarnessError("busy: <tool>")`, 죽어 있으면 회수한다.
  - `dispatch.dispatch(root, task_id: str, role_name: str, timeout_s: int = 1800) -> Task`
    1. worktree가 없으면 `git worktree add -b task/<id> <wt>`를 실행한다.
    2. 프롬프트를 만든다: "AGENTS.md를 따르라" + spec.md 전문 + "결과 요약을 `.task/result.md`에 써라. 커밋하지 마라."
    3. env를 만든다: `safe_env`에 extra로 `JAVIS_AGENT=1`, `JAVIS_WORKTREE=<wt>`, `JAVIS_TASK=<id>`를 넣는다.
    4. 실행은 lock 안에서 한다.
    5. 끝나면 `.task/result.md`를 `.handoff/<id>/result.md`로 복사한다.
    6. 이어서 bump_handoff, status 갱신(done / blocked:quota / failed), ledger `dispatch.start`/`dispatch.end`(role, tool, model, effort, exit, duration_s)를 기록한다.
  - CLI: `harness dispatch <id> --role <role> [--timeout 1800]`
- `fake_agent.py`: stdin을 읽어 `JAVIS_FAKE_MODE`에 따라 동작한다.
  - `ok`: cwd에 `.task/result.md`와 `hud/demo.txt`를 쓰고 exit 0
  - `quota`: "Usage limit reached"를 출력하고 exit 1
  - `slow`: sleep

- [ ] **Step 1: 테스트**
```python
def test_safe_env_drops_api_keys():
    e = safe_env({"PATH": "p", "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", "JAVIS_X": "1"}, {"JAVIS_AGENT": "1"})
    assert "ANTHROPIC_API_KEY" not in e and "OPENAI_API_KEY" not in e and e["JAVIS_AGENT"] == "1"

def test_build_codex_command(fake_bins, tmp_path):
    cmd = build_command(Role("codex.dev", "codex", "gpt-6.2-sol", "medium"), tmp_path)
    assert cmd[1:] == ["exec","-m","gpt-6.2-sol","-c",'model_reasoning_effort="medium"',
                       "--ignore-user-config","-C",str(tmp_path),"-s","workspace-write","-"]

def test_build_claude_command(fake_bins, tmp_path):
    cmd = build_command(Role("claude.dev","claude","claude-opus-5-5","medium"), tmp_path)
    assert cmd[1:] == ["-p","--model","claude-opus-5-5","--effort","medium",
                       "--setting-sources","project,local","--permission-mode","acceptEdits"]

def test_dispatch_ok(git_repo_with_harness, fake_bins, monkeypatch):
    monkeypatch.setenv("JAVIS_FAKE_MODE", "ok")
    t = new_task(git_repo_with_harness, "한글 제목 공백 포함", "codex")
    t = dispatch(git_repo_with_harness, t.id, "codex.dev")
    assert t.status == "done" and t.handoffs == 1
    assert (handoff_dir(git_repo_with_harness, t.id)/"result.md").exists()

def test_dispatch_quota(...):   # JAVIS_FAKE_MODE=quota → status == "blocked:quota", 재시도 없음(ledger에 dispatch.start 1개)
def test_stale_lock_reclaimed(...):  # 죽은 PID(예: 2**22) lock 파일 → acquire 성공
def test_busy_lock(...):             # 현재 PID lock → HarnessError("busy: codex")
```
- [ ] **Step 2: 실패 확인** → **Step 3: 구현**(`subprocess.run(cmd, input=prompt, text=True, encoding="utf-8", capture_output=True, timeout=…, shell=False)`. PID 생존 확인은 `platform.pid_alive(pid) -> bool`로 OS별 구현) → **Step 4: 통과 확인** → **Step 5: Commit** `feat: runner and dispatch`

### Task 7: `gate`

**Files:** Create `harness/gate.py` · Modify `harness/cli.py` · Test `tests/test_gate.py`

**Interfaces:**
- Consumes: tasks, ledger, `scan_text`, `worktree_dir`
- Produces:
  - `parse_gate_commands(spec_text: str) -> list[list[str]]`: ```` ```gate ```` 블록 안에서 빈 줄과 `#` 주석을 빼고 남은 각 줄을 `shlex.split(posix=True)` 한다. `\r\n`도 처리한다.
  - `run_gate(root, task_id, timeout_s: int = 600) -> GateResult`
  - `@dataclass GateResult(passed: bool, results: list[dict], tree: str)`
    - `results`의 각 항목: `{cmd, exit, duration_s}`
    - `tree`: worktree의 `git write-tree` 결과. 먼저 `git add -A`를 `JAVIS_MERGE=1` 없이 실행하되 커밋은 하지 않는다.
  - 판정 규칙. 다음 셋을 모두 만족해야 `passed`다.
    - 모든 명령이 exit 0
    - `git diff main...HEAD`와 작업 트리 변경의 추가 줄에 R7 탐지가 0건
    - 명령이 1개 이상(gate 블록이 비어 있으면 실패)
  - 결과는 `.handoff/<id>/gate.json`에 쓰고, task에 `status="gated"`(통과 시)와 `gate_tree`를 저장한다. ledger에 `gate` 이벤트를 남긴다.
  - CLI: `harness gate <id>` → 통과 시 exit 0, 실패 시 1, 표로 출력

- [ ] **Step 1: 테스트**
```python
def test_parse_crlf():
    spec = "## 완료 기준\r\n```gate\r\nuv run pytest -q\r\n# note\r\n\r\npython -c \"print(1)\"\r\n```\r\n"
    assert parse_gate_commands(spec) == [["uv","run","pytest","-q"], ["python","-c","print(1)"]]

def test_empty_gate_fails(dispatched_task): assert not run_gate(*dispatched_task).passed
def test_gate_pass(dispatched_task_with_gate_cmd_python_exit0): ...   # passed, gate.json 존재, status == "gated"
def test_gate_blocks_r7(...):  # worktree에 "/Users/bob/x" 줄 추가 → passed False, results에 R7 언급 <!-- harness:allow -->
```
- [ ] **Step 2: 실패 확인** → **Step 3: 구현** → **Step 4: 통과 확인** → **Step 5: Commit** `feat: gate`

### Task 8: `merge`

**Files:** Create `harness/merge.py` · Modify `harness/cli.py` · Test `tests/test_merge.py`

**Interfaces:**
- Consumes: tasks, ledger, gate 결과(`gate_tree`), `worktree_dir`, `handoff_dir`
- Produces:
  - `merge(root, task_id: str) -> str`: main에 만든 커밋 sha를 반환한다.
  - 사전 조건. 하나라도 어기면 `HarnessError`이고, 메시지에 이유가 들어간다.
    - (a) status가 `gated`
    - (b) 현재 worktree tree hash가 `gate_tree`와 같음(`"changed since gate"`)
    - (c) root가 main 브랜치이고, gitignore 대상을 제외하면 깨끗함(`"main not clean"`)
  - 절차. 모든 git 호출 env에 `JAVIS_MERGE=1`을 넣는다.
    1. worktree에서 `git add -A` 후 `git commit -m "wip(<id>)"`
    2. root에서 `git merge --squash task/<id>` 후 `git commit -m "<prefix>(<id>): <title>"`. prefix는 owner가 codex면 `feat(codex)`, claude면 `feat(claude)`
    3. `git worktree remove <wt>` 후 `git branch -D task/<id>`
    4. `JAVIS_BRAIN_DIR`가 있으면 `.handoff/<id>/`를 `<brain>/javis-dev/handoff/<id>/`로 복사(`shutil.copytree(dirs_exist_ok=True)`)
    5. status=`merged`, ledger `merge`(sha) 기록
  - CLI: `harness merge <id>`

- [ ] **Step 1: 테스트**
```python
def test_merge_happy(gated_task):
    sha = merge(*gated_task)
    log = run(["git","log","-1","--format=%s"], gated_task[0]).strip()
    assert log.startswith("feat(codex)(T001):") and not worktree_dir(*gated_task).exists()

def test_merge_rejects_changed_after_gate(gated_task):
    (worktree_dir(*gated_task)/"hud/new.txt").write_text("x")
    with pytest.raises(HarnessError, match="changed since gate"): merge(*gated_task)

def test_merge_rejects_dirty_main(gated_task):
    (gated_task[0]/"stray.txt").write_text("x")
    with pytest.raises(HarnessError, match="main not clean"): merge(*gated_task)

def test_merge_archives_handoff(gated_task, tmp_path, monkeypatch):
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(tmp_path/"brain")); merge(*gated_task)
    assert (tmp_path/"brain/javis-dev/handoff/T001/spec.md").exists()

def test_pre_commit_hook_allows_merge(gated_task_with_hooks_path): ...  # core.hooksPath=.githooks 상태에서 merge 성공
```
- [ ] **Step 2: 실패 확인** → **Step 3: 구현** → **Step 4: 통과 확인** → **Step 5: Commit** `feat: merge`

### Task 9: `doctor`

**Files:** Create `harness/doctor.py` · Modify `harness/cli.py` · Test `tests/test_doctor.py`

**Interfaces:**
- Consumes: `find_exe`, `lead_candidates`, `build_command`, `safe_env`, `repo_root`
- Produces:
  - `@dataclass Check(name: str, status: Literal["ok","warn","fail"], detail: str, fix: str | None)`
  - `run_checks(root, probe: bool = False) -> list[Check]`
  - `select_lead(root, probe_fn) -> Role | None`: `.harness/local.toml`에 `lead = "<tool>/<model>/<effort>"`를 쓴다. 환경변수 `JAVIS_LEAD=claude|codex`가 있으면 그 도구 후보를 강제한다.
  - `parse_version(text: str) -> tuple[int,int,int]`
  - CLI: `harness doctor [--probe] [--fix]` → fail이 하나라도 있으면 exit 1. 출력은 `✅/⚠/✗ 이름 — 설명 (고치는 법)`.
- 점검 항목(이름 고정):
  - `python>=3.12`, `uv`, `git`
  - `node`: 없으면 warn
  - `claude`: 있는지, `claude --version`
  - `codex>=0.171.0`: 미만이면 fail, fix=`npm i -g @openai/codex@latest`
  - `hooksPath`: 값이 `.githooks`가 아니면 fail. `--fix`면 `git config core.hooksPath .githooks`로 고침
  - `env-keys`: `.env.example`에 있는 키 이름이 `.env`나 환경변수에 있는지. 값은 출력하지 않음
  - `eol`: `.gitattributes` 존재
  - `lead`: `--probe`면 후보마다 짧은 실호출(프롬프트 "Reply with OK", timeout 90초, safe_env)을 해서 처음 성공한 것을 선택. 아니면 local.toml의 값을 표시하고, 없으면 warn "run doctor --probe"
- 이 PC 실측 기준: codex 0.164.0이면 fail이 정상이다.

- [ ] **Step 1: 테스트**
```python
def test_parse_version(): assert parse_version("codex-cli 0.164.0") == (0,164,0)
def test_codex_too_old(fake_bins_version_0_164, git_repo):
    c = {x.name: x for x in run_checks(git_repo)}["codex>=0.171.0"]
    assert c.status == "fail" and "npm i -g @openai/codex@latest" in c.fix
def test_select_lead_fallback(git_repo):
    r = select_lead(git_repo, probe_fn=lambda role: role.tool == "codex")
    assert r.model == "gpt-6.2-astra"
    assert 'lead = "codex/gpt-6.2-astra/high"' in (git_repo/".harness/local.toml").read_text()
def test_env_keys_no_values(git_repo, capsys): ...  # .env에 JAVIS_BRAIN_DIR=secretpath → 출력에 secretpath 없음
def test_fix_sets_hooks_path(git_repo): ...         # --fix 후 git config core.hooksPath == ".githooks"
```
- [ ] **Step 2: 실패 확인** → **Step 3: 구현** → **Step 4: 통과 확인** → **Step 5: Commit** `feat: doctor`

### Task 10: 지침 파일 + 전체 흐름 테스트 + 실제 CLI 확인

**Files:** Create `AGENTS.md`, `CLAUDE.md`, `README.md` · Test `tests/test_e2e.py`

**Interfaces:**
- Consumes: 모든 CLI 명령
- `AGENTS.md`: 100줄 이하로 쓴다. 섹션은 다음과 같다.
  - 목적 1단락
  - 역할과 소유 경로(roles.toml을 가리킴)
  - 작업 흐름 `task new → dispatch → gate → merge`
  - 금지 규칙 R1/R2/R4/R5/R7 한 줄씩(각 줄에 "만든 이유 / 만료 조건")
  - 작업자 규칙: 커밋 금지, 결과는 `.task/result.md`, 소유 경로 밖은 손대지 않기
  - 새 PC 설치: `git clone → uv run harness doctor --fix --probe`
- `CLAUDE.md`: 정확히 한 줄 `@AGENTS.md`
- `README.md`: 공개용 짧은 소개 + 설치 3줄. 개인 정보는 넣지 않는다.

- [ ] **Step 1: e2e 테스트 작성**: 임시 저장소에 하네스를 설치하고 가짜 실행 파일을 쓴다. CLI로 다음을 차례로 확인한다.
  - `task new`
  - spec의 gate 블록에 `python -c "import pathlib,sys; sys.exit(0 if pathlib.Path('hud/demo.txt').exists() else 1)"`를 넣음
  - `dispatch --role codex.dev`
  - `gate` 결과 exit 0
  - `merge` 후 main에 `hud/demo.txt`가 있음
  - ledger에 이벤트 5종이 순서대로 남음
- [ ] **Step 2: 실행** `uv run pytest -q` → 전부 PASS. `uv run harness hook scan-all` → 탐지 0건
- [ ] **Step 3: 실제 CLI 확인(이 PC, 수동)**
  - `uv run harness doctor`를 실행한다. codex가 0.164라서 fail이 나오는 게 정상이다.
  - 사용자 승인 후 codex를 업데이트하고 `doctor --probe`를 실행한다. lead가 astra로 선택되는지 확인한다.
  - 작은 실작업 1건(`hud/README.md` 한 줄 작성, codex.dev)을 dispatch → gate → merge까지 돌린다.
  - ledger에 실제 duration이 기록되는지 확인한다.
- [ ] **Step 4: Commit** `docs: AGENTS.md, CLAUDE.md, README; e2e test`
- [ ] **Step 5: push 전 사용자 확인**: 원격 push는 사용자가 허락한 뒤에 한다. CI가 windows와 macos 둘 다 초록인지 확인한다.
