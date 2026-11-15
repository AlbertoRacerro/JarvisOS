"""Fixture: reduced copy of pre-guard release 5159b8e1 argument parser."""
from __future__ import annotations

import argparse
import os

OPERATIONS = (
    "bench.run",
    "git.clone",
    "git.diff",
    "git.fetch",
    "git.log",
    "git.patch",
    "git.push",
    "git.status",
    "release.install",
    "service.action",
    "service.status",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="host_ops.py", description="host ops")
    parser.add_argument("--home", default=os.environ.get("HOME", "/home/user"), help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("request", help="submit a typed host operation request")
    p.add_argument("op", choices=sorted(OPERATIONS))
    p.add_argument("--args", default="{}", help="JSON object of operation arguments")
    p.add_argument("--sha", help="full candidate commit the operation is evidence for")
    p.add_argument("--requester", default=os.environ.get("USER", "agent"))
    p.add_argument("--wait", type=float, default=0, help="seconds to wait for the verified receipt")
    p = sub.add_parser("cancel", help="cancel a request (before it starts, or a running bench.run)")
    p.add_argument("id")
    p = sub.add_parser("verify", help="verify a receipt's host signature and outcome")
    p.add_argument("id")
    p.add_argument("--op")
    p.add_argument("--sha")
    sub.add_parser("status", help="executor heartbeat and recent receipts")
    p = sub.add_parser("serve", help="run the host executor (systemd unit)")
    p.add_argument("--interval", type=float, default=2.0)
    p = sub.add_parser("install", help="install the executor from a GitHub master commit (host session)")
    p.add_argument("--sha", required=True)
    p.add_argument("--with-watch", action="store_true")
    sub.add_parser("health", help="host-side health check")
    parser.parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
