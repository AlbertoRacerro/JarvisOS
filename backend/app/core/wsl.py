"""Small WSL detection helper for backend features that hand files to Windows."""

from __future__ import annotations

import contextlib
from pathlib import Path


def running_under_wsl() -> bool:
    with contextlib.suppress(OSError):
        return "microsoft" in Path("/proc/version").read_text().lower()
    return False
