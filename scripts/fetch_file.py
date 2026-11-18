#!/usr/bin/env python3
"""Spec 310: fetch a file over HTTPS (or loopback HTTP) and record progress.

Usage:
  python3 scripts/fetch_file.py URL DEST [--sha256 HEX] [--label TEXT]

Records download state under <control_root>/downloads/<id>.json with path-free state.
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import secrets
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_CONTROL = Path.home() / "jarvis-control" / "work" / "continuity"
CHUNK_SIZE = 65_536


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def control_root() -> Path:
    return Path(os.environ.get("RELAY_CONTINUITY_DIR", DEFAULT_CONTROL)).expanduser()


def downloads_dir(root: Path | None = None) -> Path:
    return (root or control_root()) / "downloads"


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    with open(tmp, "x", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_url(url: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"unsupported scheme: {parsed.scheme!r}")
    if parsed.scheme == "http" and not is_loopback(parsed.hostname):
        raise ValueError(f"http scheme only permitted for loopback: {parsed.hostname!r}")
    return parsed


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        try:
            validate_url(newurl)
        except ValueError as exc:
            raise urllib.error.HTTPError(
                req.full_url, code, f"redirect refused: {exc}", headers, fp
            ) from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _short_reason(exc: BaseException) -> str:
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted"
    msg = str(exc).strip()
    return msg[:80] if msg else exc.__class__.__name__


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch a file over HTTPS (or loopback HTTP) and record progress."
    )
    parser.add_argument("url", metavar="URL", help="Source URL (HTTPS or loopback HTTP)")
    parser.add_argument("dest", metavar="DEST", help="Destination file path")
    parser.add_argument("--sha256", dest="sha256", default=None, help="Expected SHA-256 hex digest")
    parser.add_argument(
        "--label", dest="label", default=None, help="Readable label (defaults to destination file name)"
    )

    args = parser.parse_args()

    dest_path = Path(args.dest)
    if dest_path.exists():
        sys.stderr.write(f"destination already exists: {dest_path}\n")
        return 1

    try:
        parsed = validate_url(args.url)
    except ValueError as exc:
        sys.stderr.write(f"refused URL: {exc}\n")
        return 1

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest_path.with_name(f"{dest_path.name}.part")

    download_id = f"d-{now_utc().strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    rec_path = downloads_dir() / f"{download_id}.json"

    label = args.label or dest_path.name
    host = parsed.hostname or ""
    file_name = dest_path.name
    pid = os.getpid()
    started_at = iso(now_utc())

    record: dict[str, Any] = {
        "id": download_id,
        "label": label,
        "host": host,
        "file_name": file_name,
        "bytes_done": 0,
        "bytes_total": None,
        "state": "running",
        "reason": None,
        "started_at": started_at,
        "ended_at": None,
        "pid": pid,
        "sha256": None,
    }

    def _cleanup_part() -> None:
        if part_path.exists():
            try:
                part_path.unlink()
            except OSError:
                pass

    def _record_failed(reason: str) -> None:
        record["state"] = "failed"
        record["reason"] = reason
        record["ended_at"] = iso(now_utc())
        try:
            write_json_atomic(rec_path, record)
        except OSError:
            pass

    def sigterm_handler(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt("SIGTERM")

    signal.signal(signal.SIGTERM, sigterm_handler)

    try:
        handlers: list[urllib.request.BaseHandler] = [SafeRedirectHandler()]
        if is_loopback(parsed.hostname):
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(args.url, headers={"User-Agent": "JarvisOS-Fetch/1.0"})

        with opener.open(req) as response:
            cl = response.headers.get("Content-Length")
            bytes_total: int | None = None
            if cl is not None and cl.isdigit():
                bytes_total = int(cl)
            record["bytes_total"] = bytes_total
            write_json_atomic(rec_path, record)

            bytes_done = 0
            hasher = hashlib.sha256()
            last_record_write = time.monotonic()

            with open(part_path, "wb") as handle:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    bytes_done += len(chunk)
                    hasher.update(chunk)

                    now_m = time.monotonic()
                    if now_m - last_record_write >= 1.0:
                        record["bytes_done"] = bytes_done
                        write_json_atomic(rec_path, record)
                        last_record_write = now_m

            if bytes_total is not None and bytes_done != bytes_total:
                raise OSError(f"server closed early: got {bytes_done} of {bytes_total} bytes")

            digest = hasher.hexdigest().lower()
            if args.sha256 and digest != args.sha256.lower().strip():
                raise ValueError("checksum mismatch")

            os.replace(part_path, dest_path)

            record["bytes_done"] = bytes_done
            record["state"] = "succeeded"
            record["ended_at"] = iso(now_utc())
            record["sha256"] = digest
            write_json_atomic(rec_path, record)
            return 0

    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
        _cleanup_part()
        reason = _short_reason(exc)
        _record_failed(reason)
        sys.stderr.write(f"download failed: {reason}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
