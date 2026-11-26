"""Deterministic zone-A gate over the text Inkling writes for a consult (spec 380, directive §3.1–3.3).

The gate never writes or rewrites text: it passes it, asks Inkling for a rewrite naming what to change, or refuses.
It runs over the question and context fields only; the approved brief and the fixed template are not Inkling's text.
Every step fails closed: an exception is a refusal with code ``gate_error``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.core.packs import KNOWN_SEARCH_GUARD_UNITS
from app.modules.ai.cloud_escalation import screen_outbound_text

TAINT_TOLERANCE_REL = 0.02
VERBATIM_SPAN = 200
MAX_VAULT_TEXT_BYTES = 2 << 20
_VAULT_TEXT_SUFFIXES = {".md", ".txt", ".toml", ".json", ".csv", ".yaml", ".yml", ".py", ".tex"}
_FILE_SUFFIXES = (".step", ".stp", ".stl", ".3mf", ".iges", ".igs", ".fcstd", ".msh", ".vtk", ".vtu", ".foam",
                  ".inp", ".obj", ".glb")

# Physical constants that may appear in any physics question; a match with a project number is logged, not blocked.
PHYSICAL_CONSTANTS = (9.80665, 9.81, 8.314462618, 1.380649e-23, 6.02214076e23, 2.99792458e8, 6.62607015e-34,
                      5.670374419e-8, 96485.33212, 273.15, 101325.0, 1.602176634e-19, 8.8541878128e-12)

_NUMBER = r"[-+]?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:[eE][-+]?\d+)?"
_NUMBER_RE = re.compile(rf"(?<![\w.,]){_NUMBER}(?![\w])|(?<![\w.,]){_NUMBER}(?=[a-zA-Z°µ%])")
_POWER_OF_TEN_RE = re.compile(r"(?<![\w.])10\s*\^\s*[-+]?\d+|(?<![\w.])1[eE][-+]?\d+(?![\w.])|10[⁰¹²³⁴⁵⁶⁷⁸⁹⁻]+")
_RANGE_RE = re.compile(rf"({_NUMBER})\s*[a-zA-Z°µ%/0-9]*\s*(?:-|–|—|\bto\b|\ba\b|÷)\s*({_NUMBER})")
_LITERATURE_RE = re.compile(rf"({_NUMBER}(?:\s*(?:-|–|—|\bto\b)\s*{_NUMBER})?)\s*[^\s{{}}]{{0,12}}\s*\{{lit:[^}}\n]{{3,200}}\}}")

_IDENTITY_PATTERNS = (
    ("tax_code", re.compile(r"\b[A-Z]{6}\d{2}[A-EHLMPR-T]\d{2}[A-Z]\d{3}[A-Z]\b", re.I)),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("phone", re.compile(r"(?<![\w.])(?:\+\d{1,3}[\s.-]?\d{2,4}(?:[\s.-]?\d{2,4}){2,3}|3\d{2}[\s.-]?\d{3}[\s.-]?\d{3,4})(?![\w.])")),
    ("birth_date", re.compile(r"\b(?:nat[oa] il|data di nascita|born on|date of birth)\b", re.I)),
    ("address", re.compile(r"\b(?:Via|Viale|Piazza|Corso|Largo|Vicolo)\s+[A-Z][\w'.]+(?:\s+[A-Z][\w'.]+)*,?\s+\d{1,4}\b")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z]\d{10}[0-9A-Z]{12}\b")),
    ("id_document", re.compile(r"\b(?:C[A-Z]\d{5}[A-Z]{2}|[A-Z]{2}\d{7})\b")),
)

Status = Literal["pass", "rewrite_required", "refused"]


@dataclass(frozen=True)
class StepResult:
    step: str
    result: Literal["pass", "rewrite_required", "refused"]
    reason: str | None = None
    hits: tuple[str, ...] = ()
    ai_job_id: str | None = None


@dataclass(frozen=True)
class GateResult:
    status: Status
    code: str | None
    steps: tuple[StepResult, ...]
    # The numbers or spans Inkling must change (rewrite_required) - from its own text, never from a source.
    offending: tuple[str, ...] = ()
    literature_numbers: tuple[str, ...] = ()
    constant_numbers: tuple[str, ...] = ()


@dataclass(frozen=True)
class GateSources:
    """What counts as specific: this turn's tool outputs, stored project numbers and protected dimensions."""

    tool_outputs: tuple[str, ...] = ()
    stored_numbers: tuple[float, ...] = ()
    protected_values: tuple[float, ...] = ()
    vault_dir: Path | None = None
    data_root: Path | None = None
    pack_roots: tuple[Path, ...] = ()
    extra_texts: tuple[str, ...] = field(default_factory=tuple)


def _to_float(text: str) -> float | None:
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _significant(token: str, value: float, followed_by_unit: bool) -> bool:
    """Small bare integers (list numbers, "2 rounds") are not compared; anything with a unit or a fraction is."""
    if followed_by_unit:
        return value != 0
    if re.fullmatch(r"[-+]?\d+", token):
        return abs(value) >= 10
    return value != 0


def _matches(value: float, references: Iterable[float], *, exact_int: bool = False) -> bool:
    for reference in references:
        if reference == 0:
            continue
        if exact_int and float(value).is_integer() and float(reference).is_integer():
            if value == reference:
                return True
            continue
        if abs(value - reference) <= TAINT_TOLERANCE_REL * abs(reference) + 1e-12:
            return True
    return False


def _mask(text: str, spans: Iterable[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _wide_range_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for match in _RANGE_RE.finditer(text):
        low, high = _to_float(match.group(1)), _to_float(match.group(2))
        if low is None or high is None:
            continue
        low, high = sorted((abs(low), abs(high)))
        if low > 0 and high / low >= 2:
            spans.append(match.span())
    return spans


def source_numbers(texts: Iterable[str]) -> list[float]:
    numbers = []
    for text in texts:
        for match in _NUMBER_RE.finditer(text):
            value = _to_float(match.group(0))
            if value is not None and value != 0:
                numbers.append(value)
    return numbers


def number_taint(text: str, sources: GateSources) -> tuple[StepResult, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    references = [*source_numbers(sources.tool_outputs), *sources.stored_numbers, *sources.protected_values]
    literature: list[str] = []
    spans: list[tuple[int, int]] = []
    for match in _LITERATURE_RE.finditer(text):
        literature.append(match.group(1))
        spans.append(match.span())
    spans.extend(match.span() for match in _POWER_OF_TEN_RE.finditer(text))
    masked = _mask(text, spans)
    spans = _wide_range_spans(masked)
    masked = _mask(masked, spans)
    offending: list[str] = []
    constants: list[str] = []
    for match in _NUMBER_RE.finditer(masked):
        token = match.group(0)
        value = _to_float(token)
        if value is None:
            continue
        unit = re.match(r"\s*([a-zA-Z°µ%][a-zA-Z°µ%/0-9]*)", masked[match.end():match.end() + 16])
        followed = unit is not None and unit.group(1).lower().rstrip(".,;:") in KNOWN_SEARCH_GUARD_UNITS
        exact_int = not followed and re.fullmatch(r"[-+]?\d+", token) is not None
        if not _significant(token, value, followed) or not _matches(abs(value), [abs(r) for r in references],
                                                                    exact_int=exact_int):
            continue
        if _matches(abs(value), PHYSICAL_CONSTANTS):
            constants.append(token)
            continue
        offending.append(token)
    if offending:
        reason = ("These numbers match project results or protected values: " + ", ".join(dict.fromkeys(offending))
                  + ". Write an order of magnitude or a range spanning at least x2 instead.")
        return StepResult("number_taint", "rewrite_required", reason), tuple(dict.fromkeys(offending)), \
            tuple(literature), tuple(constants)
    return StepResult("number_taint", "pass"), (), tuple(literature), tuple(constants)


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _windows(text: str, size: int) -> set[int]:
    return {hash(text[index:index + size]) for index in range(0, max(len(text) - size + 1, 0))}


_vault_cache: dict[tuple[str, int, int], set[int]] = {}


def _vault_text_windows(vault_dir: Path, data_root: Path | None, size: int) -> set[int]:
    windows: set[int] = set()
    skip = data_root.resolve() if data_root else None
    for path in sorted(vault_dir.rglob("*")):
        try:
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in _VAULT_TEXT_SUFFIXES:
                continue
            resolved = path.resolve()
            if skip is not None and (resolved == skip or skip in resolved.parents):
                continue
            stat = resolved.stat()
            if stat.st_size > MAX_VAULT_TEXT_BYTES:
                continue
            key = (str(resolved), stat.st_size, stat.st_mtime_ns)
            if key not in _vault_cache:
                _vault_cache[key] = _windows(_normalise(resolved.read_text(encoding="utf-8", errors="ignore")), size)
            windows |= _vault_cache[key]
        except OSError:
            continue
    return windows


def files_and_verbatim(text: str, sources: GateSources) -> StepResult:
    lowered = text.lower()
    roots = [root for root in (sources.vault_dir, sources.data_root, *sources.pack_roots) if root is not None]
    for root in roots:
        for form in {str(root), str(root.expanduser().resolve())}:
            if form and form != "/" and form.lower() in lowered:
                return StepResult("files_and_verbatim", "refused", "private_path")
    if re.search(r"(?<![\w.])(?:[\w-]+\.)+(?:" + "|".join(s[1:] for s in _FILE_SUFFIXES) + r")\b", lowered):
        return StepResult("files_and_verbatim", "refused", "file_reference")
    size = VERBATIM_SPAN + 1
    packet = _windows(_normalise(text), size)
    if not packet:
        return StepResult("files_and_verbatim", "pass")
    source_windows: set[int] = set()
    for output in (*sources.tool_outputs, *sources.extra_texts):
        source_windows |= _windows(_normalise(output), size)
    if packet & source_windows:
        return StepResult("files_and_verbatim", "refused", "verbatim_tool_output")
    if sources.vault_dir is not None and packet & _vault_text_windows(sources.vault_dir, sources.data_root, size):
        return StepResult("files_and_verbatim", "refused", "verbatim_vault_text")
    return StepResult("files_and_verbatim", "pass")


def secrets_and_identity(text: str) -> StepResult:
    status, _level, code, _reason = screen_outbound_text(text)
    if status == "refused" and code == "secret_detected":
        return StepResult("secrets_and_identity", "refused", "secret_detected")
    for name, pattern in _IDENTITY_PATTERNS:
        if pattern.search(text):
            return StepResult("secrets_and_identity", "refused", f"personal_{name}")
    return StepResult("secrets_and_identity", "pass")


def run_gate(question: str, context: str, sources: GateSources) -> GateResult:
    text = f"{question}\n\n{context}"
    steps: list[StepResult] = []
    try:
        first = secrets_and_identity(text)
        steps.append(first)
        if first.result != "pass":
            return GateResult("refused", first.reason, tuple(steps))
        taint, offending, literature, constants = number_taint(text, sources)
        steps.append(taint)
        if taint.result != "pass":
            return GateResult("rewrite_required", "number_taint", tuple(steps), offending, literature, constants)
        third = files_and_verbatim(text, sources)
        steps.append(third)
        if third.result != "pass":
            return GateResult("refused", third.reason, tuple(steps), (), literature, constants)
        from app.modules.consult.judge import run_local_judge

        # Step 4 always runs: without a readable vault it refuses (vault_absent / gate_error).
        fourth = run_local_judge(question, context, vault_dir=sources.vault_dir)
        steps.append(StepResult("local_only_judge", fourth.status, fourth.reason, hits=fourth.hits, ai_job_id=fourth.ai_job_id))
        if fourth.status == "refused":
            return GateResult("refused", fourth.code or "gate_error", tuple(steps), (), literature, constants)
        if fourth.status == "rewrite_required":
            return GateResult("rewrite_required", "local_only", tuple(steps), fourth.hits, literature, constants)
        return GateResult("pass", None, tuple(steps), (), literature, constants)
    except Exception as exc:  # noqa: BLE001 - every unexpected failure refuses
        steps.append(StepResult("gate", "refused", f"gate_error:{type(exc).__name__}"))
        return GateResult("refused", "gate_error", tuple(steps))
