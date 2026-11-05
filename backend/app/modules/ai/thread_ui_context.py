"""Bubu page awareness: DOM-first UI context and optional screenshots for thread turns.

The page is described to the model as a bounded list of targets it may point at.
Nothing here lets the model act on the page; the frontend parses ``[[point:<id>]]``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.ai.thread_app_map import app_map_for_route

MAX_TARGETS = 150
MAX_IMAGES = 2
MAX_IMAGE_BYTES = 6 * 1024 * 1024
VISIBLE_PAGE_SOURCE = "jarvis:visible-page"
# Head + target list only; the navigation guide and the pointing rules come on top (see render_visible_page_block).
_BLOCK_CHAR_BUDGET = 4700
APP_MAP_HEADER = "How to get around this app (reference data; the screen may differ from it):"
POINTING_INSTRUCTIONS = (
    "To show the user where something is, write [[point:<id>]] or [[point:<id>|1. CFD]] (the label is optional) "
    "using only ids listed above; never invent an id. "
    "The user sees the directive as the item's name, so put it where the name belongs (\"Click <directive>, then "
    "choose Results\") and do not also write the name next to it. "
    "When what the user wants is not on screen yet, answer with the steps in order and point only at the FIRST step "
    "the user can do right now, using an id from the list above; name the later steps in words (\"then choose "
    "Results\"). With several steps, number the step in the label as in the example above. "
    "When the user asks about simulation results, do not point at Library entries: the Library holds published "
    "models, not results. Point only when the user asks where something is. "
    "You cannot click or change anything on the page."
)
_IMAGE_RE = re.compile(r"^data:(image/png|image/jpeg);base64,([A-Za-z0-9+/]+={0,2})$")
_IMAGE_MAGIC = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}
_IMAGE_EXTENSION = {"image/png": "png", "image/jpeg": "jpg"}
_POINTABLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/\-]{0,79}$")


def _clean_text(value: object) -> object:
    """Control characters become spaces and whitespace collapses; page text is data."""
    if not isinstance(value, str):
        return value
    text = "".join(" " if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} else char for char in value)
    return " ".join(text.split())


def _defuse_directives(value: str) -> str:
    # Page text must not be able to forge a pointer directive in the model's context.
    return value.replace("[[", "[").replace("]]", "]")


class UiTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    role: str = Field(max_length=40)
    name: str = Field(max_length=160)
    text: str | None = Field(default=None, max_length=160)

    _clean = field_validator("id", "role", "name", "text", mode="before")(_clean_text)


class UiContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: str = Field(max_length=300)
    title: str = Field(default="", max_length=200)
    targets: list[UiTarget] = Field(default_factory=list, max_length=MAX_TARGETS)

    _clean = field_validator("route", "title", mode="before")(_clean_text)


def validate_images(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if len(value) > MAX_IMAGES:
        raise ValueError(f"at most {MAX_IMAGES} images are allowed")
    for item in value:
        decode_image(item)
    return value


def decode_image(data_url: str) -> tuple[str, bytes]:
    """Return (mime_type, bytes) for a PNG/JPEG data URL, or raise ValueError."""
    if not isinstance(data_url, str) or len(data_url) > (MAX_IMAGE_BYTES * 4) // 3 + 64:
        raise ValueError("image is too large")
    match = _IMAGE_RE.fullmatch(data_url)
    if match is None:
        raise ValueError("images must be image/png or image/jpeg base64 data URLs")
    mime, encoded = match.group(1), match.group(2)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image is not valid base64") from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("image is empty or larger than 6 MB")
    if not raw.startswith(_IMAGE_MAGIC[mime]):
        raise ValueError("image bytes do not match the declared type")
    return mime, raw


def image_extension(mime: str) -> str:
    return _IMAGE_EXTENSION[mime]


def images_digest(images: list[str]) -> list[str]:
    return ["sha256:" + hashlib.sha256(image.encode("ascii")).hexdigest() for image in images]


def render_visible_page_block(ui: UiContext) -> str:
    """Compact, bounded description of the page plus the pointing protocol."""
    title = _defuse_directives(ui.title) or "Untitled"
    head = [f"Visible page: {title} ({_defuse_directives(ui.route)})",
            "Targets you can point at (id · role · name):"]
    budget = _BLOCK_CHAR_BUDGET - len("\n".join(head)) - 40
    lines: list[str] = []
    seen: set[str] = set()
    omitted = 0
    for target in ui.targets:
        if target.id in seen or not _POINTABLE_ID_RE.fullmatch(target.id):
            continue
        seen.add(target.id)
        name = _defuse_directives(target.name) or "-"
        text = _defuse_directives(target.text or "")
        line = f"- {target.id} · {target.role or 'element'} · {name}"
        if text and text != name:
            line += f" · {text}"
        if budget - len(line) - 1 < 0:
            omitted += 1
            continue
        budget -= len(line) + 1
        lines.append(line)
    if omitted:
        lines.append(f"(+{omitted} more targets not listed)")
    if not lines:
        lines.append("(none)")
    guide = app_map_for_route(ui.route)
    parts = [*head, *lines]
    if guide:
        parts += [APP_MAP_HEADER, _defuse_directives(guide)]
    return "\n".join([*parts, POINTING_INSTRUCTIONS])
