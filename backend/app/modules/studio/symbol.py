"""Strict allowlist sanitizer for Studio process-unit SVG symbols (spec 190). Output is re-serialized, never echoed."""

import re
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

MAX_SYMBOL_BYTES = 32 * 1024
_SVG_NS = "http://www.w3.org/2000/svg"
_MAX_ELEMENTS = 200
_MAX_DEPTH = 8
_MAX_TEXT = 200
_ELEMENTS = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "tspan", "title"}
_PAINT = re.compile(r"^(none|currentColor|#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,20}|rgb\(\s*\d{1,3}\s*(,\s*\d{1,3}\s*){2}\))$")
_GEOMETRY = re.compile(r"^[0-9eE+\-.,\s#%a-zA-Z()]{0,4096}$")
_PLAIN = re.compile(r"^[A-Za-z0-9 _\-]{0,64}$")
_PAINT_ATTRS = {"fill", "stroke"}
_GEOMETRY_ATTRS = {"viewBox", "width", "height", "x", "y", "cx", "cy", "r", "rx", "ry", "x1", "y1", "x2", "y2", "points",
                   "d", "transform", "stroke-width", "stroke-dasharray", "opacity", "fill-opacity", "stroke-opacity",
                   "font-size"}
_PLAIN_ATTRS = {"stroke-linecap", "stroke-linejoin", "font-family", "font-weight", "text-anchor", "dominant-baseline"}
_FORBIDDEN_TEXT = ("<!doctype", "<!entity", "<!", "<?", "<![cdata[")


class SymbolError(ValueError):
    pass


def _name(tag: str) -> str:
    namespace, _, local = tag.rpartition("}")
    if namespace not in ("", "{" + _SVG_NS):
        raise SymbolError("Only SVG elements are allowed.")
    return local


def _render(element: ElementTree.Element, depth: int, count: list[int], root: bool) -> str:
    name = _name(element.tag)
    count[0] += 1
    if name not in _ELEMENTS or depth > _MAX_DEPTH or count[0] > _MAX_ELEMENTS:
        raise SymbolError(f"Element {name!r} is not allowed or the symbol is too large.")
    attributes = []
    for key, value in sorted(element.attrib.items()):
        if key.startswith("{") or key in {"xmlns"}:
            raise SymbolError(f"Attribute {key!r} is not allowed.")
        if key in _PAINT_ATTRS:
            valid = _PAINT.fullmatch(value.strip()) is not None
        elif key in _GEOMETRY_ATTRS:
            valid = _GEOMETRY.fullmatch(value) is not None and "url(" not in value.lower()
        elif key in _PLAIN_ATTRS:
            valid = _PLAIN.fullmatch(value) is not None
        else:
            raise SymbolError(f"Attribute {key!r} is not allowed.")
        if not valid:
            raise SymbolError(f"Value of attribute {key!r} is not allowed.")
        attributes.append(f" {key}={quoteattr(value)}")
    if root:
        attributes.insert(0, f' xmlns="{_SVG_NS}"')
    text = (element.text or "").strip()
    if text and (name not in {"text", "tspan", "title"} or len(text) > _MAX_TEXT):
        raise SymbolError("Text is only allowed in text, tspan and title elements, up to 200 characters.")
    if any((child.tail or "").strip() for child in element):
        raise SymbolError("Mixed text content is not allowed.")
    body = escape(text) + "".join(_render(child, depth + 1, count, False) for child in element)
    return f"<{name}{''.join(attributes)}>{body}</{name}>"


def sanitize_symbol(text: str) -> str:
    """Return a canonical safe SVG string, or raise SymbolError. Nothing unsafe is silently dropped."""
    if len(text.encode("utf-8")) > MAX_SYMBOL_BYTES:
        raise SymbolError("The symbol exceeds 32 KiB.")
    lowered = text.lower()
    if any(token in lowered for token in _FORBIDDEN_TEXT):
        raise SymbolError("DOCTYPE, entities, processing instructions and CDATA are not allowed.")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise SymbolError("The symbol is not well-formed SVG.") from exc
    if _name(root.tag) != "svg":
        raise SymbolError("The root element must be svg.")
    return _render(root, 0, [0], True)
