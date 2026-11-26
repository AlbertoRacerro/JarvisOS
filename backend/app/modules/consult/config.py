"""Maintainer-owned consult zone and mode config (spec 380).

The file is ``consult.toml`` in the vault named by ``JARVISOS_VAULT_PATH``, next to ``jarvis_vault.toml``. Only the
maintainer writes it: no route, tool or model output does. Anything missing, unreadable or unknown means ``off``::

    consult_mode = "ask"            # off | ask | auto
    brief_sha256 = "<hex>"          # the maintainer's approval of the pack brief's exact bytes

    [[trusted_accounts]]
    id = "claude-subscription"
    agent = "claude"                # a key of configs/relay_gateway.json agents
    model = "claude-opus-5-5"
    training_off_verified_on = "2026-11-25"

A trusted account without a valid ``training_off_verified_on`` date is zone C and never used for a consult.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from app.core.vault import VAULT_PATH_ENV

CONSULT_CONFIG = "consult.toml"
ConsultMode = Literal["off", "ask", "auto"]
_MODES = ("off", "ask", "auto")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class TrustedAccount:
    id: str
    agent: str
    model: str
    training_off_verified_on: date


@dataclass(frozen=True)
class ConsultConfig:
    mode: ConsultMode = "off"
    brief_sha256: str | None = None
    trusted_accounts: tuple[TrustedAccount, ...] = ()
    sage_model_id: str | None = None
    # Why the mode is off when the file asked for more, as a short code; None when the file was read as written.
    reason: str | None = None


def _account(entry: object) -> TrustedAccount | None:
    if not isinstance(entry, dict) or set(entry) != {"id", "agent", "model", "training_off_verified_on"}:
        return None
    account_id, agent, model = entry["id"], entry["agent"], entry["model"]
    verified = entry["training_off_verified_on"]
    if isinstance(verified, datetime):
        return None
    if not all(isinstance(value, str) and _ID.match(value) for value in (account_id, agent, model)):
        return None
    if isinstance(verified, str):
        try:
            verified = date.fromisoformat(verified)
        except ValueError:
            return None
    if not (isinstance(verified, date) and not isinstance(verified, datetime)) or verified > date.today():
        return None
    return TrustedAccount(id=account_id, agent=agent, model=model, training_off_verified_on=verified)


def read_consult_config(vault_dir: Path | None) -> ConsultConfig:
    if vault_dir is None:
        return ConsultConfig(reason="vault_absent")
    try:
        data = tomllib.loads((vault_dir.expanduser() / CONSULT_CONFIG).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ConsultConfig(reason="config_absent")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return ConsultConfig(reason="config_unreadable")
    if set(data) - {"consult_mode", "brief_sha256", "trusted_accounts", "sage_model_id"}:
        return ConsultConfig(reason="config_unknown_key")
    digest = data.get("brief_sha256")
    digest = digest.lower() if isinstance(digest, str) and _SHA256.match(digest.lower()) else None
    sage_model = data.get("sage_model_id")
    if sage_model is not None:
        if not isinstance(sage_model, str) or not sage_model.strip() or not _ID.match(sage_model.strip()):
            return ConsultConfig(reason="config_unreadable")
        sage_model = sage_model.strip()
    raw_accounts = data.get("trusted_accounts", [])
    accounts = tuple(account for account in map(_account, raw_accounts if isinstance(raw_accounts, list) else [])
                     if account is not None)
    mode = data.get("consult_mode")
    if mode not in _MODES:
        return ConsultConfig(brief_sha256=digest, trusted_accounts=accounts, sage_model_id=sage_model, reason="mode_unknown")
    return ConsultConfig(mode=mode, brief_sha256=digest, trusted_accounts=accounts, sage_model_id=sage_model)


def load_consult_config() -> ConsultConfig:
    """Read on every call: the maintainer's edit takes effect at the next consult, and nothing caches a wider mode."""
    value = os.environ.get(VAULT_PATH_ENV, "").strip()
    return read_consult_config(Path(value) if value else None)
