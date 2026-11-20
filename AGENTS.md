# javis — agent instructions

javis is a personal AI assistant (3D HUD + Obsidian second brain + pluggable teams).
It is built by a lead plus a Claude team and a Codex team. This file is the only
instruction source; `CLAUDE.md` just imports it. Keep it under 100 lines.

## Roles
- Models and efforts: `harness/roles.toml` (single source; do not restate them here).
- The **lead** is the interactive session a human opens. `uv run harness doctor --probe`
  says which tool to open it in. The lead writes specs (what / why / done-criteria),
  dispatches, judges gates, and is the only one who merges. The lead does not write the "how".
- Ownership: Claude team = `core/ brain/ security/ channels/telegram/`;
  Codex team = `hud/ worklog/ workshop/ teams/`. `harness review` enforces opposite-team review.
- On Windows, dispatch code tasks to Codex workers only; use Claude workers on macOS/Linux.
  Claude Code sandbox `denyRead` is unsupported on native Windows, so a Claude worker's shell can read the brain vault.

## Workflow (one task)
1. `uv run harness task new "<title>" --owner claude|codex` → fill `.handoff/<id>/spec.md`,
   including the ```gate block (one command per line, all must exit 0).
2. `uv run harness dispatch <id> --role <team.role>` → worker runs in `../javis-wt/<id>`.
3. `uv run harness review <id>` → runs the opposite-team reviewer in read-only mode and writes `.handoff/<id>/review.md`.
4. `uv run harness gate <id>` → commands + personal-data scan. Opinions do not pass a gate.
   Changes touching `harness/`, `security/`, `.githooks/`, `.github/`, `.claude/`, or `AGENTS.md`, or changing 50 or more lines, require an opposite-team `VERDICT: pass` for the same tree.
5. `uv run harness merge <id>` → squash commit on main (lead only).
   Merge refuses if main has changed any of the same files since the task base.
- Usage: `uv run harness usage [--by team|model|role|task]`.
- At most 4 handoffs per task. One run per tool at a time.
- A run that hits a usage limit is marked `blocked:quota` and is not retried automatically.

## Rules enforced by code (do not rely on memory)
| Rule | What | Why / expires |
|---|---|---|
| R1 | No read/write of `.env*` (except `.env.example`), `secrets/`, the brain vault, `~/.claude`, `~/.codex`, `~/.ssh` | secrets and the private brain never reach dev agents / never |
| R2 | A worker writes only inside its own worktree | parallel teams collide otherwise / never |
| R3 | A task changes only the paths in its spec's "건드릴 경로" section (fallback: roles.toml ownership) | out-of-spec changes slip in without review / never |
| R4 | No raw `git commit/push/merge/rebase/reset --hard/--no-verify`; use `harness merge` (lead may run plain `git push` to origin, no force or delete) | only the lead commits, after gate / when merges move server-side |
| R5 | Child processes get an allow-listed environment (no API keys) | subscription billing only, no secret leaks / never |
| R7 | Commits and gates reject personal data and secret patterns (`harness:allow` marks intentional lines) | this repo becomes public / never |

- Claude workers use the PreToolUse hook (`harness hook claude-pre`) and sandbox `denyRead` for the brain vault (macOS/Linux only).
- Codex workers use the PreToolUse hook (`harness hook codex-pre`), which fails closed on hook errors. `harness dispatch` passes `--dangerously-bypass-hook-trust`.
  An OS ACL denies Codex sandbox accounts access to the brain folder (`harness doctor` → `brain-isolation`).
- Shell-path checks are heuristic. OS isolation is the enforcement boundary.

Add a rule only after a real failure, and write why it exists and when it may be removed.

## Worker rules
- Do not commit. Write a short summary (what changed, how you checked) to `.task/result.md`.
- Stay inside your ownership paths. Ask the lead (via result.md) instead of touching others.
- Do not invent personal data; never paste real names, paths, or keys into code.
- Keep test temp files and caches out of the worktree (e.g. point pytest --basetemp at a directory under system temp) because the lead cannot remove sandbox-created artifacts from the worktree.

## New machine
```
git clone <repo> && cd javis
uv run harness doctor --fix --probe
```
Then fill `.env` from `.env.example` (`JAVIS_BRAIN_DIR` = path to the private brain vault).

## Docs
- Design: `docs/superpowers/specs/2026-11-20-harness-design.md`
- Plans: `docs/superpowers/plans/`
