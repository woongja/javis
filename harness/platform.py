"""The only place that branches on the operating system."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def is_windows() -> bool:
    return sys.platform == "win32"


def find_exe(name: str, env_override: str | None = None) -> Path | None:
    """Locate an executable. An override env var (e.g. JAVIS_CODEX_BIN) wins.

    On Windows shutil.which resolves npm `.cmd` shims, so callers can run the
    returned path with shell=False.
    """
    if env_override:
        override = os.environ.get(env_override)
        if override:
            return Path(override)
    found = shutil.which(name)
    return Path(found) if found else None
