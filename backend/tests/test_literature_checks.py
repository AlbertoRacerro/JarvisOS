from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.paths import build_paths
from app.modules.memory import literature_service
from app.modules.memory.literature_checks import check_datum, number_tokens
from tests.test_literature_knowledge import _artifact, _source_url, _workspace_ids, client  # noqa: F401

LINES = ["intro", "rate", "k = 1.2 × 10⁻³ s⁻¹ at 300 K"]


def _datum(lines=LINES, **overrides):
    args = {
        "value_number": 0.0012,
        "value_text": None,
        "unit": "1/s",
        "locator_kind": "line",
        "locator_start": 3,
        "locator_end": 3,
        "lines": lines,
    }
    args.update(overrides)
    checks, verdict = check_datum(**args)
    return {item.id: item.status for item in checks}, verdict


def test_value_at_locator_passes_and_wrong_value_is_not_usable() -> None:
    statuses, verdict = _datum()
    assert statuses == {"LIT_LOCATOR_PRESENT": "pass", "LIT_VALUE_AT_LOCATOR": "pass", "LIT_UNIT_KNOWN": "pass"}
    assert verdict == "usable"
    statuses, verdict = _datum(value_number=0.0021)
    assert statuses["LIT_VALUE_AT_LOCATOR"] == "fail"
    assert verdict == "not_usable"


def test_missing_locator_and_unsupported_backing_are_unchecked() -> None:
    statuses, verdict = _datum(locator_kind=None, locator_start=None, locator_end=None, lines=None)
    assert statuses["LIT_LOCATOR_PRESENT"] == "not_evaluated"
    assert verdict == "unchecked"
    statuses, verdict = _datum(lines=None, unavailable="The backing is not a text or markdown file.")
    assert statuses["LIT_LOCATOR_PRESENT"] == "pass"
    assert statuses["LIT_VALUE_AT_LOCATOR"] == "not_evaluated"
    assert verdict == "unchecked"
    _, verdict = _datum(locator_kind="page", lines=None)
    assert verdict == "unchecked"


def test_unknown_unit_warns_and_missing_unit_on_number_warns() -> None:
    statuses, verdict = _datum(unit="notaunit")
    assert statuses["LIT_UNIT_KNOWN"] == "warn"
    assert verdict == "usable_with_warnings"
    statuses, _ = _datum(unit=None)
    assert statuses["LIT_UNIT_KNOWN"] == "warn"
    statuses, verdict = _datum(
        value_number=None, value_text="at  300 K", unit=None, locator_start=3
    )
    assert statuses["LIT_UNIT_KNOWN"] == "not_evaluated"
    assert statuses["LIT_VALUE_AT_LOCATOR"] == "pass"
    assert verdict == "usable"


def test_changed_backing_range_fails() -> None:
    statuses, verdict = _datum(locator_start=9, locator_end=9)
    assert statuses["LIT_VALUE_AT_LOCATOR"] == "fail"
    assert verdict == "not_usable"


@pytest.mark.parametrize(
    ("text", "wanted"),
    [
        ("x = 0,5 m", 0.5),
        ("x = 5e-1 m", 0.5),
        ("x = −0.5 m", -0.5),
        ("1.2 × 10⁻³", 0.0012),
        ("1.2 x 10^-3", 0.0012),
        ("1.2·10^3", 1200.0),
        ("1.2 × 10-3", 0.0012),
        ("1.2 × 10−3", 0.0012),
        ("2 × 10²", 200.0),
        ("range 2-3", 3.0),
        ("0 K", 0.0),
    ],
)
def test_number_tokens_match(text: str, wanted: float) -> None:
    assert any(abs(token - wanted) <= 1e-12 + 1e-9 * abs(wanted) for token in number_tokens(text))


@pytest.mark.parametrize(("text", "unwanted"), [("15", 1.5), ("11.5", 1.5), ("-0.5", 0.5), ("0.5", -0.5)])
def test_number_tokens_do_not_over_match(text: str, unwanted: float) -> None:
    assert unwanted not in number_tokens(text)


def test_claim_entries_have_no_checks(client: TestClient) -> None:  # noqa: F811
    workspace_id, _ = _workspace_ids(client)
    artifact = _artifact(
        workspace_id, build_paths().artifacts_dir / "literature" / "c.md", mime_type="text/markdown", payload=b"a\n"
    )
    source = client.post(
        _source_url(workspace_id), json={"title": "C", "source_kind": "report", "artifact_id": artifact}
    ).json()
    entry = client.post(
        f"{_source_url(workspace_id)}/{source['id']}/entries", json={"entry_kind": "claim", "statement": "s"}
    ).json()
    assert entry["checks"] == []
    assert entry["verdict"] is None


def test_route_returns_checks_and_reads_backing_once(client: TestClient, monkeypatch) -> None:  # noqa: F811
    workspace_id, _ = _workspace_ids(client)
    artifact = _artifact(
        workspace_id,
        build_paths().artifacts_dir / "literature" / "k.md",
        mime_type="text/markdown",
        payload="intro\nrate\nk = 1.2 × 10⁻³ s⁻¹\n".encode(),
    )
    source = client.post(
        _source_url(workspace_id), json={"title": "K", "source_kind": "report", "artifact_id": artifact}
    ).json()
    base = f"{_source_url(workspace_id)}/{source['id']}/entries"
    good = {"entry_kind": "datum", "value_number": 0.0012, "unit": "1/s", "locator_kind": "line", "locator_start": 3}
    assert client.post(base, json=good).status_code == 201
    assert client.post(base, json={**good, "value_number": 0.0021}).status_code == 201
    assert client.post(base, json={"entry_kind": "datum", "value_number": 7.0, "unit": "1/s"}).status_code == 201

    reads = []
    original = literature_service._read_backing_lines
    monkeypatch.setattr(literature_service, "_read_backing_lines", lambda path: reads.append(path) or original(path))
    entries = client.get(f"{_source_url(workspace_id)}/{source['id']}").json()["entries"]
    verdicts = sorted(item["verdict"] for item in entries)
    assert verdicts == ["not_usable", "unchecked", "usable"]
    assert len(reads) == 1
