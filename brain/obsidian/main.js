"use strict";

const { spawn, spawnSync } = require("child_process");
const { createHash } = require("crypto");
const { existsSync, statSync } = require("fs");
const { delimiter, isAbsolute, join, resolve } = require("path");
const { createInterface } = require("readline");
const { apiVersion, FileSystemAdapter, Notice, Plugin, TFile } = require("obsidian");

class Conflict extends Error {}
class Rejected extends Error {}

const hash = bytes => createHash("sha256").update(bytes).digest("hex");

function javisPython() {
    if (process.env.JAVIS_PYTHON) return process.env.JAVIS_PYTHON;
    const suffix = process.platform === "win32" ? ".exe" : "";
    for (const dir of (process.env.PATH || "").split(delimiter).filter(isAbsolute)) {
        const python = join(dir, "python" + suffix);
        if (existsSync(join(dir, "javis" + suffix)) && existsSync(python)) return python;
    }
}

// process() serializes editor changes. Whole-file publication on interruption is
// a separate storage property, qualified on a disposable vault on the same volume.
function publicationProfile(path) {
    if (!apiVersion) throw new Rejected();
    const device = statSync(path, { bigint: true }).dev;
    return [process.platform, apiVersion, process.versions.electron || process.versions.node, device].join("/");
}

// serve_session has authenticated the wire request and selected this exact stored op.
function validOperation(op) {
    return op?.kind === "append" && typeof op.path === "string" &&
        typeof op.content === "string" && op.content.length > 0 &&
        /^[a-f0-9]{64}$/.test(op.base_sha256) && /^[a-f0-9]{64}$/.test(op.result_sha256);
}

// disk is an independently checked snapshot taken inside the process callback.
function appendedText(op, current, disk) {
    if (typeof current !== "string" || disk.toString("utf8") !== current ||
        !Buffer.from(current, "utf8").equals(disk)) {
        throw new Rejected();
    }
    if (hash(disk) !== op.base_sha256) throw new Conflict();
    const addition = Buffer.from(op.content, "utf8");
    if (addition.toString("utf8") !== op.content) throw new Rejected();
    const result = Buffer.concat([disk, addition]);
    const text = result.toString("utf8");
    if (!Buffer.from(text, "utf8").equals(result) || hash(result) !== op.result_sha256) {
        throw new Rejected();
    }
    return text;
}

async function applyAppend(app, op, check, active) {
    if (!validOperation(op)) return "rejected";
    const { vault, workspace } = app;
    const file = vault.getAbstractFileByPath(op.path);
    function checkTarget() {
        if (!active() || !(file instanceof TFile) || file.path !== op.path ||
            vault.getAbstractFileByPath(op.path) !== file) throw new Rejected();
        workspace.iterateAllLeaves(leaf => {
            // Inactive/deferred Markdown tabs may have view state but no loaded file.
            if (leaf.view?.file === file || leaf.view?.file?.path === file.path ||
                leaf.getViewState().state?.file === file.path) throw new Conflict();
        });
    }
    let returnedText = false;
    try {
        checkTarget();
        await vault.process(file, current => {
            checkTarget();
            const text = appendedText(op, current, check());
            returnedText = true;
            return text;
        });
        return returnedText ? "ok" : "rejected";
    } catch (error) {
        if (returnedText) throw error; // Publication may have finished. Drop the reply.
        if (error instanceof Conflict) return "conflict";
        return "rejected";
    }
}

class JavisAdapter extends Plugin {
    onload() {
        this.app.workspace.onLayoutReady(() => this.start());
    }

    start() {
        const { vault, workspace } = this.app;
        const python = javisPython();
        if (this.stopped) return;
        if (!python || !isAbsolute(python) || !(vault.adapter instanceof FileSystemAdapter) ||
            typeof vault.process !== "function" || typeof workspace.iterateAllLeaves !== "function") {
            new Notice("javis 추가 기능 미연결 · 로컬 vault와 javis Python 설정을 확인하세요.");
            return;
        }
        const root = vault.adapter.getBasePath();
        let profile;
        try {
            profile = publicationProfile(root);
        } catch {
            new Notice("javis 추가 기능 미연결 · 저장 볼륨을 확인할 수 없습니다.");
            return;
        }
        if (profile !== process.env.JAVIS_OBSIDIAN_ATOMIC_PROFILE) {
            new Notice("javis 추가 기능 미연결 · 임시 vault에서 완성본 게시를 확인할 환경 " + profile);
            return;
        }
        const args = ["-I", "-B", "-u", "-m", "brain.obsidian.bridge"];
        const child = spawn(python, [...args, "serve", root], {
            stdio: ["pipe", "pipe", "ignore"], windowsHide: true
        });
        this.child = child;
        const active = () => this.child === child && !child.killed && child.exitCode === null;
        const check = (guard, path) => {
            const target = vault.adapter.getFullPath(path);
            if (resolve(target) !== resolve(root, path) || publicationProfile(target) !== profile) {
                throw new Rejected();
            }
            const result = spawnSync(python, [...args, "check", root], {
                input: guard, timeout: 2500, maxBuffer: 64 * 1024, windowsHide: true
            });
            if (!result.error && result.status === 2) throw new Conflict();
            if (result.error || result.status !== 0 || !active()) throw new Rejected();
            return result.stdout;
        };
        createInterface({ input: child.stdout }).on("line", async line => {
            try {
                const { ticket, guard } = JSON.parse(line);
                const { op } = JSON.parse(guard);
                // An unknown publication closes just this IPC session in serve_session.
                const status = await applyAppend(this.app, op, () => check(guard, op.path), active)
                    .catch(() => null);
                if (status === "conflict") new Notice("저장 후 닫고 다시 제안");
                if (active()) child.stdin.write(JSON.stringify({ ticket, status }) + "\n");
            } catch {
                child.kill(); // The executor reconciles a lost reply against the on-disk hash.
            }
        });
        child.once("error", () => child.kill());
        child.stdin.on("error", () => child.kill());
        child.once("close", () => {
            if (this.child === child) {
                this.child = null;
                new Notice("javis 추가 기능 미연결 · 설정을 확인하고 플러그인을 다시 켜세요.");
            }
        });
    }

    onunload() {
        this.stopped = true;
        const child = this.child;
        this.child = null;
        if (child) child.kill();
    }
}

module.exports = JavisAdapter;
module.exports.appendedText = appendedText;
module.exports.applyAppend = applyAppend;
