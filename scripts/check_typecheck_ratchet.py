#!/usr/bin/env python3
"""Fail-closed per-file mypy debt ratchet for the backend runtime package."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
DEFAULT_BASELINE = BACKEND_ROOT / "mypy-baseline.json"
SCHEMA_VERSION = 1
ERROR_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?::(?P<column>\d+))?: error: ")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class RatchetError(RuntimeError):
    pass


def _normalize_path(raw: str) -> str:
    value = raw.replace("\\", "/")
    marker = "/backend/"
    if marker in value:
        value = value.split(marker, 1)[1]
    elif value.startswith("backend/"):
        value = value[len("backend/") :]
    while value.startswith("./"):
        value = value[2:]
    path = PurePosixPath(value)
    normalized = path.as_posix()
    if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
        raise RatchetError(f"mypy diagnostic escaped backend scope: {raw!r}")
    return normalized


def parse_error_counts(output: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for line in output.splitlines():
        match = ERROR_RE.match(line)
        if match:
            counts[_normalize_path(match.group("path"))] += 1
        elif " error: " in line:
            raise RatchetError(f"unparseable mypy error diagnostic: {line!r}")
    return dict(sorted(counts.items()))


def _validate_counts(value: Any, *, label: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise RatchetError(f"{label} files must be an object")
    result: dict[str, int] = {}
    previous = ""
    for raw_path, raw_count in value.items():
        if not isinstance(raw_path, str):
            raise RatchetError(f"{label} path is not a string")
        path = _normalize_path(raw_path)
        if path != raw_path:
            raise RatchetError(f"{label} path is not normalized: {raw_path!r}")
        if previous and path <= previous:
            raise RatchetError(f"{label} file map must be strictly sorted")
        previous = path
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count <= 0:
            raise RatchetError(f"{label} count for {path!r} must be a positive integer")
        result[path] = raw_count
    return result


def validate_baseline(data: Any, *, label: str = "baseline") -> tuple[str, str, dict[str, int]]:
    if not isinstance(data, dict):
        raise RatchetError(f"{label} must be a JSON object")
    if set(data) != {"schema_version", "source_sha", "mypy_version", "files"}:
        raise RatchetError(f"{label} has unexpected or missing keys")
    if data["schema_version"] != SCHEMA_VERSION:
        raise RatchetError(f"{label} schema_version must be {SCHEMA_VERSION}")
    source_sha = data["source_sha"]
    mypy_version = data["mypy_version"]
    if not isinstance(source_sha, str) or not SHA_RE.fullmatch(source_sha):
        raise RatchetError(f"{label} source_sha must be an exact lowercase 40-char SHA")
    if not isinstance(mypy_version, str) or not mypy_version:
        raise RatchetError(f"{label} mypy_version must be non-empty")
    files = _validate_counts(data["files"], label=label)
    return source_sha, mypy_version, files


def load_baseline(path: Path) -> tuple[str, str, dict[str, int]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RatchetError(f"baseline is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RatchetError(f"cannot read baseline {path}: {exc}") from exc
    return validate_baseline(data)


def _git_show_baseline(base_sha: str) -> tuple[str, str, dict[str, int]] | None:
    if not SHA_RE.fullmatch(base_sha):
        raise RatchetError("--base-sha must be an exact lowercase 40-char SHA")
    proc = subprocess.run(
        ["git", "show", f"{base_sha}:backend/mypy-baseline.json"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        missing_markers = ("does not exist", "exists on disk, but not in", "Path '")
        if any(marker in proc.stderr for marker in missing_markers):
            return None
        raise RatchetError(f"cannot read base baseline at {base_sha}: {proc.stderr.strip()}")
    try:
        return validate_baseline(json.loads(proc.stdout), label="base baseline")
    except json.JSONDecodeError as exc:
        raise RatchetError(f"base baseline is invalid JSON: {exc}") from exc


def compare_counts(current: dict[str, int], allowed: dict[str, int]) -> list[tuple[str, int, int]]:
    regressions: list[tuple[str, int, int]] = []
    for path, count in sorted(current.items()):
        limit = allowed.get(path, 0)
        if count > limit:
            regressions.append((path, limit, count))
    return regressions


def reject_baseline_increases(proposed: dict[str, int], parent: dict[str, int]) -> list[tuple[str, int, int]]:
    increases: list[tuple[str, int, int]] = []
    for path, count in sorted(proposed.items()):
        previous = parent.get(path, 0)
        if count > previous:
            increases.append((path, previous, count))
    return increases


def _mypy_version() -> str:
    try:
        return importlib.metadata.version("mypy")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RatchetError("mypy is not installed") from exc


def run_mypy() -> tuple[dict[str, int], str]:
    command = [
        sys.executable,
        "-m",
        "mypy",
        "app",
        "--no-pretty",
        "--no-error-summary",
        "--show-error-codes",
    ]
    proc = subprocess.run(
        command,
        cwd=BACKEND_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise RatchetError(f"mypy invocation failed with exit {proc.returncode}:\n{proc.stdout}")
    return parse_error_counts(proc.stdout), proc.stdout


def baseline_json(source_sha: str, mypy_version: str, counts: dict[str, int]) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_sha": source_sha,
        "mypy_version": mypy_version,
        "files": dict(sorted((path, count) for path, count in counts.items() if count > 0)),
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def self_test() -> None:
    sample = (
        "app/a.py:10: error: bad [assignment]\n"
        "app/a.py:12:3: error: worse [arg-type]\n"
        "app/a.py:12: note: context\n"
        "C:\\repo\\backend\\app\\b.py:7: error: bad [name-defined]\n"
    )
    assert parse_error_counts(sample) == {"app/a.py": 2, "app/b.py": 1}
    assert compare_counts({"app/a.py": 2}, {"app/a.py": 2}) == []
    assert compare_counts({"app/a.py": 3}, {"app/a.py": 2}) == [("app/a.py", 2, 3)]
    assert compare_counts({"app/new.py": 1}, {}) == [("app/new.py", 0, 1)]
    assert compare_counts({}, {"app/deleted.py": 4}) == []
    assert reject_baseline_increases({"app/a.py": 1}, {"app/a.py": 2}) == []
    assert reject_baseline_increases({"app/a.py": 3}, {"app/a.py": 2}) == [("app/a.py", 2, 3)]
    payload = {
        "schema_version": 1,
        "source_sha": "a" * 40,
        "mypy_version": "2.3.1",
        "files": {"app/a.py": 2, "app/b.py": 1},
    }
    assert validate_baseline(payload)[2] == {"app/a.py": 2, "app/b.py": 1}
    try:
        parse_error_counts("not-a-path error: malformed")
    except RatchetError:
        pass
    else:
        raise AssertionError("malformed error line must fail closed")
    print("typecheck ratchet self-test: PASS")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--base-sha", default=None)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    version = _mypy_version()
    current, raw_output = run_mypy()

    if not args.baseline.exists():
        source_sha = args.base_sha
        if not source_sha or not SHA_RE.fullmatch(source_sha):
            raise RatchetError("missing baseline requires --base-sha for an auditable bootstrap candidate")
        print(raw_output, end="")
        print("TYPECHECK_BASELINE_CANDIDATE_BEGIN")
        print(baseline_json(source_sha, version, current), end="")
        print("TYPECHECK_BASELINE_CANDIDATE_END")
        raise RatchetError("baseline missing; commit the exact-base candidate above")

    source_sha, baseline_version, allowed = load_baseline(args.baseline)
    if baseline_version != version:
        raise RatchetError(
            f"mypy version mismatch: baseline={baseline_version}, installed={version}"
        )

    if args.base_sha:
        parent = _git_show_baseline(args.base_sha)
        if parent is None:
            if source_sha != args.base_sha:
                raise RatchetError(
                    "bootstrap baseline source_sha must equal the exact PR base SHA"
                )
        else:
            _, parent_version, parent_counts = parent
            if parent_version != baseline_version:
                raise RatchetError(
                    "baseline tool version changed relative to PR base; re-derive explicitly"
                )
            increases = reject_baseline_increases(allowed, parent_counts)
            if increases:
                for path, previous, proposed in increases:
                    print(
                        f"baseline increase forbidden: {path}: base={previous} proposed={proposed}"
                    )
                raise RatchetError("baseline allowance may not increase")

    regressions = compare_counts(current, allowed)
    if regressions:
        for path, limit, count in regressions:
            print(f"type debt regression: {path}: baseline={limit} current={count}")
        raise RatchetError("per-file mypy debt increased")

    print(
        "typecheck ratchet: PASS "
        f"(current_errors={sum(current.values())}, baseline_errors={sum(allowed.values())})"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RatchetError as exc:
        print(f"typecheck ratchet: FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
