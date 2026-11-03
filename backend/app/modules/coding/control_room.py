"""Read-only maintainer control-room projection (spec 164).

Derived only from git at the local target ref and ``docs/specs/STATUS.md`` at
that same commit. Nothing here writes, fetches, or calls GitHub.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.modules.coding import runtime_truth as rt
from app.modules.coding.pipeline_state import STATUS_PATH

RECENT_WORK_LIMIT = 20
GIT_TIMEOUT_SECONDS = 4.0
STATUS_MAX_BYTES = 1_048_576
MAX_WARNINGS = 32
DESCRIPTION_MAX_CHARS = 280
MAX_RANGE_SPAN = 9

STATUSES = ("planned", "blocked", "ready", "in_progress", "in_review", "merged", "cancelled")
_AUTHORIZED_ORDER = ("in_review", "in_progress", "ready")
_SPEC_TOKEN = r"[0-9]{3}[a-z]?"
_SPEC_ID_RE = re.compile(rf"^{_SPEC_TOKEN}$", re.ASCII)
_DEP_RE = re.compile(rf"\b({_SPEC_TOKEN})\b", re.ASCII)
_PR_RE = re.compile(r"/pull/([1-9][0-9]*)")
_TARGET_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9._-]+)*$", re.ASCII)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.ASCII)
_MERGE_SUBJECT_RE = re.compile(
    r"^Merge pull request #([1-9][0-9]*) from ([A-Za-z0-9_.-]+)/(\S+)$",
    re.ASCII,
)
_ANY_PR_RE = re.compile(r"#([1-9][0-9]*)\b")
_BRANCH_SPECS_RE = re.compile(rf"^(?:spec-)?({_SPEC_TOKEN}(?:-{_SPEC_TOKEN})*)(?=-|$)", re.ASCII)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_CODE_NAME_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*:?$", re.ASCII)

KIND_LABELS = {
    "impl": "implementation",
    "plan": "plan",
    "reconcile": "reconcile",
    "docs": "docs",
    "repair": "repair",
    "fix": "repair",
}
_ACRONYMS = frozenset(
    {
        "3D", "3MF", "ADR", "AI", "API", "CAD", "CI", "DOF", "DWSIM", "FEM", "GLB",
        "IP", "LLM", "MCP", "OS", "PBR", "PR", "PTY", "RAG", "SQL", "STEP", "STL",
        "UI", "UX", "WSL",
    }
)
_PROPER_WORDS = {"BLUECAD": "BlueCAD", "DEVCTX": "devctx"}

GitRunner = Callable[[tuple[str, ...], int], str]


class GitProbeError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RegistryRow:
    spec_id: str
    status: str
    implementation_prs: tuple[int, ...]
    name: str
    depends_on: tuple[str, ...]
    description: str


def valid_target_ref(ref: str) -> bool:
    return (
        0 < len(ref) <= 100
        and _TARGET_REF_RE.fullmatch(ref) is not None
        and ".." not in ref
        and not ref.endswith((".lock", "/", "."))
    )


def short_title(name: str) -> str:
    """Turn registry code names such as ``CODING-CONTROL-ROOM-1`` into readable titles."""
    words: list[str] = []
    for token in name.split():
        if not _CODE_NAME_RE.fullmatch(token) or not any(ch.isalpha() for ch in token):
            words.append(token)
            continue
        colon = token.endswith(":")
        parts = token.rstrip(":").split("-")
        if len(parts) > 1 and parts[-1].isdigit():
            parts = parts[:-1]
        if len(parts) == 1 and (parts[0] in _ACRONYMS or len(parts[0]) <= 3) and not colon:
            words.append(token)
            continue
        rendered = [
            _PROPER_WORDS.get(part)
            or (part if part in _ACRONYMS or part.isdigit() else part.capitalize())
            for part in parts
        ]
        words.append(" ".join(rendered) + (":" if colon else ""))
    return " ".join(words) or name


def short_description(description: str) -> str:
    text = _MARKDOWN_LINK_RE.sub(r"\1", description).strip()
    if len(text) <= DESCRIPTION_MAX_CHARS:
        return text
    return text[: DESCRIPTION_MAX_CHARS - 1].rstrip() + "…"


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_registry(text: str) -> tuple[dict[str, RegistryRow], list[str]]:
    """Parse the full STATUS registry, dropping (and reporting) rows that are not exact."""
    rows: dict[str, RegistryRow] = {}
    duplicates: set[str] = set()
    warnings: list[str] = []
    active = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip() == "## Registry":
            active = True
            continue
        if active and line.startswith("## "):
            break
        if not active or not line.lstrip().startswith("|"):
            continue
        cells = _cells(line)
        if cells[0] in {"Spec", "---"}:
            continue
        if len(cells) != 6:
            warnings.append(f"status_row_malformed:line_{number}")
            continue
        spec_id, status, pr_cell, name, dep_cell, description = cells
        spec_id = spec_id.lower()
        if not _SPEC_ID_RE.fullmatch(spec_id):
            warnings.append(f"status_row_invalid_id:line_{number}")
            continue
        if status not in STATUSES:
            warnings.append(f"status_value_unknown:{spec_id}")
            continue
        if spec_id in rows or spec_id in duplicates:
            warnings.append(f"status_row_duplicate:{spec_id}")
            rows.pop(spec_id, None)
            duplicates.add(spec_id)
            continue
        deps = (
            ()
            if dep_cell in {"", "-", "—"}
            else tuple(dict.fromkeys(dep.lower() for dep in _DEP_RE.findall(dep_cell)))
        )
        rows[spec_id] = RegistryRow(
            spec_id=spec_id,
            status=status,
            implementation_prs=tuple(int(value) for value in _PR_RE.findall(pr_cell)),
            name=name,
            depends_on=deps,
            description=description,
        )
    if not rows:
        warnings.append("status_registry_missing")
    return rows, warnings


def branch_spec_ids(branch: str) -> list[str]:
    """Spec ids named by ``<kind>/<spec>[-<spec>...]-<slug>``; ``A-B`` with B>A is a range."""
    _, _, rest = branch.partition("/")
    match = _BRANCH_SPECS_RE.match(rest.lower()) if rest else None
    if match is None:
        return []
    tokens = match.group(1).split("-")
    if (
        len(tokens) == 2
        and tokens[0].isdigit()
        and tokens[1].isdigit()
        and 0 < int(tokens[1]) - int(tokens[0]) <= MAX_RANGE_SPAN
    ):
        start, end = int(tokens[0]), int(tokens[1])
        return [f"{value:03d}" for value in range(start, end + 1)]
    return list(dict.fromkeys(tokens))


def parse_merge_subject(subject: str) -> dict[str, object]:
    match = _MERGE_SUBJECT_RE.match(subject)
    if match is None:
        any_pr = _ANY_PR_RE.search(subject)
        return {
            "pr_number": int(any_pr.group(1)) if any_pr else None,
            "branch": None,
            "kind": "other",
            "spec_ids": [],
        }
    branch = match.group(3)
    head = branch.partition("/")[0].lower()
    return {
        "pr_number": int(match.group(1)),
        "branch": branch,
        "kind": KIND_LABELS.get(head, "other"),
        "spec_ids": branch_spec_ids(branch),
    }


def _registry_entry(spec_id: str, registry: dict[str, RegistryRow]) -> dict[str, object]:
    row = registry.get(spec_id)
    return {
        "spec_id": spec_id,
        "name": row.name if row else None,
        "title": short_title(row.name) if row else None,
        "status": row.status if row else None,
    }


def recent_work_items(log_text: str, registry: dict[str, RegistryRow]) -> tuple[list[dict[str, object]], list[str]]:
    items: list[dict[str, object]] = []
    warnings: list[str] = []
    for line in log_text.splitlines():
        if not line.strip():
            continue
        fields = line.split("\x00")
        if len(fields) != 3 or not _SHA_RE.fullmatch(fields[0]):
            warnings.append("recent_work_line_malformed")
            continue
        sha, merged_at, subject = fields
        parsed = parse_merge_subject(subject)
        spec_ids = parsed["spec_ids"]
        assert isinstance(spec_ids, list)
        items.append(
            {
                "sha": sha,
                "merged_at": merged_at,
                "subject": subject[:200],
                **parsed,
                "maintenance": not spec_ids,
                "specs": [_registry_entry(spec_id, registry) for spec_id in spec_ids],
            }
        )
    return items, warnings


def upcoming_queue(registry: dict[str, RegistryRow]) -> tuple[dict[str, list[dict[str, object]]], list[str]]:
    """Order open rows by authority, then dependency readiness; registry order inside a group."""
    warnings: list[str] = []
    groups: dict[str, list[dict[str, object]]] = {
        "authorized": [],
        "planned_ready": [],
        "planned_waiting": [],
        "blocked": [],
    }
    authorized: list[tuple[int, dict[str, object]]] = []
    for row in registry.values():
        if row.status in {"merged", "cancelled"}:
            continue
        dependencies = []
        for dep in row.depends_on:
            dep_row = registry.get(dep)
            if dep_row is None:
                warnings.append(f"dependency_absent:{row.spec_id}:{dep}")
            dependencies.append({"spec_id": dep, "status": dep_row.status if dep_row else None})
        deps_merged = all(dep["status"] == "merged" for dep in dependencies)
        entry = {
            "spec_id": row.spec_id,
            "status": row.status,
            "name": row.name,
            "title": short_title(row.name),
            "description": short_description(row.description),
            "dependencies": dependencies,
            "dependencies_merged": deps_merged,
            "implementation_prs": list(row.implementation_prs),
        }
        if row.status in _AUTHORIZED_ORDER:
            authorized.append((_AUTHORIZED_ORDER.index(row.status), entry))
        elif row.status == "planned":
            groups["planned_ready" if deps_merged else "planned_waiting"].append(entry)
        else:
            groups["blocked"].append(entry)
    groups["authorized"] = [entry for _, entry in sorted(authorized, key=lambda item: item[0])]
    return groups, warnings


def _default_git(root: Path) -> GitRunner:
    def run(args: tuple[str, ...], max_bytes: int) -> str:
        try:
            result = rt._run_git_probe(root, args, GIT_TIMEOUT_SECONDS, max_bytes)
        except FileNotFoundError as exc:
            raise GitProbeError("git_unavailable") from exc
        except TimeoutError as exc:
            raise GitProbeError("git_timeout") from exc
        except OverflowError as exc:
            raise GitProbeError("git_output_oversized") from exc
        except (OSError, RuntimeError) as exc:
            raise GitProbeError("git_probe_failed") from exc
        if result.returncode != 0:
            raise GitProbeError("git_command_failed")
        try:
            return result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitProbeError("git_output_malformed") from exc

    return run


class ControlRoomProjection:
    def __init__(self, root: Path | None = None, git: GitRunner | None = None) -> None:
        self._git = git or _default_git((root or rt.trusted_repository_root()).resolve())

    def project(self, target_ref: str) -> dict[str, object]:
        warnings: list[str] = []
        git_ref = f"refs/remotes/origin/{target_ref}"
        source: dict[str, object] = {"git_ref": git_ref, "sha": None, "status_path": STATUS_PATH}
        recent: dict[str, object] = {"status": "unavailable", "limit": RECENT_WORK_LIMIT, "items": []}
        upcoming: dict[str, object] = {
            "status": "unavailable",
            "authorized": [],
            "planned_ready": [],
            "planned_waiting": [],
            "blocked": [],
        }
        result: dict[str, object] = {
            "observed_at": datetime.now(UTC).isoformat(),
            "source": source,
            "recent_work": recent,
            "upcoming": upcoming,
            "warnings": warnings,
        }
        if not valid_target_ref(target_ref):
            warnings.append("target_ref_invalid")
            return result
        try:
            sha = self._git(("rev-parse", "--verify", "--quiet", f"{git_ref}^{{commit}}"), 4096).strip()
        except GitProbeError as exc:
            warnings.append(f"target_ref_unavailable:{exc.code}")
            return result
        if not _SHA_RE.fullmatch(sha):
            warnings.append("target_ref_unavailable:malformed_sha")
            return result
        source["sha"] = sha

        registry: dict[str, RegistryRow] = {}
        try:
            status_text = self._git(("show", f"{sha}:{STATUS_PATH}"), STATUS_MAX_BYTES)
        except GitProbeError as exc:
            warnings.append(f"status_unavailable:{exc.code}")
        else:
            registry, registry_warnings = parse_registry(status_text)
            warnings.extend(registry_warnings)
            if registry:
                groups, queue_warnings = upcoming_queue(registry)
                upcoming.update(groups)
                upcoming["status"] = "available"
                warnings.extend(queue_warnings)

        try:
            log_text = self._git(
                (
                    "log",
                    "--first-parent",
                    "--merges",
                    f"--max-count={RECENT_WORK_LIMIT}",
                    "--format=%H%x00%cI%x00%s",
                    sha,
                    "--",
                ),
                rt.MAX_PROBE_OUTPUT_BYTES,
            )
        except GitProbeError as exc:
            warnings.append(f"recent_work_unavailable:{exc.code}")
        else:
            items, item_warnings = recent_work_items(log_text, registry)
            recent["items"] = items
            recent["status"] = "available"
            warnings.extend(dict.fromkeys(item_warnings))
        del warnings[MAX_WARNINGS:]
        return result
