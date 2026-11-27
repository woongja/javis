# Javis Adapter

승인 미리보기에 표시한 줄을 닫힌 노트 한 개에 추가하는 데스크톱 플러그인이다.

1. T061이 포함된 javis를 설치한다. 플러그인은 PATH에서 javis와 같은 디렉터리의 Python을 찾는다. 직접 지정하려면 저장소에서 `uv run python -c "import sys; print(sys.executable)"`로 확인한 절대 경로를 `JAVIS_PYTHON`으로 설정한다.
2. Obsidian을 시작할 환경에 CLI와 같은 `JAVIS_BRAIN_DIR` 및 `JAVIS_DATA_DIR` 설정을 사용한다. POSIX의 데이터 디렉터리는 현재 사용자 소유이며 권한은 `0700`이어야 한다.
3. 대상 vault의 `.obsidian/plugins/javis-adapter/`에 `manifest.json`과 `main.js`를 복사한다. Obsidian을 다시 시작하고 **설정 → 커뮤니티 플러그인 → Javis Adapter**를 직접 켠다.

플러그인은 설치된 javis의 `brain.obsidian.bridge`를 실행해 IPC 인증과 승인 조회를 처리한다. 데스크톱 로컬 파일 어댑터의 `Vault.process`가 편집을 직렬화한다. [Vault API](https://docs.obsidian.md/Plugins/Vault)

추가 기능은 기본적으로 꺼져 있다. 같은 볼륨의 임시 vault에서 해당 Obsidian 버전의 게시 도중 중단해도 원본 또는 완성본만 남는 환경을 인수한 뒤, 플러그인 안내에 표시된 환경 식별값을 `JAVIS_OBSIDIAN_ATOMIC_PROFILE`에 설정하고 Obsidian을 다시 시작한다. 이 값은 OS·Obsidian API·Electron 버전·볼륨을 묶으며, 환경이 달라지면 리스너가 열리지 않는다.

열린 노트나 기준 내용이 바뀐 노트는 저장 후 닫고 다시 제안한다. 응답 도중 연결이 끊기면 CLI가 파일 해시로 결과를 대조한다. 미연결인 경우 Python 설정과 vault 경로를 확인한 뒤 플러그인을 다시 켠다.
