import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from threading import Thread
from urllib.parse import urlencode

import pytest

from core.approval import ApprovalService, os_user
from core.note import NOTE_EXECUTORS, note_path
from hud.server import HUDServer
from worklog import Item, Record, Store


@contextmanager
def running_server(store, actor):
    with HUDServer(store, actor, ApprovalService(store, NOTE_EXECUTORS)) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def request(server, method, path, *, headers=None, body=None):
    connection = HTTPConnection(*server.server_address, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert not any(key.lower().startswith("access-control-") for key in response.headers.keys())
        return response.status, response.headers, data
    finally:
        connection.close()


def exchange(server, nonce, *, headers=None, form=False):
    data = {"nonce": nonce}
    origin = {"Origin": server.origin} if headers is None else headers
    return request(
        server, "POST", "/session",
        headers={
            **origin,
            "Content-Type": "application/x-www-form-urlencoded" if form else "application/json",
        },
        body=urlencode(data) if form else json.dumps(data),
    )


def login(server, *, form=False):
    status, headers, data = exchange(server, server.nonce, form=form)
    assert status == 200
    [cookie] = SimpleCookie(headers["Set-Cookie"]).values()
    assert cookie["httponly"] and cookie["samesite"] == "Strict" and cookie["path"] == "/"
    assert int(cookie["max-age"]) == 12 * 3600
    return {
        "Cookie": f"{cookie.key}={cookie.value}",
        "Origin": server.origin,
        "X-CSRF-Token": json.loads(data)["csrf_token"],
    }


@pytest.fixture
def hud(tmp_path, monkeypatch):
    data = tmp_path / "data"
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("JAVIS_DATA_DIR", str(data))
    monkeypatch.setenv("JAVIS_BRAIN_DIR", str(vault))
    store = Store.open(data)
    actor = os_user()
    with running_server(store, actor) as server:
        yield server, store, actor, vault


def test_hosts_routes_and_authentication(hud):
    server, store, actor, vault = hud
    approval = ApprovalService(store).propose("note.create", "draft", actor, "cli")
    path = f"/api/approvals/{approval.id}/approve"
    credentials = login(server, form=True)
    other_port = 1 if server.server_port != 1 else 2
    for host in (f"127.0.0.1:{other_port}", f"localhost:{server.server_port}"):
        assert request(server, "GET", "/", headers={"Host": host})[0] == 421
        assert request(server, "POST", path, headers={**credentials, "Host": host})[0] == 421
    assert request(server, "GET", "/api/state")[0] == 401
    assert request(server, "POST", path, headers={"Origin": server.origin})[0] == 401
    code, headers, _ = request(server, "GET", path, headers=credentials)
    assert code == 405 and headers["Allow"] == "POST"
    for arbitrary in ("/../", "/hud/server.py", "/%2e%2e/", "//", "/?name=value"):
        assert request(server, "GET", arbitrary)[0] == 404
    code, headers, body = request(server, "GET", "/")
    assert code == 200 and b'<form method="post" action="/session">' in body
    assert headers["Content-Security-Policy"] == (
        "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
    )
    assert store.get_approval(approval.id) == approval
    assert not list(vault.iterdir())


def test_nonce_origin_and_attempt_limit(hud):
    server, _, _, _ = hud
    nonce = server.nonce
    for origin in ({}, {"Origin": server.origin + ".invalid"}):
        code, headers, _ = exchange(server, nonce, headers=origin)
        assert code == 403 and "Set-Cookie" not in headers
    for index in range(5):
        code, headers, _ = exchange(server, f"wrong-{index}")
        assert code == 403 and "Set-Cookie" not in headers
        assert server.nonce == (nonce if index < 4 else None)
    code, headers, _ = exchange(server, nonce)
    assert code == 403 and "Set-Cookie" not in headers


def test_nonce_single_use_session_expiry_and_logs(hud, capsys, monkeypatch):
    server, _, _, _ = hud
    nonce = server.nonce
    credentials = login(server)
    code, headers, _ = exchange(server, nonce)
    assert code == 403 and "Set-Cookie" not in headers
    assert request(server, "GET", "/api/state", headers=credentials)[0] == 200
    cookie_value = credentials["Cookie"].partition("=")[2]
    assert request(server, "GET", f"/{cookie_value}")[0] == 404
    assert request(server, "GET", f"/{nonce}")[0] == 404
    monkeypatch.setattr("hud.server.monotonic", lambda: float("inf"))
    assert request(server, "GET", "/api/state", headers=credentials)[0] == 401
    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_approval_credentials_and_one_time_execution(hud):
    server, store, actor, vault = hud
    approval = ApprovalService(store).propose("note.create", "save once", actor, "cli")
    path = f"/api/approvals/{approval.id}/approve"
    credentials = login(server)
    with running_server(store, actor) as other:
        other_token = login(other)["X-CSRF-Token"]
    invalid = [
        {key: value for key, value in credentials.items() if key != "Origin"},
        {**credentials, "Origin": f"http://localhost:{server.server_port}"},
        {key: value for key, value in credentials.items() if key != "X-CSRF-Token"},
        {**credentials, "X-CSRF-Token": other_token},
    ]
    for headers in invalid:
        assert request(server, "POST", path, headers=headers)[0] == 403
    assert request(server, "POST", path, headers=credentials, body="unexpected")[0] == 400
    assert store.get_approval(approval.id) == approval
    assert not list(vault.iterdir())

    code, _, data = request(server, "POST", path, headers=credentials)
    assert code == 200 and json.loads(data)["status"] == "done"
    target = vault / note_path(approval)
    assert target.read_text(encoding="utf-8") == "save once"
    code, _, data = request(server, "POST", path, headers=credentials)
    assert code == 409 and json.loads(data)["status"] == "already_decided"
    assert list(vault.rglob("*.md")) == [target]
    assert store.get_approval(approval.id).status == "done"


def test_state_is_read_only_and_exposes_only_public_fields(hud):
    server, store, actor, vault = hud
    service = ApprovalService(store)
    now = datetime.now(UTC)
    note = service.propose("note.create", "NOTE_BODY_MARKER", actor, "cli")
    manifest = {"dest": "archive", "files": [
        {"src": "IMPORT_MANIFEST_MARKER.md", "size": 12, "status": "copy"},
    ]}
    imported = service.propose("brain.import", manifest, actor, "cli")
    wrong_channel = service.propose("brain.import", manifest, actor, "telegram")
    wrong_owner = service.propose("note.create", "private", "other-owner", "cli")
    expired = service.propose("note.create", "expired draft", actor, "cli", now - timedelta(days=2))
    phone = service.propose("note.create", "phone draft", "chat", "telegram")
    for kind, question in (("ask", "web question"), ("local", "LOCAL_REQUEST_MARKER")):
        store.save(Record(
            None, now.isoformat(), "cli", kind, question, (Item("PRIVATE_RECORD_MARKER", ()),),
            "ok", None, None, None, None, None, None,
        ))
    credentials = login(server)
    code, _, data = request(server, "GET", "/api/state", headers=credentials)
    assert code == 200
    assert all(marker not in data for marker in (
        b"NOTE_BODY_MARKER", b"IMPORT_MANIFEST_MARKER", b"LOCAL_REQUEST_MARKER", b"PRIVATE_RECORD_MARKER",
    ))
    state = json.loads(data)
    assert [row["question_preview"] for row in state["records"]] == ["", "web question"]
    assert all(set(row) == {"id", "created_at", "kind", "status", "question_preview"} for row in state["records"])
    assert state["teams"] == {route: {"available": True, "reason": ""} for route in ("/web", "/note", "/brain")}
    previews = {row["id"]: row for row in state["pending"]}
    assert previews.keys() == {note.id, imported.id, phone.id}
    assert previews[note.id]["summary"] == note_path(note)
    assert previews[imported.id]["summary"] == "archive (복사 예정 1건 · 12바이트)"
    assert store.get_approval(expired.id) == expired

    for approval, action, expected in (
        (wrong_channel, "approve", (403, "forbidden")),
        (wrong_owner, "reject", (403, "forbidden")),
        (expired, "approve", (410, "expired")),
        (phone, "reject", (200, "rejected")),
    ):
        code, _, data = request(server, "POST", f"/api/approvals/{approval.id}/{action}", headers=credentials)
        assert (code, json.loads(data)["status"]) == expected
    assert store.get_approval(wrong_channel.id) == wrong_channel
    assert store.get_approval(wrong_owner.id) == wrong_owner
    assert store.get_approval(expired.id).status == "expired"
    assert store.get_approval(phone.id).status == "rejected"
    assert not list(vault.iterdir())
