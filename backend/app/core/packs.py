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
_KNOWN_KEYS = {"id", "workspace", "sensitivity", "ui_labels", "studio"}


class PackError(RuntimeError):
    """A pack directory or manifest is invalid."""


@dataclass(frozen=True)
class Pack:
    id: str
    root: Path
    workspace_slug: str | None = None
    workspace_name: str | None = None
    sensitivity_terms: tuple[str, ...] = ()
    ui_labels: dict[str, str] = field(default_factory=dict)
    studio_examples_dir: Path | None = None


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
    return Pack(
        id=pack_id,
        root=root,
        workspace_slug=_text(workspace, "slug", manifest, pattern=_SLUG),
        workspace_name=_text(workspace, "name", manifest),
        sensitivity_terms=tuple(term.strip() for term in terms),
        ui_labels=dict(labels),
        studio_examples_dir=examples_dir,
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
