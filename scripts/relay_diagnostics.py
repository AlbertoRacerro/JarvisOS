#!/usr/bin/env python3
"""Bounded, read-only Linux Relay liveness evidence; never cancel or retry work.

Only process metadata and explicitly selected journal metadata are inspected.
CPU activity and changed metadata are observations, not proof of useful work.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from itertools import pairwise
from pathlib import Path
from typing import Any

IO_FIELDS = ("rchar", "wchar", "read_bytes", "write_bytes")


def identity(pid: int, proc_root: Path = Path("/proc")) -> dict[str, Any] | None:
    try:
        fields = (proc_root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "ppid": int(fields[1]), "state": fields[0],
                "start_ticks": int(fields[19]), "cpu_ticks": int(fields[11]) + int(fields[12])}
    except (OSError, ValueError, IndexError):
        return None


def process(pid: int, proc_root: Path = Path("/proc"), deadline: float = float("inf")) -> dict[str, Any] | None:
    row = identity(pid, proc_root)
    if row is None:
        return None
    folder = proc_root / str(pid)
    row.update({"io": {}, "fds": {}, "threads": [], "children": []})
    try:
        for line in (folder / "io").read_text().splitlines():
            name, value = line.split(":", 1)
            if name in IO_FIELDS:
                row["io"][name] = int(value)
    except (OSError, ValueError):
        pass
    for fd in ("0", "1", "2"):
        try:
            row["fds"][fd] = os.readlink(folder / "fd" / fd)
        except OSError:
            row["fds"][fd] = None
    try:
        tasks = list((folder / "task").iterdir())
    except OSError:
        tasks = []
    for task in tasks:
        if time.monotonic() >= deadline:
            row["incomplete"] = True
            break
        thread = identity(int(task.name), folder / "task")
        if thread is None:
            continue
        try:
            thread["wchan"] = (task / "wchan").read_text().strip()
        except OSError:
            thread["wchan"] = None
        row["threads"].append(thread)
        try:
            row["children"].extend(int(value) for value in (task / "children").read_text().split())
        except (OSError, ValueError):
            pass
    row["children"] = sorted(set(row["children"]))
    # Do not combine metadata from a reused PID or a process that exited mid-read.
    after = identity(pid, proc_root)
    return row if after and after["start_ticks"] == row["start_ticks"] else None


def journal_metadata(path: Path) -> dict[str, Any]:
    row: dict[str, Any] = {"path": str(path)}
    try:
        stat = path.stat()
        row.update({"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                    "device": stat.st_dev, "inode": stat.st_ino})
    except OSError:
        row["exists"] = False
    return row


def snapshot(pid: int, journals: list[Path], proc_root: Path = Path("/proc"),
             deadline: float = float("inf")) -> dict[str, Any]:
    started = time.monotonic()
    root = process(pid, proc_root, deadline)
    descendants = []
    seen = {pid}
    pending = list(root["children"]) if root else []
    while pending and time.monotonic() < deadline:
        child = pending.pop()
        if child in seen:
            continue
        seen.add(child)
        row = process(child, proc_root, deadline)
        if row:
            descendants.append(row)
            pending.extend(row["children"])
    ancestry = []
    parent = root["ppid"] if root else 0
    while parent > 0 and parent not in seen and len(ancestry) < 32 and time.monotonic() < deadline:
        seen.add(parent)
        row = identity(parent, proc_root)
        if row is None:
            break
        ancestry.append(row)
        parent = row["ppid"]
    metadata = []
    for path in journals:
        if time.monotonic() >= deadline:
            break
        metadata.append(journal_metadata(path))
    return {"monotonic_s": started, "root": root, "ancestry": ancestry,
            "descendants": descendants, "journals": metadata,
            "incomplete": time.monotonic() >= deadline or bool(root and root.get("incomplete"))}


def delta(before: dict[str, Any], after: dict[str, Any], seconds: float, hertz: int) -> dict[str, Any]:
    same = before["pid"] == after["pid"] and before["start_ticks"] == after["start_ticks"]
    if not same:
        return {"pid": after["pid"], "identity_changed": True}
    io = {key: max(0, after["io"][key] - before["io"][key])
          for key in IO_FIELDS if key in before["io"] and key in after["io"]}
    old_threads = {(row["pid"], row["start_ticks"]): row for row in before["threads"]}
    threads = []
    for row in after["threads"]:
        old = old_threads.get((row["pid"], row["start_ticks"]))
        threads.append({"tid": row["pid"], "start_ticks": row["start_ticks"], "state": row["state"],
                        "wchan": row["wchan"], "cpu_percent": round(
                            100 * max(0, row["cpu_ticks"] - old["cpu_ticks"]) / hertz / seconds, 3
                        ) if old else None})
    ticks = max(0, after["cpu_ticks"] - before["cpu_ticks"])
    return {"pid": after["pid"], "start_ticks": after["start_ticks"], "cpu_ticks_delta": ticks,
            "cpu_percent": round(100 * ticks / hertz / seconds, 3), "io_delta": io, "threads": threads}


def compare(before: dict[str, Any], after: dict[str, Any], hertz: int) -> dict[str, Any]:
    seconds = max(after["monotonic_s"] - before["monotonic_s"], 0.000001)
    old, new = before["root"], after["root"]
    result: dict[str, Any] = {"interval_s": round(seconds, 6), "root_delta": None, "descendant_deltas": [],
                              "changed_journals": [], "new_descendants": []}
    if old is None or new is None or (old["pid"], old["start_ticks"]) != (new["pid"], new["start_ticks"]):
        return dict(result, classification="exited_or_identity_changed")
    result["root_delta"] = delta(old, new, seconds, hertz)
    old_children = {(row["pid"], row["start_ticks"]): row for row in before["descendants"]}
    active = False
    for row in after["descendants"]:
        previous = old_children.get((row["pid"], row["start_ticks"]))
        if previous is None:
            result["new_descendants"].append({"pid": row["pid"], "start_ticks": row["start_ticks"]})
            active = True
        else:
            change = delta(previous, row, seconds, hertz)
            result["descendant_deltas"].append(change)
            active |= change["cpu_ticks_delta"] > 0 or any(change["io_delta"].values())
    old_journals = {row["path"]: row for row in before["journals"]}
    for row in after["journals"]:
        if row["exists"] and old_journals.get(row["path"]) != row:
            result["changed_journals"].append(row["path"])
    result["classification"] = (
        "observed_journal_progress" if result["changed_journals"] else
        "observed_descendant_activity" if active else "no_observed_progress_requires_investigation"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--journal", type=Path, action="append", default=[])
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--samples", type=int, default=2)
    args = parser.parse_args(argv)
    if args.pid <= 0 or not 0.1 <= args.interval <= 10 or not 2 <= args.samples <= 6:
        parser.error("PID must be positive; interval must be 0.1..10; samples must be 2..6")
    if not Path("/proc/self/stat").exists():
        parser.error("Linux /proc is required")
    started = time.monotonic()
    deadline = started + 60
    snapshots = [snapshot(args.pid, args.journal, deadline=deadline)]
    for _ in range(args.samples - 1):
        remaining = deadline - time.monotonic()
        if remaining <= args.interval:
            break
        time.sleep(args.interval)
        snapshots.append(snapshot(args.pid, args.journal, deadline=deadline))
    hertz = os.sysconf("SC_CLK_TCK")
    output = {"schema": "relay_diagnostics.v1", "pid": args.pid, "hertz": hertz,
              "elapsed_s": round(time.monotonic() - started, 6), "snapshots": snapshots,
              "intervals": [compare(a, b, hertz) for a, b in pairwise(snapshots)],
              "sampling_deadline_s": 60,
              "limits": "Metadata only. Activity does not prove useful progress; no observed progress does not prove a stall. "
                        "CPU alone never authorizes termination. No signals, retries, or recovery actions are performed. "
                        "Sampling stops at its deadline; individual kernel/filesystem reads can be delayed externally."}
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
