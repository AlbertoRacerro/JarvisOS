from __future__ import annotations

from pathlib import Path

import check_ip_denylist as dl
import pytest


def tree(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("print('hello')\nsecret_project = 'Zebra Falls'\n", encoding="utf-8")
    (root / "src" / "zebra_notes.md").write_text("nothing here\n", encoding="utf-8")
    (root / "src" / "blob.bin").write_bytes(b"\0\0zebra falls\0")


def test_matches_are_reported_as_paths_and_lines_only(tmp_path: Path) -> None:
    tree(tmp_path)
    patterns = dl.parse_terms("# comment\n\nzebra\\s+falls\nzebra_notes\n")
    hits = dl.scan(tmp_path, patterns, files=dl.tracked_files(tmp_path))
    assert hits == ["src/a.py:2", "src/zebra_notes.md"]  # name match, content match; the binary file is skipped
    assert all("zebra" not in hit.lower() or hit.endswith("zebra_notes.md") for hit in hits)


def test_clean_tree_has_no_hits(tmp_path: Path) -> None:
    tree(tmp_path)
    assert dl.scan(tmp_path, dl.parse_terms("walrus\n"), files=dl.tracked_files(tmp_path)) == []


def test_invalid_term_is_an_error_that_does_not_echo_the_term() -> None:
    with pytest.raises(ValueError) as caught:
        dl.parse_terms("fine\n(unclosed-secret-term\n")
    assert "line 2" in str(caught.value) and "unclosed-secret-term" not in str(caught.value)


def test_missing_secret_is_skipped_unless_required(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("IP_DENYLIST", raising=False)
    assert dl.main([]) == 0
    assert "skipped" in capsys.readouterr().out
    assert dl.main(["--require"]) == 1
    assert "required" in capsys.readouterr().err


def test_main_fails_on_a_match_without_printing_the_term(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tree(tmp_path)
    monkeypatch.setattr(dl, "ROOT", tmp_path)
    monkeypatch.setenv("IP_DENYLIST", "zebra\\s+falls")
    assert dl.main(["--require"]) == 1
    captured = capsys.readouterr()
    assert "src/a.py:2" in captured.err
    assert "zebra" not in (captured.out + captured.err).lower()
    monkeypatch.setenv("IP_DENYLIST", "walrus")
    assert dl.main([]) == 0


def test_main_rejects_an_invalid_term_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("IP_DENYLIST", "(broken-private-term")
    assert dl.main([]) == 2
    assert "broken-private-term" not in capsys.readouterr().err
