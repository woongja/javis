# javis

A personal AI assistant with a 3D HUD, an Obsidian second brain, and pluggable agent teams —
built by Claude Code and Codex working under a small, portable development harness.

## Status
Phase 1: the development harness (`harness/`). The assistant itself comes next.

## Quick start (Windows or macOS)
```
git clone https://github.com/woongja/javis.git && cd javis
uv run harness doctor --fix --probe
uv run pytest -q
```

How agents work in this repo: see [AGENTS.md](AGENTS.md).
