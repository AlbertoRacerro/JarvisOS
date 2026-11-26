"""The consultant brief from the pack, approved by its exact digest (spec 380).

The pack names the brief and its nucleus heading in ``[consult]``. The nucleus is the level-2 section with that heading,
up to the next level-2 heading. A module is a block that starts with ``**M<n> — `` and runs to the next module or
level-2 heading. The core holds no brief text.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from app.core import packs

_MODULE_START = re.compile(r"^\*\*(M[1-9][0-9]?) — ")
_LEVEL2 = re.compile(r"^## (?!#)")


class BriefUnavailable(ValueError):
    """No usable approved brief; carries a short reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Brief:
    sha256: str
    nucleus: str
    modules: dict[str, str] = field(default_factory=dict)

    @property
    def version(self) -> str:
        return self.sha256[:12]

    def packet_part(self, module_ids: list[str]) -> str:
        return "\n\n".join([self.nucleus, *(self.modules[module_id] for module_id in module_ids)])


def parse_brief(text: str, nucleus_heading: str) -> tuple[str, dict[str, str]]:
    lines = text.splitlines()
    nucleus: list[str] | None = None
    modules: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in lines:
        if _LEVEL2.match(line):
            current = None
            if line[3:].strip() == nucleus_heading.strip() and nucleus is None:
                nucleus = [line]
                current = nucleus
            continue
        start = _MODULE_START.match(line)
        if start:
            if start.group(1) in modules:
                raise BriefUnavailable("brief_module_duplicate")
            current = modules[start.group(1)] = [line]
            continue
        if current is not None:
            current.append(line)
    if not nucleus or not "\n".join(nucleus[1:]).strip():
        raise BriefUnavailable("brief_nucleus_missing")
    return "\n".join(nucleus).strip(), {key: "\n".join(value).strip() for key, value in modules.items()}


def approved_brief(brief_sha256: str | None) -> Brief:
    """The pack brief when its bytes match the maintainer's digest; otherwise :class:`BriefUnavailable`."""
    found = [pack for pack in packs.load_packs() if pack.consult_brief_file is not None]
    if not found:
        raise BriefUnavailable("brief_absent")
    if len(found) > 1:
        raise BriefUnavailable("brief_ambiguous")
    pack = found[0]
    assert pack.consult_brief_file is not None and pack.consult_nucleus_heading is not None
    try:
        raw = pack.consult_brief_file.read_bytes()
    except OSError:
        raise BriefUnavailable("brief_unreadable") from None
    digest = hashlib.sha256(raw).hexdigest()
    if brief_sha256 is None or digest != brief_sha256:
        raise BriefUnavailable("brief_not_approved")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BriefUnavailable("brief_unreadable") from None
    nucleus, modules = parse_brief(text, pack.consult_nucleus_heading)
    return Brief(sha256=digest, nucleus=nucleus, modules=modules)


def current_brief_digest() -> str | None:
    """The digest the maintainer would write to approve the brief as it is now (shown in Settings)."""
    found = [pack for pack in packs.load_packs() if pack.consult_brief_file is not None]
    if len(found) != 1 or found[0].consult_brief_file is None:
        return None
    try:
        return hashlib.sha256(found[0].consult_brief_file.read_bytes()).hexdigest()
    except OSError:
        return None
