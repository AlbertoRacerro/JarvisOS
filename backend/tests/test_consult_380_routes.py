"""Spec 380 part a & b routes: read-only status, the pending packet and maintainer actions."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import packs
from app.core.database import initialize_database, open_sqlite_connection
from app.core.vault import VAULT_PATH_ENV
from app.main import create_app
from app.modules.consult import routes
from app.modules.consult.service import ConsultRequest, request_consult
from tests.test_consult_380_flow import BRIEF, DIGEST, FakeTransport


@pytest.fixture()
def client(isolated_data_root: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "brief.md").write_text(BRIEF, encoding="utf-8")
    (pack / "jarvis_pack.toml").write_text(
        'id = "orchard"\n[consult]\nbrief = "brief.md"\nnucleus_heading = "Nucleus"\n', encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "jarvis_vault.toml").write_text("", encoding="utf-8")
    (vault / "consult.toml").write_text(
        f'consult_mode = "ask"\nbrief_sha256 = "{DIGEST}"\n[[trusted_accounts]]\nid = "sub"\nagent = "claude"\n'
        'model = "fake-consultant"\ntraining_off_verified_on = 2026-11-25\n', encoding="utf-8")
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(pack))
    monkeypatch.setenv(VAULT_PATH_ENV, str(vault))
    packs._load.cache_clear()
    initialize_database()
    with TestClient(create_app()) as test_client:
        yield test_client
    packs._load.cache_clear()


def test_status_is_read_only_and_minimal(client: TestClient) -> None:
    body = client.get("/ai/consult/status").json()
    assert body["mode"] == "ask"
    assert body["reason"] is None
    assert body["brief_approved"] is True
    assert body["trusted_accounts"] == [{"id": "sub", "training_off_verified_on": "2026-11-25"}]
    assert body["current_brief_digest"] == DIGEST
    assert body["audit_items"] == []
    assert client.post("/ai/consult/status").status_code == 405


def test_pending_packet_approve_without_transport_and_reject(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "TRANSPORT", None)
    with open_sqlite_connection() as connection:
        pending = request_consult(
            connection, ConsultRequest(kind="ideas", question="How do roots find water?", modules=["M2"]),
            transport=FakeTransport())
        connection.commit()
    record = client.get(f"/ai/consult/{pending.consult_id}").json()
    assert record["state"] == "awaiting_approval" and record["modules"] == ["M2"]
    assert "irrigation" in record["packet_text"]
    approved = client.post(f"/ai/consult/{pending.consult_id}/approve",
                           json={"packet_sha256": record["packet_sha256"]}).json()
    assert approved["code"] == "transport_unavailable"
    edited = client.post(f"/ai/consult/{pending.consult_id}/edit", json={"question": "Why, briefly?"}).json()
    assert edited["status"] == "pending"
    assert client.post(f"/ai/consult/{pending.consult_id}/reject").json()["code"] == "not_awaiting_approval"
    assert client.post(f"/ai/consult/{edited['consult_id']}/reject").json()["status"] == "rejected"
    assert client.get("/ai/consult/consult-missing").status_code == 404
    assert client.get(f"/ai/consult/{pending.consult_id}/answer").json()["status"] == "not_found"


def test_pending_packet_approve_with_fake_transport(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_CONSULT_FAKE_TRANSPORT", "1")
    with open_sqlite_connection() as connection:
        pending = request_consult(
            connection, ConsultRequest(kind="ideas", question="How do roots find water?", modules=["M2"]),
            transport=FakeTransport())
        connection.commit()
    record = client.get(f"/ai/consult/{pending.consult_id}").json()
    assert record["state"] == "awaiting_approval"
    approved = client.post(f"/ai/consult/{pending.consult_id}/approve",
                           json={"packet_sha256": record["packet_sha256"]}).json()
    assert approved["status"] == "answered"
    ans = client.get(f"/ai/consult/{pending.consult_id}/answer").json()
    assert ans["status"] == "answered"
    assert "simulated" in ans["answer"]
