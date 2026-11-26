"""Spec 380: consult config, approved brief and the deterministic zone-A gate (offline, fake pack and vault)."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.core import packs
from app.modules.consult.brief import BriefUnavailable, approved_brief, current_brief_digest
from app.modules.consult.config import read_consult_config
from app.modules.consult.gate import GateSources, run_gate

BRIEF = """# Fake brief

Draft notes for the maintainer, not part of the packet.

## Nucleus

You are a consultant for an orchard research project.

## Modules

**M1 — Soil.** Questions about soil and roots.

**M2 — Water.** Questions about irrigation.
"""


def _write_vault(tmp_path: Path, body: str) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    (vault / "consult.toml").write_text(body, encoding="utf-8")
    return vault


def test_config_defaults_to_off(tmp_path: Path) -> None:
    assert read_consult_config(None).mode == "off"
    assert read_consult_config(tmp_path).reason == "config_absent"
    assert read_consult_config(_write_vault(tmp_path, "consult_mode = 'always'\n")).mode == "off"
    assert read_consult_config(_write_vault(tmp_path, "consult_mode = [\n")).reason == "config_unreadable"
    assert read_consult_config(_write_vault(tmp_path, "consult_mode = 'ask'\nextra = 1\n")).mode == "off"


def test_config_reads_mode_and_only_dated_accounts(tmp_path: Path) -> None:
    future = (date.today() + timedelta(days=3)).isoformat()
    vault = _write_vault(tmp_path, f"""consult_mode = "ask"
brief_sha256 = "{'A' * 64}"
[[trusted_accounts]]
id = "sub"
agent = "claude"
model = "claude-opus-5-5"
training_off_verified_on = 2026-11-25
[[trusted_accounts]]
id = "undated"
agent = "claude"
model = "claude-opus-5-5"
training_off_verified_on = ""
[[trusted_accounts]]
id = "future"
agent = "claude"
model = "claude-opus-5-5"
training_off_verified_on = "{future}"
[[trusted_accounts]]
id = "with_time"
agent = "claude"
model = "claude-opus-5-5"
training_off_verified_on = 2026-11-25T12:00:00Z
""")
    config = read_consult_config(vault)
    assert config.mode == "ask" and config.brief_sha256 == "a" * 64
    assert [account.id for account in config.trusted_accounts] == ["sub"]



@pytest.fixture(autouse=True)
def _offline_local_only_judge(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate step 4 (spec 381) needs a readable vault; give each test an empty one and the offline judge fake."""
    from app.core import vault as _vault

    empty = tmp_path_factory.mktemp("empty_vault")
    (empty / "jarvis_vault.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv(_vault.VAULT_PATH_ENV, str(empty))
    monkeypatch.setenv("JARVISOS_CONSULT_FAKE_TRANSPORT", "1")
    _vault._load.cache_clear()
    from app.core.database import initialize_database as _init_db

    _init_db()

@pytest.fixture()
def brief_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "brief.md").write_text(BRIEF, encoding="utf-8")
    (pack / "jarvis_pack.toml").write_text(
        'id = "orchard"\n[consult]\nbrief = "brief.md"\nnucleus_heading = "Nucleus"\n', encoding="utf-8")
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(pack))
    packs._load.cache_clear()
    yield pack
    packs._load.cache_clear()


def test_brief_needs_the_exact_approved_digest(brief_pack: Path) -> None:
    digest = hashlib.sha256(BRIEF.encode()).hexdigest()
    assert current_brief_digest() == digest
    with pytest.raises(BriefUnavailable, match="brief_not_approved"):
        approved_brief(None)
    brief = approved_brief(digest)
    assert brief.nucleus.startswith("## Nucleus") and "Draft notes" not in brief.nucleus
    assert set(brief.modules) == {"M1", "M2"} and brief.modules["M2"].startswith("**M2 — Water.**")
    assert "Questions about soil" in brief.packet_part(["M1"]) and "irrigation" not in brief.packet_part(["M1"])
    (brief_pack / "brief.md").write_text(BRIEF + "\nedited\n", encoding="utf-8")
    with pytest.raises(BriefUnavailable, match="brief_not_approved"):
        approved_brief(digest)


def test_brief_absent_without_a_pack_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(packs.PACK_PATH_ENV, raising=False)
    packs._load.cache_clear()
    with pytest.raises(BriefUnavailable, match="brief_absent"):
        approved_brief("a" * 64)


def test_secrets_and_identity_refuse_first() -> None:
    sources = GateSources(tool_outputs=("pressure drop 236.8 Pa",))
    assert run_gate("Why?", "key sk-ant-api03-" + "x" * 40, sources).code == "secret_detected"
    for text in ("write to someone@example.org", "codice RSSMRA85T10A562S", "call +39 345 123 4567",
                 "lives in Via Roma 12", "nato il 3 marzo"):
        result = run_gate("Why?", text, sources)
        assert result.status == "refused" and result.code.startswith("personal_"), text
        assert len(result.steps) == 1


def test_number_from_a_tool_output_needs_a_rewrite_and_a_wide_range_passes() -> None:
    sources = GateSources(tool_outputs=("result: pressure drop 236.8 Pa over 242.1 m",))
    result = run_gate("Is a drop of 240 Pa plausible?", "", sources)
    assert result.status == "rewrite_required" and result.offending == ("240",)
    assert "range spanning at least x2" in result.steps[-1].reason
    assert run_gate("Is a drop of 100-300 Pa plausible?", "", sources).status == "pass"
    assert run_gate("Is a drop of order 10^2 Pa plausible?", "", sources).status == "pass"
    assert run_gate("Is a drop of 200 Pa plausible?", "", sources).status == "pass"  # outside 2 %


def test_stored_and_protected_numbers_are_specific() -> None:
    assert run_gate("A tube of 52 mm?", "", GateSources(protected_values=(52.0,))).status == "rewrite_required"
    assert run_gate("A drop of 242 Pa?", "", GateSources(stored_numbers=(240.0,))).status == "rewrite_required"
    assert run_gate("Rate 0.731 per day?", "", GateSources(stored_numbers=(0.73,))).status == "rewrite_required"
    assert run_gate("Count 12 units?", "", GateSources(stored_numbers=(13.0,))).status == "pass"  # counts exact
    assert run_gate("Two rounds, 3 options", "", GateSources(tool_outputs=("2 3",))).status == "pass"


def test_literature_and_constants_pass_and_are_logged() -> None:
    sources = GateSources(tool_outputs=("h = 0.206 mol/m3", "g = 9.81 m/s2"))
    result = run_gate("Solubility 0.206 mol/m3 {lit:Weiss 1970} and g 9.81 m/s2", "", sources)
    assert result.status == "pass"
    assert result.literature_numbers == ("0.206",) and result.constant_numbers == ("9.81",)


def test_files_paths_and_verbatim_output_refuse(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    note = "The vault note says something long and specific about the design. " * 5
    (vault / "notes" / "n.md").write_text(note, encoding="utf-8")
    output = "Tool output that the model must not paste back into a consult packet. " * 4
    sources = GateSources(tool_outputs=(output,), vault_dir=vault)
    assert run_gate("see", f"{vault}/notes/n.md", sources).code == "private_path"
    assert run_gate("see", "open part_v2.step", sources).code == "file_reference"
    assert run_gate("see", "intro " + output[:220], sources).code == "verbatim_tool_output"
    assert run_gate("see", note[:230], sources).code == "verbatim_vault_text"
    assert run_gate("see", output[:150], sources).status == "pass"
