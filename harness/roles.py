"""Load the role table (harness/roles.toml)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from harness import HarnessError

Tool = Literal["claude", "codex"]
Effort = Literal["low", "medium", "high"]
_TOOLS = ("claude", "codex")
_EFFORTS = ("low", "medium", "high")
DEFAULT_PATH = Path(__file__).with_name("roles.toml")


@dataclass(frozen=True)
class Role:
    name: str
    tool: Tool
    model: str
    effort: Effort


def _read(path: Path | None) -> dict:
    try:
        return tomllib.loads((path or DEFAULT_PATH).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HarnessError(f"cannot read roles table: {exc}") from exc


def _make_role(name: str, spec: dict) -> Role:
    tool, model, effort = spec.get("tool"), spec.get("model"), spec.get("effort")
    if tool not in _TOOLS:
        raise HarnessError(f"role {name}: tool must be one of {_TOOLS}, got {tool!r}")
    if effort not in _EFFORTS:
        raise HarnessError(f"role {name}: effort must be one of {_EFFORTS}, got {effort!r}")
    if not isinstance(model, str) or not model:
        raise HarnessError(f"role {name}: model is required")
    return Role(name, tool, model, effort)


def load_roles(path: Path | None = None) -> dict[str, Role]:
    data = _read(path)
    roles: dict[str, Role] = {}
    for team, members in data.items():
        if team in ("lead", "ownership"):
            continue
        for role, spec in members.items():
            name = f"{team}.{role}"
            roles[name] = _make_role(name, spec)
    return roles


def lead_candidates(path: Path | None = None) -> list[Role]:
    return [_make_role("lead", c) for c in _read(path).get("lead", {}).get("candidates", [])]


def owner_of(rel_path: str, path: Path | None = None) -> Tool | None:
    normalized = rel_path.replace("\\", "/")
    for team, prefixes in _read(path).get("ownership", {}).items():
        if any(normalized.startswith(p) for p in prefixes):
            return team
    return None
