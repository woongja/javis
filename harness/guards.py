"""Guard rules as pure functions. Hooks (Claude, git) call these.

R1  secrets / brain / tool homes: no read, no write.
    why: credentials and the private brain must never reach a dev agent. expires: never.
    exception: the lead session (no worktree) may use ~/.claude/projects/*/memory/.
R2  a dispatched worker writes only inside its own worktree.
    why: teams run side by side; stray writes collide. expires: never.
R4  raw git commit/push/merge/rebase/reset --hard/--no-verify are blocked; use `harness merge`.
    why: only the lead commits, after gate. expires: when merge rules move server-side.
    exception: the lead session (no worktree) may run a single plain git push to origin.
R7  personal data / secret patterns in text (pre-commit, gate, CI).
    why: the code repo becomes public. expires: never.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from harness.paths import env_path

ALLOW_MARKER = "harness:allow"


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    rule: str | None
    reason: str


@dataclass(frozen=True)
class GuardContext:
    repo: Path
    home: Path
    brain_dir: Path | None
    worktree: Path | None

    @classmethod
    def from_env(cls, repo: Path) -> "GuardContext":
        worktree = os.environ.get("JAVIS_WORKTREE")
        return cls(
            repo=repo,
            home=Path.home(),
            brain_dir=env_path(repo, "JAVIS_BRAIN_DIR"),
            worktree=Path(worktree) if worktree else None,
        )


@dataclass(frozen=True)
class Finding:
    rule: str
    line: int
    snippet: str


_OK = Verdict(True, None, "")


def _key(p: Path) -> tuple[str, ...]:
    return tuple(part.casefold() for part in p.resolve().parts)


def _under(path: Path, base: Path) -> bool:
    child, parent = _key(path), _key(base)
    return child[: len(parent)] == parent


def _absolute(path: Path, ctx: GuardContext) -> Path:
    if path.is_absolute():
        return path
    return (ctx.worktree or ctx.repo) / path


def _is_agent_memory(target: Path, home: Path) -> bool:
    """~/.claude/projects/<project>/memory/** (Claude Code auto-memory)."""
    rel, base = _key(target), _key(home / ".claude" / "projects")
    return rel[: len(base)] == base and len(rel) > len(base) + 2 and rel[len(base) + 1] == "memory"


def check_path(path: Path, op: Literal["read", "write"], ctx: GuardContext) -> Verdict:
    target = _absolute(Path(path), ctx)
    name = target.name.rstrip(" .").casefold()
    if (name == ".env" or name.startswith(".env.")) and name != ".env.example":
        return Verdict(False, "R1", f"{target.name} holds secrets")
    if ctx.worktree is None and _is_agent_memory(target, ctx.home):
        return _OK  # the lead session keeps its own notes; workers (worktree set) never do
    protected = [(ctx.home / d, f"~/{d}") for d in (".claude", ".codex", ".ssh")]
    for root in filter(None, (ctx.repo, ctx.worktree)):
        protected.append((root / "secrets", "secrets/"))
    if ctx.brain_dir:
        protected.append((ctx.brain_dir, "brain vault"))
    for base, label in protected:
        if _under(target, base):
            return Verdict(False, "R1", f"{label} is off limits")
    if op == "write" and ctx.worktree and not _under(target, ctx.worktree):
        return Verdict(False, "R2", "workers write only inside their worktree")
    return _OK


_MERGE_ONLY = re.compile(r"^\s*(uv\s+run\s+(--\S+\s+)*)?harness\s+merge\s+\S+\s*$")
_PLAIN_PUSH = re.compile(
    r"[ \t]*git[ \t]+push"
    r"(?:(?:[ \t]+-u)?[ \t]+origin[ \t]+[A-Za-z0-9_][A-Za-z0-9_./-]*|[ \t]+origin)?"
    r"[ \t]*"
)
_GIT_FORBIDDEN = [
    re.compile(r"\bgit\b[^;&|]*?\s(commit|push|merge|rebase)\b"),
    re.compile(r"\bgit\b[^;&|]*?\sreset\s+[^;&|]*--hard"),
    re.compile(r"--no-verify\b"),
]


def check_command(cmd: str, ctx: GuardContext) -> Verdict:
    if _MERGE_ONLY.match(cmd):
        return _OK
    if ctx.worktree is None and _PLAIN_PUSH.fullmatch(cmd):
        return _OK
    for pattern in _GIT_FORBIDDEN:
        if pattern.search(cmd):
            return Verdict(False, "R4", "only `harness merge` may commit/push/merge")
    return _OK


def check_command_paths(cmd: str, ctx: GuardContext, cwd: Path) -> Verdict:
    # Token checks are heuristic; OS ACLs are the primary defense.
    for match in re.finditer(r'''"[^"]*"|'[^']*'|[^\s"';&|<>]+''', cmd):
        path = Path(match.group().strip("\"'"))
        if path.parts[:1] == ("~",):
            path = ctx.home.joinpath(*path.parts[1:])
        verdict = check_path(cwd / path, "read", ctx)
        if not verdict.allowed:
            return verdict
    return _OK


_SAFE_EMAIL_DOMAIN = re.compile(r"@(example\.(com|org|net)|[\w.-]+\.(invalid|test))$", re.I)
_PATTERNS = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")),
    ("windows user path", re.compile(r"[A-Za-z]:\\Users\\[^\\\s<]+")),
    ("mac user path", re.compile(r"/Users/[^/\s<]+/")),  # harness:allow
    ("linux home path", re.compile(r"/home/[^/\s<]+/")),  # harness:allow
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("api key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("github token", re.compile(r"\bghp_[A-Za-z0-9]{30,}")),
    ("slack token", re.compile(r"\bxox[bp]-")),
    ("aws key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
]


def _line_finding(line: str) -> str | None:
    for label, pattern in _PATTERNS:
        for match in pattern.finditer(line):
            if label == "email" and _SAFE_EMAIL_DOMAIN.search(match.group(0)):
                continue
            return label  # never echo the matched value (it may be a secret)
    return None


def scan_text(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        label = _line_finding(line)
        if label:
            findings.append(Finding("R7", number, f"{label} on line {number}"))
    return findings


def added_lines(diff_text: str) -> list[str]:
    """Added content lines of a unified diff, read hunk by hunk (so '++x' content is kept)."""
    lines, in_hunk = [], False
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            in_hunk = True
        elif line.startswith("diff --git"):
            in_hunk = False
        elif in_hunk and line.startswith("+"):
            lines.append(line[1:])
    return lines
