"""Tests for scripts/check_spec_status.py: spec registry gating rules."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_spec_status.py"
SPEC = importlib.util.spec_from_file_location("check_spec_status", SCRIPT)
assert SPEC and SPEC.loader
css = importlib.util.module_from_spec(SPEC)
sys.modules["check_spec_status"] = css
SPEC.loader.exec_module(css)


def make_registry(*rows: str) -> str:
    header = [
        "# Spec status and roadmap",
        "",
        "## Registry",
        "| Spec | Status | Implementation PR | Name | Depends on | Description |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    return "\n".join(header + list(rows))


def test_unlisted_dependency_parses_ok() -> None:
    """A row depending on an unlisted spec parses OK."""
    text = make_registry(
        "| 042 | in_review | [#100](https://example.com/pull/100) | Test | 999 | desc |"
    )
    rows = css.parse_registry(text)
    assert "042" in rows
    assert rows["042"]["deps"] == ["999"]


def test_implementation_passes_when_dependency_unlisted() -> None:
    """implementation() passes for an in_review row with the current PR whose only dependency is unlisted."""
    text = make_registry(
        "| 042 | in_review | [#100](https://example.com/pull/100) | Test | 999 | desc |"
    )
    rows = css.parse_registry(text)
    result = css.implementation(rows, 100, "042")
    assert result == "spec-status: PR #100 -> implementation 042: OK"


def test_implementation_fails_when_listed_dependency_is_ready() -> None:
    """implementation() still fails when a listed dependency is ready."""
    text = make_registry(
        "| 001 | ready | — | Base spec | — | base desc |",
        "| 042 | in_review | [#100](https://example.com/pull/100) | Test | 001 | desc |",
    )
    rows = css.parse_registry(text)
    with pytest.raises(css.GateError, match="unmerged dependencies: 001=ready"):
        css.implementation(rows, 100, "042")


def test_self_dependency_fails() -> None:
    """Self-dependency still fails in parse_registry."""
    text = make_registry(
        "| 042 | in_review | [#100](https://example.com/pull/100) | Test | 042 | desc |"
    )
    with pytest.raises(css.GateError, match="spec 042: self-dependency"):
        css.parse_registry(text)


def test_implementation_passes_when_row_is_merged() -> None:
    """implementation() passes for a merged row carrying the current PR."""
    text = make_registry(
        "| 042 | merged | [#100](https://example.com/pull/100) | Test | — | desc |"
    )
    rows = css.parse_registry(text)
    result = css.implementation(rows, 100, "042")
    assert result == "spec-status: PR #100 -> implementation 042: OK"


def test_implementation_fails_when_row_is_ready() -> None:
    """implementation() fails when the row is ready."""
    text = make_registry(
        "| 042 | ready | — | Test | — | desc |"
    )
    rows = css.parse_registry(text)
    with pytest.raises(css.GateError, match=r"spec 042: expected in_review or merged, found ready"):
        css.implementation(rows, 100, "042")
