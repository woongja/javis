# 로컬 HUD

`javis hud`를 실행하고 터미널에 표시된 URL을 연다. 서버는 자동으로 선택한
포트에 바인딩하며 `Host`는 `127.0.0.1:<port>`만 허용한다. 페이지에 일회용
코드를 입력한다. 코드 입력을 5회 실패하면 서버를 다시 시작해야 한다. 세션은
12시간 동안 유효하며 Ctrl+C로 서버를 종료하면 코드와 세션이 폐기된다.

- `POST /session`은 폼 또는 JSON의 `nonce`를 받아 세션 쿠키와 JSON
  `csrf_token`을 발급한다. 두 POST 경로 모두 리슨 URL과 일치하는
  `Origin` 헤더가 필요하다.
- `GET /api/state`는 세션 쿠키를 확인한 뒤 최근 일지 20건의 공개 필드
  (`records`), 팀 사용 가능 상태(`teams`), 승인 미리보기(`pending`)를
  반환한다. 승인 미리보기는 CLI와 같은 PC 권한을 적용하며, 요약에는
  메모의 대상 경로나 가져오기 목적지와 복사 예정 건수 및 바이트 수를 담는다.
- `POST /api/approvals/<ID>/approve`와 `POST /api/approvals/<ID>/reject`는
  세션 쿠키와 `X-CSRF-Token`이 필요하다. 요청 본문은 비워 둔다.
  결과의 `status`는 완료 시 `done`, 거절 시 `rejected`, 이미 결정된
  경우 `already_decided`(409), 만료된 경우 `expired`(410)다.
