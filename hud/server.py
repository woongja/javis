"""Loopback HTTP server for the local HUD."""

import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from threading import Lock
from time import monotonic
from urllib.parse import parse_qs

from core.approval import ApprovalService, is_interrupted
from core.config import EngineSettings
from core.engine import ClaudeEngine
from core.note import approval_summary, decide_on_pc
from core.recall import question_preview
from teams import registry
from worklog import Approval, Store

_COOKIE = "javis_hud_session"
_SESSION_SECONDS = 12 * 60 * 60
_MAX_ATTEMPTS = 5
_DECISION_ROUTE = re.compile(r"/api/approvals/([0-9a-f]{32})/(approve|reject)")
_CSP = "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
_DECISION_CODES = {
    "done": 200, "rejected": 200, "not_found": 404, "forbidden": 403,
    "already_decided": 409, "expired": 410, "executor_missing": 503,
    "failed": 500, "unknown": 500,
}


@dataclass(frozen=True)
class _Session:
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    expires_at: float


def _pending(store: Store, actor: str) -> list[dict]:
    # Store.list_approvals also writes expirations. This snapshot only reads them.
    now = datetime.now(UTC)
    with store._connect() as db:
        rows = db.execute(
            """
            SELECT * FROM approvals WHERE status IN ('pending', 'executing')
            ORDER BY status DESC, created_at, id
            """
        ).fetchall()
    previews = []
    for row in rows:
        approval = Approval(**dict(row))
        if not approval.can_decide(actor, "cli"):
            continue
        if approval.status == "pending":
            if datetime.fromisoformat(approval.expires_at) <= now:
                continue
        elif not is_interrupted(approval):
            continue
        previews.append({
            "id": approval.id,
            "kind": approval.kind,
            "status": approval.status,
            "expires_at": approval.expires_at,
            "summary": approval_summary(approval),
        })
    return previews


class HUDServer(ThreadingHTTPServer):
    # Let an in-flight decision finish before returning from server_close().
    daemon_threads = False

    def __init__(self, store: Store, actor: str, service: ApprovalService):
        self.store = store
        self.actor = actor
        self.service = service
        self.teams = registry(ClaudeEngine(EngineSettings()))
        self.html = files("hud").joinpath("index.html").read_bytes()
        self.nonce: str | None = secrets.token_urlsafe(32)
        self._session: _Session | None = None
        self._lock = Lock()
        self._attempts = 0
        super().__init__(("127.0.0.1", 0), _Handler)
        self.authority = f"127.0.0.1:{self.server_port}"
        self.origin = f"http://{self.authority}"

    def run(self) -> None:
        print(self.origin, flush=True)
        print(f"One-time code: {self.nonce}", flush=True)
        self.serve_forever()

    def server_close(self) -> None:
        with self._lock:
            self.nonce = None
            self._session = None
        super().server_close()

    def handle_error(self, request, client_address) -> None:
        # Request diagnostics must not echo caller-supplied credentials.
        pass

    def exchange(self, candidate: str) -> _Session | None:
        with self._lock:
            if self.nonce is None:
                return None
            if _matches(candidate, self.nonce):
                session = _Session(
                    secrets.token_urlsafe(32), secrets.token_urlsafe(32), monotonic() + _SESSION_SECONDS,
                )
                self.nonce = None
                self._session = session
                return session
            self._attempts += 1
            if self._attempts >= _MAX_ATTEMPTS:
                self.nonce = None
            return None

    def authenticate(self, token: str) -> _Session | None:
        with self._lock:
            session = self._session
            if session and session.expires_at > monotonic() and _matches(token, session.token):
                return session
            return None

    def state(self) -> dict:
        records = []
        for row in self.store.recent(20):
            records.append({
                "id": row.id,
                "created_at": row.created_at,
                "kind": row.kind,
                "status": row.status,
                "question_preview": question_preview(row.request) if row.kind in ("ask", "recall") else "",
            })
        teams = {}
        for route, team in self.teams.items():
            available, reason = team.available()
            teams[route] = {"available": available, "reason": reason}
        return {"records": records, "teams": teams, "pending": _pending(self.store, self.actor)}


def _matches(candidate: str, expected: str) -> bool:
    return candidate.isascii() and secrets.compare_digest(candidate, expected)


class _Handler(BaseHTTPRequestHandler):
    server: HUDServer
    timeout = 5
    default_request_version = "HTTP/1.0"

    def log_message(self, format, *args) -> None:
        pass

    def send_error(self, code, message=None, explain=None) -> None:
        self._json(code, {"status": "request_error"})

    def _respond(self, code: int, body: bytes, content_type: str, **headers: str) -> None:
        # Include headers even when rejecting a legacy HTTP/0.9 request.
        if self.request_version == "HTTP/0.9":
            self.request_version = self.protocol_version
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", _CSP)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict, **headers: str) -> None:
        self._respond(code, json.dumps(payload).encode("utf-8"), "application/json", **headers)

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        if self.headers.get_all("Host") != [self.server.authority]:
            self._json(421, {"status": "invalid_host"})
            return False
        parts = self.requestline.split()
        if len(parts) != 3 or self.request_version not in ("HTTP/1.0", "HTTP/1.1"):
            self._json(505, {"status": "unsupported_http_version"})
            return False
        # BaseHTTPRequestHandler normalizes leading double slashes.
        if parts[1] != self.path:
            self._json(404, {"status": "not_found"})
            return False
        method = {"/": "GET", "/api/state": "GET", "/session": "POST"}.get(self.path)
        self.decision_route = _DECISION_ROUTE.fullmatch(self.path)
        if self.decision_route is not None:
            method = "POST"
        if method is None:
            self._json(404, {"status": "not_found"})
            return False
        if self.command != method:
            self._json(405, {"status": "method_not_allowed"}, Allow=method)
            return False
        return True

    def _session(self) -> _Session | None:
        headers = self.headers.get_all("Cookie", [])
        if len(headers) == 1:
            try:
                cookie = SimpleCookie(headers[0]).get(_COOKIE)
            except CookieError:
                return None
            if cookie is not None:
                return self.server.authenticate(cookie.value)
        return None

    def _length(self) -> int | None:
        if "Transfer-Encoding" in self.headers:
            return None
        lengths = self.headers.get_all("Content-Length", ["0"])
        if len(lengths) == 1 and re.fullmatch(r"[0-9]{1,4}", lengths[0]):
            return int(lengths[0])
        return None

    def _nonce(self, length: int) -> str:
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                return ""
            body = raw.decode("utf-8")
            content_type = self.headers.get_content_type()
            if content_type == "application/json":
                payload = json.loads(body)
                value = payload.get("nonce") if isinstance(payload, dict) else None
                return value if isinstance(value, str) else ""
            if content_type == "application/x-www-form-urlencoded":
                values = parse_qs(body).get("nonce", [])
                if len(values) == 1:
                    return values[0]
        except (ValueError, RecursionError):
            pass
        return ""

    def do_GET(self) -> None:
        if self.path == "/":
            self._respond(200, self.server.html, "text/html; charset=utf-8")
        elif self._session() is None:
            self._json(401, {"status": "session_required"})
        else:
            try:
                state = self.server.state()
            except (OSError, sqlite3.Error):
                self._json(500, {"status": "unavailable"})
                return
            self._json(200, state)

    def do_POST(self) -> None:
        if self.headers.get_all("Origin") != [self.server.origin]:
            self._json(403, {"status": "invalid_origin"})
            return
        if self.path == "/session":
            length = self._length()
            if length is None or not 0 < length <= 1024:
                self._json(400, {"status": "invalid_body"})
                return
            session = self.server.exchange(self._nonce(length))
            if session is None:
                self._json(403, {"status": "invalid_nonce"})
                return
            cookie = f"{_COOKIE}={session.token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={_SESSION_SECONDS}"
            self._json(200, {"csrf_token": session.csrf_token}, **{"Set-Cookie": cookie})
            return

        session = self._session()
        if session is None:
            self._json(401, {"status": "session_required"})
            return
        tokens = self.headers.get_all("X-CSRF-Token", [])
        if len(tokens) != 1 or not _matches(tokens[0], session.csrf_token):
            self._json(403, {"status": "invalid_csrf"})
            return
        if self._length() != 0:
            self._json(400, {"status": "invalid_body"})
            return
        assert self.decision_route is not None
        id, action = self.decision_route.groups()
        try:
            status, message = decide_on_pc(
                self.server.store, self.server.service, id, self.server.actor, approve=action == "approve",
            )
        except (OSError, sqlite3.Error):
            self._json(500, {"id": id, "status": "unavailable"})
            return
        self._json(_DECISION_CODES[status], {"id": id, "status": status, "message": message})
