from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.coding import control_room as cr
from app.modules.coding import runtime_routes
from app.modules.coding import runtime_truth as rt

REPOSITORY = "AlbertoRacerro/JarvisOS"

HEADER = (
    "# Spec status\n\nPreamble | not a row |\n\n## Registry\n\n"
    "| Spec | Status | Implementation PR | Name | Depends on | Description |\n"
    "| --- | --- | --- | --- | --- | --- |\n"
)


def registry_text(*rows: str, trailer: str = "") -> str:
    return HEADER + "\n".join(rows) + "\n" + trailer


def row(spec: str, status: str, name: str = "NAME-1", deps: str = "—", pr: str = "—", desc: str = "d") -> str:
    return f"| {spec} | {status} | {pr} | {name} | {deps} | {desc} |"


# --- registry parsing ---------------------------------------------------------


def test_registry_parses_all_rows_with_prs_dependencies_and_section_bound() -> None:
    text = registry_text(
        row("001", "merged", "Schema freeze", pr="[#4](https://github.com/o/r/pull/4)"),
        row("059b", "merged", "IP-EGRESS-1"),
        row("002", "planned", "SECOND-1", deps="001, 059b"),
        trailer="\n## Later section\n\n" + row("999", "ready"),
    )
    rows, warnings = cr.parse_registry(text)
    assert warnings == []
    assert list(rows) == ["001", "059b", "002"]
    assert rows["001"].implementation_prs == (4,)
    assert rows["002"].depends_on == ("001", "059b")
    assert rows["059b"].depends_on == ()


def test_registry_drops_and_reports_inexact_rows() -> None:
    text = registry_text(
        row("001", "merged"),
        "| 002 | planned | — | Too few columns |",
        row("12", "planned"),
        row("003", "shipping"),
        row("004", "planned"),
        row("004", "ready"),
        row("004", "merged"),
    )
    rows, warnings = cr.parse_registry(text)
    assert list(rows) == ["001"]
    assert any(w.startswith("status_row_malformed:line_") for w in warnings)
    assert any(w.startswith("status_row_invalid_id:line_") for w in warnings)
    assert "status_value_unknown:003" in warnings
    assert warnings.count("status_row_duplicate:004") == 2


def test_registry_missing_section_is_reported() -> None:
    rows, warnings = cr.parse_registry("# no registry here\n" + row("001", "merged"))
    assert rows == {}
    assert warnings == ["status_registry_missing"]


# --- merge subject parsing ----------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "pr", "kind", "spec_ids"),
    [
        ("Merge pull request #747 from AlbertoRacerro/impl/023-adversarial-proposal-corpus", 747, "implementation", ["023"]),
        ("Merge pull request #748 from AlbertoRacerro/reconcile/023-merged", 748, "reconcile", ["023"]),
        ("Merge pull request #739 from AlbertoRacerro/plan/158-159-contracts", 739, "plan", ["158", "159"]),
        ("Merge pull request #749 from AlbertoRacerro/plan/161-164-human-acceptance", 749, "plan", ["161", "162", "163", "164"]),
        ("Merge pull request #723 from AlbertoRacerro/docs/154a-status-reconcile", 723, "docs", ["154a"]),
        ("Merge pull request #726 from AlbertoRacerro/repair/154b-status-recovery", 726, "repair", ["154b"]),
        ("Merge pull request #719 from AlbertoRacerro/reconcile/spec-154-pr-718-ad58f407", 719, "reconcile", ["154"]),
        ("Merge pull request #729 from AlbertoRacerro/reconcile/pre155-closure", 729, "reconcile", []),
        ("Merge pull request #600 from AlbertoRacerro/infra/cloud-native-delivery-bridge", 600, "other", []),
        ("Merge pull request #1 from AlbertoRacerro/plan/155-2026-direction", 1, "plan", ["155"]),
        ("Merge pull request #2 from AlbertoRacerro/plan/100a-100c-split", 2, "plan", ["100a", "100c"]),
        ("Merge pull request #3 from AlbertoRacerro/plan/100-200-wide", 3, "plan", ["100", "200"]),
        ("Merge pull request #4 from AlbertoRacerro/impl/123abc-slug", 4, "implementation", []),
        ("Merge PR #595: generic builder toolbox priority", 595, "other", []),
        ("Add cross-chat idea intake register", None, "other", []),
    ],
)
def test_merge_subject_parsing(subject: str, pr: int | None, kind: str, spec_ids: list[str]) -> None:
    parsed = cr.parse_merge_subject(subject)
    assert parsed["pr_number"] == pr
    assert parsed["kind"] == kind
    assert parsed["spec_ids"] == spec_ids


@pytest.mark.parametrize(
    ("name", "title"),
    [
        ("CODING-CONTROL-ROOM-1", "Coding Control Room"),
        ("BLUECAD-3D-INSPECTION-RECOVERY-1", "BlueCAD 3D Inspection Recovery"),
        ("CODING-REPOSITORY-BROWSER-UX-1", "Coding Repository Browser UX"),
        ("GRADE-0", "Grade"),
        ("AGENT-ORCH: Hermes integration umbrella", "Agent Orch: Hermes integration umbrella"),
        ("IP-EGRESS-1 umbrella definition", "IP Egress umbrella definition"),
        ("Tier 2 domain-validator plugin interface", "Tier 2 domain-validator plugin interface"),
        ("L2 ephemeral free-script proposals", "L2 ephemeral free-script proposals"),
    ],
)
def test_short_title(name: str, title: str) -> None:
    assert cr.short_title(name) == title


def test_short_description_strips_links_and_truncates() -> None:
    assert cr.short_description("[Accepted contract](x.md): body") == "Accepted contract: body"
    long = cr.short_description("x" * 1000)
    assert len(long) == cr.DESCRIPTION_MAX_CHARS and long.endswith("…")


# --- upcoming ordering --------------------------------------------------------


def test_upcoming_orders_by_authority_then_dependency_readiness_not_numeric_id() -> None:
    rows, _ = cr.parse_registry(
        registry_text(
            row("001", "merged"),
            row("010", "ready"),
            row("090", "planned", deps="001"),
            row("020", "planned"),
            row("300", "blocked"),
            row("200", "in_review", pr="[#9](https://github.com/o/r/pull/9)"),
            row("050", "planned", deps="010"),
            row("150", "in_progress"),
            row("030", "cancelled"),
        )
    )
    groups, warnings = cr.upcoming_queue(rows)
    assert warnings == []
    assert [e["spec_id"] for e in groups["authorized"]] == ["200", "150", "010"]
    assert [e["spec_id"] for e in groups["planned_ready"]] == ["090", "020"]
    assert [e["spec_id"] for e in groups["planned_waiting"]] == ["050"]
    assert [e["spec_id"] for e in groups["blocked"]] == ["300"]
    waiting = groups["planned_waiting"][0]
    assert waiting["dependencies"] == [{"spec_id": "010", "status": "ready"}]
    assert waiting["dependencies_merged"] is False


def test_upcoming_empty_authorized_queue_and_absent_dependency_fail_closed() -> None:
    rows, _ = cr.parse_registry(registry_text(row("001", "merged"), row("002", "planned", deps="001, 777")))
    groups, warnings = cr.upcoming_queue(rows)
    assert groups["authorized"] == []
    assert groups["planned_ready"] == []
    assert [e["spec_id"] for e in groups["planned_waiting"]] == ["002"]
    assert warnings == ["dependency_absent:002:777"]


# --- projection against a real git repository --------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _merge(root: Path, branch: str, message: str, file: str) -> None:
    _git(root, "checkout", "-q", "-b", branch, "main")
    (root / file).write_text(branch)
    _git(root, "add", file)
    _git(root, "commit", "-q", "-m", f"work on {branch}")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "-q", "--no-ff", branch, "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "docs" / "specs").mkdir(parents=True)
    (root / "docs" / "specs" / "STATUS.md").write_text(
        registry_text(
            row("001", "merged", "FIRST-THING-1"),
            row("002", "ready", "SECOND-THING-1", deps="001"),
            row("003", "planned", "Third thing", deps="002"),
        )
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    _merge(root, "impl/001-first", "Merge pull request #10 from o/impl/001-first", "a.txt")
    # A merge inside a side branch must not appear: only first-parent merges count.
    _git(root, "checkout", "-q", "-b", "side", "main")
    _git(root, "checkout", "-q", "-b", "inner", "side")
    (root / "inner.txt").write_text("inner")
    _git(root, "add", "inner.txt")
    _git(root, "commit", "-q", "-m", "inner work")
    _git(root, "checkout", "-q", "side")
    _git(root, "merge", "-q", "--no-ff", "inner", "-m", "Merge pull request #11 from o/impl/003-hidden")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "-q", "--no-ff", "side", "-m", "Merge pull request #12 from o/reconcile/pre002-closure")
    _merge(root, "plan/002-003-pair", "Merge pull request #13 from o/plan/002-003-pair", "b.txt")
    _git(root, "update-ref", "refs/remotes/origin/master", "main")
    return root


def test_projection_reads_first_parent_merges_and_joins_registry_at_ref(repo: Path) -> None:
    result = cr.ControlRoomProjection(root=repo).project("master")
    assert result["warnings"] == []
    source = result["source"]
    assert source["sha"] == _git(repo, "rev-parse", "main")
    items = result["recent_work"]["items"]
    assert [item["pr_number"] for item in items] == [13, 12, 10]
    newest, maintenance, oldest = items
    assert newest["kind"] == "plan"
    assert [(s["spec_id"], s["title"], s["status"]) for s in newest["specs"]] == [
        ("002", "Second Thing", "ready"),
        ("003", "Third thing", "planned"),
    ]
    assert maintenance["maintenance"] is True and maintenance["specs"] == []
    assert oldest["specs"][0]["status"] == "merged"
    assert newest["sha"] == source["sha"]
    upcoming = result["upcoming"]
    assert upcoming["status"] == "available"
    assert [e["spec_id"] for e in upcoming["authorized"]] == ["002"]
    assert [e["spec_id"] for e in upcoming["planned_waiting"]] == ["003"]


def test_projection_reads_status_at_ref_not_worktree(repo: Path) -> None:
    (repo / "docs" / "specs" / "STATUS.md").write_text(registry_text(row("001", "cancelled")))
    result = cr.ControlRoomProjection(root=repo).project("master")
    assert [e["spec_id"] for e in result["upcoming"]["authorized"]] == ["002"]


def test_projection_missing_target_ref_fails_closed(repo: Path) -> None:
    result = cr.ControlRoomProjection(root=repo).project("does-not-exist")
    assert result["source"]["sha"] is None
    assert result["recent_work"] == {"status": "unavailable", "limit": cr.RECENT_WORK_LIMIT, "items": []}
    assert result["upcoming"]["status"] == "unavailable"
    assert result["warnings"] == ["target_ref_unavailable:git_command_failed"]


def test_projection_missing_status_keeps_recent_work_without_inventing_state(repo: Path) -> None:
    _git(repo, "rm", "-q", "docs/specs/STATUS.md")
    _git(repo, "commit", "-q", "-m", "drop status")
    _git(repo, "update-ref", "refs/remotes/origin/master", "main")
    result = cr.ControlRoomProjection(root=repo).project("master")
    assert result["warnings"] == ["status_unavailable:git_command_failed"]
    assert result["upcoming"]["status"] == "unavailable"
    assert result["recent_work"]["status"] == "available"
    assert result["recent_work"]["items"][0]["specs"][0]["status"] is None


def test_projection_git_unavailable_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def missing(*args: object, **kwargs: object):
        raise FileNotFoundError("git")

    monkeypatch.setattr(rt, "_run_git_probe", missing)
    result = cr.ControlRoomProjection(root=tmp_path).project("master")
    assert result["warnings"] == ["target_ref_unavailable:git_unavailable"]
    assert result["recent_work"]["status"] == "unavailable"


def test_projection_oversized_or_failing_log_is_reported() -> None:
    sha = "a" * 40

    def git(args: tuple[str, ...], max_bytes: int) -> str:
        if args[0] == "rev-parse":
            return sha + "\n"
        if args[0] == "show":
            return registry_text(row("001", "ready"))
        raise cr.GitProbeError("git_output_oversized")

    result = cr.ControlRoomProjection(git=git).project("master")
    assert result["warnings"] == ["recent_work_unavailable:git_output_oversized"]
    assert result["upcoming"]["status"] == "available"


def test_projection_rejects_unsafe_target_ref_without_running_git() -> None:
    def git(args: tuple[str, ...], max_bytes: int) -> str:
        raise AssertionError("git must not run")

    for ref in ("../etc", "-x", "a..b", "master.lock", ""):
        assert cr.ControlRoomProjection(git=git).project(ref)["warnings"] == ["target_ref_invalid"]


# --- route --------------------------------------------------------------------


class _FakeProjection:
    def __init__(self, sha: str | None) -> None:
        self.sha = sha

    def project(self, target_ref: str) -> dict[str, object]:
        return {
            "observed_at": "2026-10-01T00:00:00+00:00",
            "source": {"git_ref": f"refs/remotes/origin/{target_ref}", "sha": self.sha, "status_path": "x"},
            "recent_work": {"status": "available", "limit": 20, "items": []},
            "upcoming": {"status": "available", "authorized": [], "planned_ready": [], "planned_waiting": [], "blocked": []},
            "warnings": [],
        }


def _client(monkeypatch: pytest.MonkeyPatch, *, sha: str | None, startup: object | None) -> TestClient:
    monkeypatch.setattr(runtime_routes, "_control_room_projection", lambda: _FakeProjection(sha))
    monkeypatch.setattr(runtime_routes, "_repository_service", lambda: object())

    class FakeRuntime:
        def __init__(self, _service: object) -> None:
            pass

        def inspect(self, **_: object) -> dict[str, object]:
            return {"alignment": "aligned", "remote": {"resolved_sha": "b" * 40}}

    monkeypatch.setattr(runtime_routes, "RuntimeTruthService", FakeRuntime)
    app = FastAPI()
    app.include_router(runtime_routes.router)
    if startup is not None:
        app.state.runtime_startup_snapshot = startup
    return TestClient(app)


def _startup() -> rt.RuntimeSnapshot:
    return rt.RuntimeSnapshot(
        root_identity="a" * 64,
        observed_at="2026-10-01T00:00:00+00:00",
        git_available=True,
        git_sha="b" * 40,
        head_state="branch",
        branch="master",
        dirty_state="clean",
        provenance="process_start_observation",
        failure_code=None,
    )


def test_route_reports_local_ref_lagging_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, sha="a" * 40, startup=_startup())
    body = client.get("/api/coding/control-room", params={"repository": REPOSITORY, "target_ref": "master"}).json()
    assert body["runtime"]["alignment"] == "aligned"
    assert body["warnings"] == ["local_target_ref_differs_from_remote"]
    assert body["repository"] == REPOSITORY and body["target_ref"] == "master"


def test_route_without_startup_snapshot_keeps_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, sha="b" * 40, startup=None)
    body = client.get("/api/coding/control-room", params={"repository": REPOSITORY, "target_ref": "master"}).json()
    assert body["runtime"] is None
    assert body["warnings"] == ["startup_snapshot_unavailable"]
    assert body["recent_work"]["status"] == "available"


def test_route_refuses_other_repository_and_bad_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, sha=None, startup=_startup())
    bad_repo = client.get("/api/coding/control-room", params={"repository": "o/other", "target_ref": "master"})
    assert bad_repo.status_code == 400
    bad_ref = client.get("/api/coding/control-room", params={"repository": REPOSITORY, "target_ref": "../x"})
    assert bad_ref.status_code == 400


def test_control_room_route_is_get_only() -> None:
    methods = {route.path: route.methods for route in runtime_routes.router.routes if hasattr(route, "methods")}
    assert methods["/api/coding/control-room"] == {"GET"}
