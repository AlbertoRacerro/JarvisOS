from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def _lock_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        locking = msvcrt.locking  # type: ignore[attr-defined]
        lk_nblck = msvcrt.LK_NBLCK  # type: ignore[attr-defined]
        try:
            locking(handle.fileno(), lk_nblck, 1)
        except OSError:
            handle.close()
            return None
    else:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
    return handle


def _unlock_file(handle) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            locking = msvcrt.locking  # type: ignore[attr-defined]
            lk_unlck = msvcrt.LK_UNLCK  # type: ignore[attr-defined]
            locking(handle.fileno(), lk_unlck, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def main() -> int:
    if len(sys.argv) != 6:
        return 2

    lock_path = Path(sys.argv[1])
    ready_path = Path(sys.argv[2])
    script_path = Path(sys.argv[3])
    input_file = Path(sys.argv[4])
    output_dir = Path(sys.argv[5])

    lock_handle = _lock_file(lock_path)
    if lock_handle is None:
        return 3

    try:
        ready_path.write_text("ready", encoding="utf-8")
        if sys.stdin.readline() != "run\n":
            return 4

        sys.argv = [str(script_path), str(input_file), str(output_dir)]
        sys.path[0] = str(script_path.parent)
        runpy.run_path(str(script_path), run_name="__main__")
        return 0
    finally:
        ready_path.unlink(missing_ok=True)
        _unlock_file(lock_handle)


if __name__ == "__main__":
    raise SystemExit(main())
