"""Optional project packs.

A pack is a directory (usually its own private repository) that carries everything specific to one
project: the default workspace identity, extra sensitivity terms for the egress guard, UI labels and
Studio example models. JarvisOS runs with zero packs and generic defaults.

Packs are found through the ``JARVISOS_PACK_PATH`` environment variable, a list of directories separated
by ``os.pathsep``. Each directory must contain a ``jarvis_pack.toml``::

    id = "acme"

    [workspace]                  # optional
    slug = "acme"
    name = "Acme"

    [sensitivity]                # optional
    terms = ["acme", "project falcon"]

    [ui_labels]                  # optional, free-form key -> text
    home = "Acme"

    [studio]                     # optional
    examples_dir = "studio_examples"

An invalid pack raises :class:`PackError`; ``app.main`` loads the packs at startup so a broken pack stops
the application instead of silently disappearing.
"""

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

PACK_PATH_ENV = "JARVISOS_PACK_PATH"
PACK_MANIFEST = "jarvis_pack.toml"

DEFAULT_WORKSPACE_SLUG = "main"
DEFAULT_WORKSPACE_NAME = "Workspace"
DEFAULT_WORKSPACE_DESCRIPTION = "Default workspace."

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_KNOWN_KEYS = {"id", "workspace", "sensitivity", "ui_labels", "studio", "hermes_roles", "readiness", "search_guard"}


KNOWN_SEARCH_GUARD_UNITS = frozenset({
    "mm", "cm", "m", "um", "µm", "nm", "km", "in", "inch", "inches", "ft", "feet", "yd",
    "mg", "g", "kg", "t", "oz", "lb",
    "ml", "l", "liter", "liters", "m3", "cm3", "mm3", "m2", "cm2", "mm2",
    "s", "sec", "min", "h", "hr", "hz", "khz", "mhz", "ghz", "rpm",
    "pa", "kpa", "mpa", "gpa", "bar", "mbar", "psi", "atm", "torr",
    "k", "c", "°c", "degc", "f", "degf",
    "n", "kn", "j", "kj", "mj", "w", "kw", "mw", "gw", "hp",
    "m/s", "mm/s", "km/h", "mph", "l/min", "l/s", "l/h", "m3/s", "m3/h",
    "deg", "degree", "degrees", "rad", "mrad",
    "v", "mv", "kv", "a", "ma", "ohm", "kohm", "mohm",
    "uf", "nf", "pf", "mh", "%", "pct",
})


class PackError(RuntimeError):
    """A pack directory or manifest is invalid."""


@dataclass(frozen=True)
class ProtectedDimension:
    value: float
    unit: str
    tolerance_rel: float = 0.02


@dataclass(frozen=True)
class SearchGuardConfig:
    groups: tuple[tuple[str, ...], ...] = ()
    min_groups: int = 4
    exact_combinations: tuple[tuple[str, ...], ...] = ()
    protected_dimensions: tuple[ProtectedDimension, ...] = ()
    allow_generic_ranges: bool = True


@dataclass(frozen=True)
class Pack:
    id: str
    root: Path
    workspace_slug: str | None = None
    workspace_name: str | None = None
    sensitivity_terms: tuple[str, ...] = ()
    ui_labels: dict[str, str] = field(default_factory=dict)
    studio_examples_dir: Path | None = None
    hermes_roles: dict[str, str] = field(default_factory=dict)
    readiness_cases_file: Path | None = None
    search_guard: SearchGuardConfig = field(default_factory=SearchGuardConfig)


def _table(data: dict, key: str, manifest: Path) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise PackError(f"{manifest}: [{key}] must be a table")
    return value


def _text(table: dict, key: str, manifest: Path, *, pattern: re.Pattern | None = None) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PackError(f"{manifest}: {key} must be a non-empty string")
    if pattern is not None and not pattern.match(value):
        raise PackError(f"{manifest}: {key} {value!r} does not match {pattern.pattern}")
    return value.strip() if pattern is None else value


def read_pack(directory: Path) -> Pack:
    root = directory.expanduser().resolve()
    manifest = root / PACK_MANIFEST
    if not manifest.is_file():
        raise PackError(f"{PACK_PATH_ENV} entry {root} has no {PACK_MANIFEST}")
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PackError(f"{manifest}: cannot be read: {exc}") from exc
    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        raise PackError(f"{manifest}: unknown keys {unknown}")
    pack_id = _text(data, "id", manifest, pattern=_ID)
    if pack_id is None:
        raise PackError(f"{manifest}: id is required")
    workspace = _table(data, "workspace", manifest)
    terms = _table(data, "sensitivity", manifest).get("terms", [])
    if not isinstance(terms, list) or not all(isinstance(term, str) and term.strip() for term in terms):
        raise PackError(f"{manifest}: sensitivity.terms must be a list of non-empty strings")
    labels = _table(data, "ui_labels", manifest)
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items()):
        raise PackError(f"{manifest}: ui_labels values must be strings")
    examples = _text(_table(data, "studio", manifest), "examples_dir", manifest)
    examples_dir = None
    if examples is not None:
        examples_dir = (root / examples).resolve()
        if not examples_dir.is_dir() or root not in examples_dir.parents:
            raise PackError(f"{manifest}: studio.examples_dir {examples!r} must be a directory inside the pack")
    roles = _table(data, "hermes_roles", manifest)
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in roles.items()):
        raise PackError(f"{manifest}: hermes_roles values must be strings")
    for role_name, route in roles.items():
        from app.modules.agents.hermes.roles import ROLES
        if role_name not in ROLES:
            raise PackError(f"{manifest}: unknown Hermes role {role_name!r}")
        if not route.startswith("local:"):
            raise PackError(f"{manifest}: hermes_roles.{role_name} route must start with 'local:'")
    readiness = _table(data, "readiness", manifest)
    cases = _text(readiness, "cases", manifest)
    cases_file = None
    if cases is not None:
        cases_file = (root / cases).resolve()
        if not cases_file.is_file() or root not in cases_file.parents:
            raise PackError(f"{manifest}: readiness.cases {cases!r} must be a file inside the pack")
    search_guard_config = SearchGuardConfig()
    if "search_guard" in data:
        guard_table = _table(data, "search_guard", manifest)
        unknown_guard_keys = sorted(
            set(guard_table) - {"groups", "min_groups", "exact_combinations", "protected_dimensions", "allow_generic_ranges"}
        )
        if unknown_guard_keys:
            raise PackError(f"{manifest}: unknown search_guard keys {unknown_guard_keys}")
        raw_groups = guard_table.get("groups")
        groups_list: list[tuple[str, ...]] = []
        if raw_groups is not None:
            if not isinstance(raw_groups, list):
                raise PackError(f"{manifest}: search_guard.groups must be a list of lists of non-empty strings")
            for g in raw_groups:
                if not isinstance(g, list) or not g or not all(isinstance(term, str) and term.strip() for term in g):
                    raise PackError(f"{manifest}: search_guard.groups must be a list of lists of non-empty strings")
                groups_list.append(tuple(term.strip() for term in g))
        raw_min = guard_table.get("min_groups", 4)
        if isinstance(raw_min, bool) or not isinstance(raw_min, int) or raw_min <= 0:
            raise PackError(f"{manifest}: search_guard.min_groups must be a positive integer")

        raw_combinations = guard_table.get("exact_combinations")
        combinations_list: list[tuple[str, ...]] = []
        if raw_combinations is not None:
            if not isinstance(raw_combinations, list):
                raise PackError(f"{manifest}: search_guard.exact_combinations must be a list of lists of non-empty strings")
            for c in raw_combinations:
                if not isinstance(c, list) or not c or not all(isinstance(term, str) and term.strip() for term in c):
                    raise PackError(f"{manifest}: search_guard.exact_combinations must be a list of lists of non-empty strings")
                combinations_list.append(tuple(term.strip() for term in c))

        raw_dimensions = guard_table.get("protected_dimensions")
        dimensions_list: list[ProtectedDimension] = []
        if raw_dimensions is not None:
            if not isinstance(raw_dimensions, list):
                raise PackError(f"{manifest}: search_guard.protected_dimensions must be a list of tables")
            for d in raw_dimensions:
                if not isinstance(d, dict):
                    raise PackError(f"{manifest}: search_guard.protected_dimensions entries must be tables")
                unknown_dim_keys = sorted(set(d) - {"value", "unit", "tolerance_rel"})
                if unknown_dim_keys:
                    raise PackError(f"{manifest}: unknown search_guard.protected_dimensions keys {unknown_dim_keys}")
                val = d.get("value")
                if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
                    raise PackError(f"{manifest}: search_guard.protected_dimensions value must be a positive number")
                unit = d.get("unit")
                if not isinstance(unit, str) or not unit.strip():
                    raise PackError(f"{manifest}: search_guard.protected_dimensions unit must be a non-empty string")
                unit_clean = unit.strip().lower()
                if unit_clean not in KNOWN_SEARCH_GUARD_UNITS:
                    raise PackError(f"{manifest}: search_guard.protected_dimensions unknown unit {unit!r}")
                tol = d.get("tolerance_rel", 0.02)
                if isinstance(tol, bool) or not isinstance(tol, (int, float)) or not math.isfinite(tol) or tol < 0 or tol >= 1.0:
                    raise PackError(f"{manifest}: search_guard.protected_dimensions tolerance_rel must be a non-negative float < 1.0")
                dimensions_list.append(ProtectedDimension(value=float(val), unit=unit_clean, tolerance_rel=float(tol)))

        raw_generic_ranges = guard_table.get("allow_generic_ranges", True)
        if not isinstance(raw_generic_ranges, bool):
            raise PackError(f"{manifest}: search_guard.allow_generic_ranges must be a boolean")

        search_guard_config = SearchGuardConfig(
            groups=tuple(groups_list),
            min_groups=raw_min,
            exact_combinations=tuple(combinations_list),
            protected_dimensions=tuple(dimensions_list),
            allow_generic_ranges=raw_generic_ranges,
        )
    return Pack(
        id=pack_id,
        root=root,
        workspace_slug=_text(workspace, "slug", manifest, pattern=_SLUG),
        workspace_name=_text(workspace, "name", manifest),
        sensitivity_terms=tuple(term.strip() for term in terms),
        ui_labels=dict(labels),
        studio_examples_dir=examples_dir,
        hermes_roles=dict(roles),
        readiness_cases_file=cases_file,
        search_guard=search_guard_config,
    )


@lru_cache(maxsize=8)
def _load(path_value: str) -> tuple[Pack, ...]:
    packs: list[Pack] = []
    for entry in path_value.split(os.pathsep):
        if not entry.strip():
            continue
        pack = read_pack(Path(entry.strip()))
        if any(existing.id == pack.id for existing in packs):
            raise PackError(f"duplicate pack id {pack.id!r} in {PACK_PATH_ENV}")
        packs.append(pack)
    return tuple(packs)


def load_packs() -> tuple[Pack, ...]:
    """Packs named by ``JARVISOS_PACK_PATH`` (cached per value); empty when the variable is unset."""
    return _load(os.environ.get(PACK_PATH_ENV, ""))


def _workspace_pack() -> Pack | None:
    return next((pack for pack in load_packs() if pack.workspace_slug), None)


def default_workspace_slug() -> str:
    """Slug (and id) of the workspace seeded on first boot and used when a request names none."""
    pack = _workspace_pack()
    return pack.workspace_slug if pack and pack.workspace_slug else DEFAULT_WORKSPACE_SLUG


def default_workspace_name() -> str:
    pack = _workspace_pack()
    if pack is None:
        return DEFAULT_WORKSPACE_NAME
    return pack.workspace_name or pack.workspace_slug or DEFAULT_WORKSPACE_NAME


def sensitivity_terms() -> tuple[str, ...]:
    """Lower-cased, de-duplicated project terms from every loaded pack."""
    seen: dict[str, None] = {}
    for pack in load_packs():
        for term in pack.sensitivity_terms:
            seen.setdefault(" ".join(term.lower().split()))
    return tuple(seen)


def mentions_pack_term(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in sensitivity_terms())


def ui_labels() -> dict[str, str]:
    labels: dict[str, str] = {}
    for pack in load_packs():
        labels.update(pack.ui_labels)
    return labels


def studio_example_dirs() -> tuple[Path, ...]:
    return tuple(pack.studio_examples_dir for pack in load_packs() if pack.studio_examples_dir)


def hermes_role_pack_defaults() -> dict[str, str]:
    """Default local routes for Hermes roles from loaded packs (later pack overrides earlier)."""
    defaults: dict[str, str] = {}
    for pack in load_packs():
        defaults.update(pack.hermes_roles)
    return defaults


def readiness_case_files() -> tuple[Path, ...]:
    return tuple(pack.readiness_cases_file for pack in load_packs() if pack.readiness_cases_file)


def search_guard() -> SearchGuardConfig:
    """Combine loaded pack rules using the strictest configured thresholds."""
    all_groups: list[tuple[str, ...]] = []
    thresholds: list[int] = []
    all_combinations: list[tuple[str, ...]] = []
    all_dimensions: list[ProtectedDimension] = []
    allow_generic_ranges = True
    for pack in load_packs():
        if pack.search_guard.groups:
            all_groups.extend(pack.search_guard.groups)
            thresholds.append(pack.search_guard.min_groups)
        if pack.search_guard.exact_combinations:
            all_combinations.extend(pack.search_guard.exact_combinations)
        if pack.search_guard.protected_dimensions:
            all_dimensions.extend(pack.search_guard.protected_dimensions)
        if not pack.search_guard.allow_generic_ranges:
            allow_generic_ranges = False
    return SearchGuardConfig(
        groups=tuple(all_groups),
        min_groups=min(thresholds, default=4),
        exact_combinations=tuple(all_combinations),
        protected_dimensions=tuple(all_dimensions),
        allow_generic_ranges=allow_generic_ranges,
    )
