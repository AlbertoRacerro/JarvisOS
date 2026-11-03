#!/usr/bin/env python3
"""Fail CI when the canonical spec registry and a PR disagree."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REGISTRY = Path("docs/specs/STATUS.md")
STATUSES = {
    "planned",
    "blocked",
    "ready",
    "in_progress",
    "in_review",
    "merged",
    "cancelled",
}
NO_PR = {"planned", "blocked", "ready", "in_progress"}
DEFINITION = {"planned", "blocked", "ready"}
SPEC = r"\d{3}[a-z]?"
SPEC_RE = re.compile(rf"^{SPEC}$", re.I)
PR_RE = re.compile(r"/pull/(\d+)")
DEP_RE = re.compile(rf"\b({SPEC})\b", re.I)
DECL_RE = re.compile(
    rf"^\s*\*\*Spec gate:\*\*\s*"
    rf"(?:(implementation|definition)\s+({SPEC})|(N/A))\s*$",
    re.I | re.M,
)
TITLE_RE = re.compile(rf"implement(?:ation of)?\s+spec\s+({SPEC})\b", re.I)


class GateError(ValueError):
    pass


def pr_numbers(cell: str) -> set[int]:
    return {int(value) for value in PR_RE.findall(cell)}


def dependencies(cell: str) -> list[str]:
    if cell.strip() in {"", "-", "—"}:
        return []
    return list(dict.fromkeys(value.lower() for value in DEP_RE.findall(cell)))


def parse_registry(text: str) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    active = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip() == "## Registry":
            active = True
            continue
        if active and line.startswith("## "):
            break
        if not active or not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] in {"Spec", "---"}:
            continue
        if len(cells) != 6:
            raise GateError(f"STATUS.md:{number}: expected 6 columns")
        spec_id, status, pr_cell, name, dep_cell, description = cells
        spec_id = spec_id.lower()
        if not SPEC_RE.fullmatch(spec_id):
            raise GateError(f"STATUS.md:{number}: invalid spec id {spec_id!r}")
        if spec_id in rows:
            raise GateError(f"STATUS.md:{number}: duplicate spec {spec_id}")
        if status not in STATUSES:
            raise GateError(f"spec {spec_id}: invalid status {status!r}")
        prs = pr_numbers(pr_cell)
        if status in NO_PR and prs:
            raise GateError(
                f"spec {spec_id}: {status} cannot carry an implementation PR"
            )
        if status == "in_review" and not prs:
            raise GateError(f"spec {spec_id}: in_review requires a PR")
        rows[spec_id] = {
            "status": status,
            "prs": prs,
            "deps": dependencies(dep_cell),
            "name": name,
            "description": description,
        }
    if not rows:
        raise GateError("STATUS.md registry is missing or empty")
    for spec_id, row in rows.items():
        for dep in row["deps"]:
            if dep == spec_id:
                raise GateError(f"spec {spec_id}: self-dependency")
            if dep not in rows:
                raise GateError(f"spec {spec_id}: dependency {dep} is absent")
    return rows


def declaration(body: str) -> tuple[str, str | None] | None:
    matches = list(DECL_RE.finditer(body))
    if len(matches) > 1:
        raise GateError("PR body has multiple **Spec gate:** declarations")
    if not matches:
        return None
    match = matches[0]
    if match.group(3):
        return "n/a", None
    return match.group(1).lower(), match.group(2).lower()


def implementation(rows, pr_number: int, spec_id: str) -> str:
    if spec_id not in rows:
        raise GateError(f"implementation spec {spec_id} is absent from STATUS.md")
    row = rows[spec_id]
    if row["status"] != "in_review":
        raise GateError(
            f"spec {spec_id}: expected in_review, found {row['status']}"
        )
    if pr_number not in row["prs"]:
        raise GateError(f"spec {spec_id}: missing current PR #{pr_number}")
    unmet = [
        f"{dep}={rows[dep]['status']}"
        for dep in row["deps"]
        if rows[dep]["status"] != "merged"
    ]
    if unmet:
        raise GateError(f"spec {spec_id}: unmerged dependencies: {', '.join(unmet)}")
    return f"spec-status: PR #{pr_number} -> implementation {spec_id}: OK"


def definition(rows, pr_number: int, spec_id: str) -> str:
    if spec_id not in rows:
        raise GateError(f"definition spec {spec_id} is absent from STATUS.md")
    row = rows[spec_id]
    if row["status"] not in DEFINITION:
        raise GateError(f"spec {spec_id}: definition requires planned/blocked/ready")
    if pr_number in row["prs"]:
        raise GateError(f"spec {spec_id}: definition PR cannot occupy PR column")
    return f"spec-status: PR #{pr_number} -> definition {spec_id}: OK"


def check_event(rows, event: dict) -> str:
    pr = event.get("pull_request")
    if not isinstance(pr, dict):
        return "spec-status: registry OK"
    pr_number = event.get("number")
    if not isinstance(pr_number, int):
        raise GateError("pull_request event has no integer PR number")
    linked = [spec_id for spec_id, row in rows.items() if pr_number in row["prs"]]
    if len(linked) > 1:
        raise GateError(f"PR #{pr_number} is linked to multiple specs: {linked}")

    declared = declaration(str(pr.get("body") or ""))
    if declared:
        mode, spec_id = declared
        if mode == "n/a":
            if linked:
                raise GateError(f"PR #{pr_number}: N/A conflicts with spec {linked[0]}")
            return f"spec-status: PR #{pr_number} -> N/A: OK"
        if mode == "definition":
            return definition(rows, pr_number, spec_id)
        return implementation(rows, pr_number, spec_id)

    if linked:
        return implementation(rows, pr_number, linked[0])
    title = TITLE_RE.search(str(pr.get("title") or ""))
    if title:
        return implementation(rows, pr_number, title.group(1).lower())
    return f"spec-status: PR #{pr_number}: compatibility mode, registry OK"


def run(registry: Path, event: Path | None) -> str:
    rows = parse_registry(registry.read_text(encoding="utf-8"))
    if event is None:
        return f"spec-status: registry OK ({len(rows)} rows)"
    return check_event(rows, json.loads(event.read_text(encoding="utf-8")))


def sample(*rows: str) -> str:
    return "\n".join(
        [
            "## Registry",
            "| Spec | Status | Implementation PR | Name | Depends on | Description |",
            "| --- | --- | --- | --- | --- | --- |",
            *rows,
        ]
    )


def expect_error(fn, text: str) -> None:
    try:
        fn()
    except GateError as exc:
        assert text in str(exc), exc
    else:
        raise AssertionError(f"expected GateError containing {text!r}")


def self_test() -> None:
    rows = parse_registry(
        sample(
            "| 001 | merged | [#10](https://x/pull/10) | Base | — | done |",
            "| 002 | in_review | [#99](https://x/pull/99) | Work | 001 | active |",
            "| 005b | ready | — | Definition | 001 | later |",
        )
    )

    def event(body, number=99, title=""):
        return {
            "number": number,
            "pull_request": {"body": body, "title": title},
        }

    assert check_event(rows, event("**Spec gate:** implementation 002")).endswith("OK")
    assert check_event(rows, event("**Spec gate:** definition 005b", 77)).endswith("OK")
    assert check_event(rows, event("**Spec gate:** N/A", 77)).endswith("OK")
    expect_error(
        lambda: check_event(rows, event("**Spec gate:** implementation 002", 98)),
        "missing current PR",
    )
    blocked = parse_registry(
        sample(
            "| 001 | in_review | [#10](https://x/pull/10) | Base | — | active |",
            "| 002 | in_review | [#99](https://x/pull/99) | Work | 001 | active |",
        )
    )
    expect_error(
        lambda: check_event(blocked, event("**Spec gate:** implementation 002")),
        "001=in_review",
    )
    print("spec-status: self-test OK")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    parser.add_argument("--event", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            self_test()
        else:
            print(run(args.registry, args.event))
        return 0
    except (GateError, OSError, json.JSONDecodeError, AssertionError) as exc:
        print(f"spec-status: FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
