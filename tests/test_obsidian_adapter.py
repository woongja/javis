import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from brain import append
from brain.obsidian import bridge
from brain.publish import pin_root
from core.approval import ApprovalService, _executor_identity, os_user
from tests.test_curate_execution import plan
from tests.test_curate_proposal import EXAMPLE, EXISTING, append as append_row, write
from worklog import Store


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "brain/obsidian/main.js"
NODE = shutil.which("node")
FAKE_API = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const Module = require("node:module");
class TFile {}
class FileSystemAdapter {}
const api = { TFile, FileSystemAdapter, apiVersion: "test", Plugin: class {}, Notice: class {} };
const load = Module._load;
Module._load = function(name, ...args) {
    return name === "obsidian" ? api : load.call(this, name, ...args);
};
"""


@pytest.mark.skipif(not NODE, reason="Node is needed for the fake editor")
def test_conditional_vault_process():
    script = FAKE_API + r"""
const { applyAppend } = require(process.argv[1]);
const hash = data => require("node:crypto").createHash("sha256").update(data).digest("hex");
async function exercise(mode, newline, bom) {
    const base = Buffer.from(bom + "# Note" + newline);
    const addition = Buffer.from("- [[javis/wiki/topics/Topic]]" + newline);
    const complete = Buffer.concat([base, addition]);
    const file = Object.assign(new TFile(), { path: "javis/wiki/index.md" });
    const request = { kind: "append", path: file.path, base_sha256: hash(base),
                      result_sha256: hash(complete), content: addition.toString() };
    const leaves = [];
    const open = () => leaves.push({ getViewState: () => ({ state: { file: file.path } }) });
    if (mode === "open") open();
    if (mode === "wrong result") request.result_sha256 = hash(base);
    let calls = 0, writes = 0, disk = base;
    const vault = {
        getAbstractFileByPath: path => path === file.path ? file : null,
        process: async (_, callback) => {
            calls++;
            if (mode === "open during read") open();
            if (mode === "edit") disk = Buffer.from("user edit");
            let current = disk.toString();
            if (mode === "lossy") current = current.slice(1);
            const next = callback(current);
            assert.equal(typeof next, "string");
            if (mode === "before publish") throw Error("interrupted");
            disk = Buffer.from(next);
            writes++;
            if (mode === "after publish") throw Error("interrupted");
        }
    };
    const workspace = { iterateAllLeaves: visit => leaves.forEach(visit) };
    const status = await applyAppend({ vault, workspace }, request, () => disk, () => true)
        .catch(() => "unknown");
    const expected = {
        open: "conflict", "open during read": "conflict", edit: "conflict", lossy: "rejected",
        "wrong result": "rejected", "before publish": "unknown", "after publish": "unknown", ok: "ok"
    };
    assert.equal(status, expected[mode]);
    assert.equal(calls, mode === "open" ? 0 : 1);
    const published = mode === "ok" || mode === "after publish";
    assert.equal(writes, published ? 1 : 0);
    assert.deepEqual(disk, published ? complete : mode === "edit" ? Buffer.from("user edit") : base);
}
(async () => {
    for (const nl of ["\n", "\r\n"]) for (const bom of ["", "\uFEFF"]) {
        for (const mode of ["open", "open during read", "edit", "lossy", "wrong result",
                            "before publish", "after publish", "ok"]) await exercise(mode, nl, bom);
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run([NODE, "-e", script, PLUGIN], check=True, timeout=10)


@pytest.mark.parametrize("change", ["hardlink", "symlink", "root", "parent", "policy", "invalid utf8", "executor"])
def test_callback_rechecks_disk(tmp_path, change):
    path = "javis/wiki/index.md"
    root = tmp_path / "vault"
    target = root / path
    target.parent.mkdir(parents=True)
    base = b"# Index\n"
    target.write_bytes(base)
    vault = pin_root(root)
    addition = "- [[javis/wiki/topics/Topic]]\n"
    pid, started = _executor_identity()
    guard = {"policy": "absent", "deadline": time.monotonic() + 30,
             "pid": pid, "started": started,
             "parents": [[p.stat().st_dev, p.stat().st_ino] for p in (root / "javis", target.parent)],
             "op": {"kind": "append", "path": path, "content": addition,
                    "file_id": [target.stat().st_dev, target.stat().st_ino],
                    "base_sha256": hashlib.sha256(base).hexdigest(),
                    "result_sha256": hashlib.sha256(base + addition.encode()).hexdigest()}}
    assert bridge.checked_base(vault, guard) == base
    if change == "hardlink":
        os.link(target, tmp_path / "alias")
    elif change == "symlink" and os.name == "nt":
        import _winapi
        outside = tmp_path / "outside"
        target.parent.rename(outside)
        _winapi.CreateJunction(str(outside), str(target.parent))
    elif change == "symlink":
        outside = tmp_path / "outside.md"
        target.rename(outside)
        target.symlink_to(outside)
    elif change in ("root", "parent"):
        directory = root if change == "root" else target.parent
        moved = tmp_path / "moved"
        directory.rename(moved)
        target.parent.mkdir(parents=True)
        (moved / target.relative_to(directory)).rename(target)  # Same inode and bytes.
    elif change == "policy":
        (root / "BRAIN.md").write_text("exclude: javis/wiki\n", encoding="utf8")
    elif change == "executor":
        guard["started"] = "different process"
    else:
        target.write_bytes(b"\xff")
    with pytest.raises((ValueError, OSError)) as exc:
        bridge.checked_base(vault, guard)
    assert not isinstance(exc.value, append.Conflict)
    assert target.read_bytes() == (b"\xff" if change == "invalid utf8" else base)


@pytest.mark.skipif(not NODE, reason="Node is needed for the fake editor")
def test_desktop_plugin_serves_one_authenticated_append(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    write(root, EXISTING, EXAMPLE)
    # The fake host runs the worktree's helper as if installed in the selected interpreter.
    script = FAKE_API + r"""
const children = require("node:child_process");
for (const method of ["spawn", "spawnSync"]) {
    const original = children[method];
    children[method] = (python, args, opts) => original(python,
        ["-I", "-B", "-u", "-c", process.env.TEST_BOOT, process.env.TEST_ROOT, ...args.slice(5)], opts);
}
const Plugin = require(process.argv[1]);
const root = process.env.JAVIS_BRAIN_DIR;
const adapter = Object.assign(new FileSystemAdapter(), { getBasePath: () => root,
    getFullPath: path => require("node:path").join(root, path) });
const file = Object.assign(new TFile(), { path: process.argv[2] });
const plugin = new Plugin();
const filename = require("node:path").join(root, file.path);
plugin.app = {
    vault: { adapter, getAbstractFileByPath: path => path === file.path ? file : null,
        async process(_, change) {
            const result = change(fs.readFileSync(filename, "utf8"));
            fs.writeFileSync(filename + ".tmp", result, { flag: "wx" });
            fs.renameSync(filename + ".tmp", filename);
            return result;
        }
    },
    workspace: { iterateAllLeaves() {}, onLayoutReady(fn) { fn(); } }
};
const profile = [process.platform, api.apiVersion, process.versions.node,
                 fs.statSync(root, { bigint: true }).dev].join("/");
process.env.JAVIS_OBSIDIAN_ATOMIC_PROFILE = "unqualified";
plugin.onload();
assert.equal(plugin.child, undefined); // An unqualified host must not advertise even a preview.
process.env.JAVIS_OBSIDIAN_ATOMIC_PROFILE = profile;
plugin.onload();
process.stdin.resume();
process.stdin.on("end", () => plugin.onunload());
"""
    with tempfile.TemporaryDirectory(dir=None if os.name == "nt" else "/tmp") as private:
        directory = Path(private)
        monkeypatch.setenv("JAVIS_DATA_DIR", private)
        monkeypatch.setenv("JAVIS_BRAIN_DIR", str(root))
        store = Store.open(directory)
        vault = pin_root(root)
        spec = plan(root, append_row(), sources=(EXISTING,))
        op = spec["ops"][0]
        owner = os_user()
        claim = ApprovalService(store).propose("brain.curate", json.dumps(spec), owner, "cli").id
        store.decide_approval(claim, actor=owner, channel="cli", approve=True, executor=_executor_identity())
        boot = "import sys, runpy; sys.path.insert(0, sys.argv.pop(1)); "
        try:
            append.registration(secrets.token_bytes(32))
        except FileNotFoundError as exc:
            if os.name != "nt" or exc.winerror != 2:
                raise
            # Profile-less Windows test account. Keep the real pipe DACL, peer PID and HMAC.
            boot += "from brain import append; append.registration = bytes; "
            monkeypatch.setattr(append, "_boot_key", lambda d: (d / "obsidian-append.key").read_bytes())
        boot += "runpy.run_module('brain.obsidian.bridge', run_name='__main__')"
        env = dict(os.environ, JAVIS_PYTHON=sys.executable, TEST_BOOT=boot, TEST_ROOT=str(ROOT))
        with subprocess.Popen([NODE, "-e", script, PLUGIN, EXISTING], env=env, stdin=subprocess.PIPE) as host:
            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        assert append.exchange(directory, vault) == "ok"
                        break
                    except append.Disconnected:
                        if host.poll() is not None or time.monotonic() >= deadline:
                            raise
                        time.sleep(0.025)
                for field, value in (("session", "0" * 64), ("approval", "other"),
                                     ("path", "javis/wiki/index.md")):
                    with monkeypatch.context() as patch:
                        request = append._request
                        patch.setattr(append, "_request", lambda *args: dict(request(*args), **{field: value}))
                        # A rejection before authentication has no session echo.
                        assert append.exchange(directory, vault, claim, op) == (
                            "unknown" if field == "session" else "rejected")
                    assert (root / EXISTING).read_bytes() == EXAMPLE
                assert append.exchange(directory, vault, claim, op) == "ok"
                assert append.exchange(directory, vault, claim, op) == "rejected"  # File was replaced.
                assert (root / EXISTING).read_bytes() == EXAMPLE + op["content"].encode()
            finally:
                host.stdin.close()
                try:
                    host.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    host.kill()
