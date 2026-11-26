"""Optional private vault overlay (spec 376).

The vault is a directory outside the repository and the pack that holds the operator's specific material. It is
named by ``JARVISOS_VAULT_PATH`` and must contain ``jarvis_vault.toml``::

    [[protected_dimensions]]     # same schema as a pack's search_guard.protected_dimensions
    value = 12.5
    unit = "mm"

    [[local_only]]               # items that must never leave the machine
    id = "L1"
    text = "..."

    [[protected_propositions]]
    id = "P1"
    text = "..."

Every key is optional. An unset variable means no vault. A set variable whose vault cannot be read raises
:class:`VaultError`; error messages name the file and key, never a value or text from it.
"""

import math
import os
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from app.core.packs import KNOWN_SEARCH_GUARD_UNITS, ProtectedDimension

VAULT_PATH_ENV = "JARVISOS_VAULT_PATH"
VAULT_MANIFEST = "jarvis_vault.toml"
MAX_ITEM_TEXT = 2000

_ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_KNOWN_KEYS = {"protected_dimensions", "local_only", "protected_propositions"}


class VaultError(RuntimeError):
    """The vault named by JARVISOS_VAULT_PATH is missing or invalid."""


@dataclass(frozen=True)
class Vault:
    protected_dimensions: tuple[ProtectedDimension, ...] = ()
    local_only: tuple[tuple[str, str], ...] = ()
    protected_propositions: tuple[tuple[str, str], ...] = ()


def _dimensions(raw: object, where: str) -> tuple[ProtectedDimension, ...]:
    if not isinstance(raw, list):
        raise VaultError(f"{where}: protected_dimensions must be a list of tables")
    out: list[ProtectedDimension] = []
    for index, entry in enumerate(raw):
        at = f"{where}: protected_dimensions[{index}]"
        if not isinstance(entry, dict):
            raise VaultError(f"{at} must be a table")
        if set(entry) - {"value", "unit", "tolerance_rel"}:
            raise VaultError(f"{at} has unknown keys")
        value = entry.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise VaultError(f"{at}.value must be a positive number")
        unit = entry.get("unit")
        if not isinstance(unit, str) or unit.strip().lower() not in KNOWN_SEARCH_GUARD_UNITS:
            raise VaultError(f"{at}.unit must be a known unit")
        tol = entry.get("tolerance_rel", 0.02)
        if isinstance(tol, bool) or not isinstance(tol, (int, float)) or not math.isfinite(tol) or not 0 <= tol < 1:
            raise VaultError(f"{at}.tolerance_rel must be a non-negative number below 1")
        out.append(ProtectedDimension(value=float(value), unit=unit.strip().lower(), tolerance_rel=float(tol)))
    return tuple(out)


def _items(raw: object, key: str, where: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list):
        raise VaultError(f"{where}: {key} must be a list of tables")
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for index, entry in enumerate(raw):
        at = f"{where}: {key}[{index}]"
        if not isinstance(entry, dict) or set(entry) != {"id", "text"}:
            raise VaultError(f"{at} must be a table with exactly id and text")
        item_id, text = entry["id"], entry["text"]
        if not isinstance(item_id, str) or not _ITEM_ID.match(item_id):
            raise VaultError(f"{at}.id is not a valid id")
        if item_id in seen:
            raise VaultError(f"{at}.id is a duplicate")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_ITEM_TEXT:
            raise VaultError(f"{at}.text must be non-empty and at most {MAX_ITEM_TEXT} characters")
        seen.add(item_id)
        out.append((item_id, text.strip()))
    return tuple(out)


def read_vault(directory: Path) -> Vault:
    manifest = directory.expanduser() / VAULT_MANIFEST
    where = VAULT_MANIFEST
    if not manifest.is_file():
        raise VaultError(f"{VAULT_PATH_ENV} is set but its directory has no {VAULT_MANIFEST}")
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise VaultError(f"{where}: cannot be read ({type(exc).__name__})") from None
    if set(data) - _KNOWN_KEYS:
        raise VaultError(f"{where}: unknown top-level keys")
    return Vault(
        protected_dimensions=_dimensions(data.get("protected_dimensions", []), where),
        local_only=_items(data.get("local_only", []), "local_only", where),
        protected_propositions=_items(data.get("protected_propositions", []), "protected_propositions", where),
    )


@lru_cache(maxsize=4)
def _load(path_value: str) -> Vault | None:
    return read_vault(Path(path_value)) if path_value else None


def load_vault() -> Vault | None:
    """The vault named by ``JARVISOS_VAULT_PATH`` (cached per value); None when the variable is unset."""
    return _load(os.environ.get(VAULT_PATH_ENV, "").strip())


def vault_status() -> Literal["absent", "loaded"]:
    return "loaded" if load_vault() is not None else "absent"


def vault_protected_dimensions() -> tuple[ProtectedDimension, ...]:
    vault = load_vault()
    return vault.protected_dimensions if vault else ()


def local_only_items() -> tuple[tuple[str, str], ...]:
    vault = load_vault()
    return vault.local_only if vault else ()


def protected_propositions() -> tuple[tuple[str, str], ...]:
    vault = load_vault()
    return vault.protected_propositions if vault else ()
