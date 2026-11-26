"""Safe bounded Linux page-cache prewarm helper (spec 384).

Uses posix_fadvise(POSIX_FADV_WILLNEED) to ask the kernel to populate page cache
without mlocking beyond available headroom. Always respects a safety floor so
default-model host-RAM experts are never starved.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Default safety floor: 10 GiB of MemAvailable preserved for host-RAM experts
DEFAULT_SAFETY_FLOOR_BYTES: int = 10 * 1024 * 1024 * 1024


def get_mem_available_bytes(meminfo_path: str | Path = "/proc/meminfo") -> int | None:
    """Read MemAvailable from /proc/meminfo in bytes. Return None if unreadable."""
    p = Path(meminfo_path)
    if not p.is_file():
        return None
    try:
        content = p.read_text(encoding="utf-8")
        for line in content.splitlines():
            if line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) >= 2:
                    kb_val = int(parts[1])
                    return kb_val * 1024
    except (OSError, ValueError):
        pass
    return None


def can_prewarm(
    file_path: str | Path,
    *,
    safety_floor_bytes: int = DEFAULT_SAFETY_FLOOR_BYTES,
    meminfo_path: str | Path = "/proc/meminfo",
) -> tuple[bool, str]:
    """Check whether file can be safely prewarmed into page cache without starving RAM headroom.

    Returns (can_prewarm, reason).
    """
    p = Path(file_path)
    if not p.is_file():
        return False, "file_not_found"

    if not hasattr(os, "posix_fadvise") or not hasattr(os, "POSIX_FADV_WILLNEED"):
        return False, "posix_fadvise_unavailable"

    try:
        file_size = p.stat().st_size
    except OSError as err:
        return False, f"stat_error:{err}"

    mem_avail = get_mem_available_bytes(meminfo_path)
    if mem_avail is None:
        return False, "meminfo_unavailable"

    headroom_after = mem_avail - file_size
    if headroom_after < safety_floor_bytes:
        return (
            False,
            f"insufficient_ram_headroom: available={mem_avail}, file_size={file_size}, floor={safety_floor_bytes}",
        )

    return True, "ok"


def prewarm_model_file(
    file_path: str | Path,
    *,
    safety_floor_bytes: int = DEFAULT_SAFETY_FLOOR_BYTES,
    meminfo_path: str | Path = "/proc/meminfo",
) -> bool:
    """Synchronously request page-cache prewarm via posix_fadvise if headroom is safe.

    Returns True if prewarm was initiated, False if refused or failed.
    """
    allowed, reason = can_prewarm(
        file_path, safety_floor_bytes=safety_floor_bytes, meminfo_path=meminfo_path
    )
    if not allowed:
        logger.debug("Page cache prewarm refused for %s: %s", file_path, reason)
        return False

    p = Path(file_path)
    try:
        fd = os.open(p, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_WILLNEED)
            logger.info("Initiated page-cache prewarm for %s", file_path)
            return True
        finally:
            os.close(fd)
    except OSError as err:
        logger.warning("Failed to posix_fadvise on %s: %s", file_path, err)
        return False


def prewarm_model_file_async(
    file_path: str | Path,
    *,
    safety_floor_bytes: int = DEFAULT_SAFETY_FLOOR_BYTES,
    meminfo_path: str | Path = "/proc/meminfo",
) -> threading.Thread | None:
    """Non-blocking background page-cache prewarm helper.

    Safely no-ops on missing files, non-Linux, or insufficient headroom.
    """
    p = Path(file_path)
    if not p.is_file():
        return None

    def _worker() -> None:
        try:
            prewarm_model_file(
                file_path,
                safety_floor_bytes=safety_floor_bytes,
                meminfo_path=meminfo_path,
            )
        except Exception as exc:
            logger.debug("Background prewarm exception for %s: %s", file_path, exc)

    thread = threading.Thread(
        target=_worker,
        name=f"page_cache_prewarm_{p.name[:24]}",
        daemon=True,
    )
    thread.start()
    return thread
