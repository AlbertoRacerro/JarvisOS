from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.database import initialize_database
from app.modules.ai import document_intake
from app.modules.ai.document_intake import (
    MAX_FILE_BYTES,
    MAX_FILES,
    extract_passages,
    main,
    refresh_folder,
    register_folder,
    remove_folder,
    scan_folder,
)
from app.modules.ai.retrieval_index import SQLiteIndexStore
from app.modules.ai.retrieval_query import WORKSPACE_SCOPED_OWNERS, query_context
from app.modules.ai.thread_models import AIThreadSubmit
from app.modules.ai.thread_service import _install_hermes_retrieval_grant
from app.modules.workspaces.models import WorkspaceCreate
from app.modules.workspaces.service import create_workspace


def _workspace(slug: str) -> str:
    return create_workspace(WorkspaceCreate(name=slug, slug=slug, status="active")).id


def test_document_lifecycle_isolation_and_rebuild(tmp_path: Path) -> None:
    initialize_database()
    first, second = _workspace("alpha"), _workspace("beta")
    folder = tmp_path / "documents"
    folder.mkdir()
    (folder / "notes.md").write_text("# Pressure\n\nPressure boundary is 40 kPa.\n\n## Valve\n\nValve opens slowly.\n", encoding="utf-8")
    (folder / "plain.txt").write_text("First paragraph.\n\nSecond paragraph about the impeller.\n", encoding="utf-8")
    register_folder(first, folder)
    report = refresh_folder(first)
    assert report["included"] == 3
    assert report["changed"] == 3
    docs = scan_folder(first).documents
    assert {doc.title for doc in docs} == {"notes.md — Pressure", "notes.md — Valve", "plain.txt — Document"}
    assert all(str(folder) not in str(doc.source_ref) for doc in docs)
    assert docs[0].source_ref.location is not None
    assert "notes.md#Pressure" == docs[0].source_ref.location.fragment
    store = SQLiteIndexStore()
    hit = next(hit for hit in store.search_lexical("Pressure", limit=10, workspace_id=first)
               if hit.source_ref.authority_owner == "workspace_documents")
    assert store.resolve_authoritative(hit.source_ref).state == "current"
    result = query_context("Pressure", workspace_id=first, source_scope=("workspace_documents",), store=store)
    assert any("Pressure boundary" in item["excerpt"] for item in result["evidence"])
    assert all(str(folder) not in str(item) for item in result["evidence"])
    assert not query_context("Pressure", workspace_id=second, source_scope=("workspace_documents",), store=store)["evidence"]
    assert "workspace_documents" in WORKSPACE_SCOPED_OWNERS

    (folder / "notes.md").write_text("# Pressure\n\nPressure boundary is 50 kPa.\n\n## Valve\n\nValve opens slowly.\n", encoding="utf-8")
    assert store.resolve_authoritative(hit.source_ref).state == "stale"
    report = refresh_folder(first)
    assert report["changed"] == 1
    updated = next(doc for doc in scan_folder(first).documents if doc.title == "notes.md — Pressure")
    assert updated.source_ref.object_id == hit.source_ref.object_id
    assert updated.source_ref.content_digest != hit.source_ref.content_digest
    before = [(doc.source_ref.object_id, doc.text) for doc in scan_folder(first).documents]
    store.rebuild()
    assert [(doc.source_ref.object_id, doc.text) for doc in scan_folder(first).documents] == before
    assert {hit.source_ref.object_id for hit in store.search_lexical("Pressure", limit=10, workspace_id=first)
            if hit.source_ref.authority_owner == "workspace_documents"} == {updated.source_ref.object_id}
    assert query_context("50 kPa", workspace_id=first, source_scope=("workspace_documents",), store=store)["evidence"]

    (folder / "notes.md").unlink()
    assert store.resolve_authoritative(updated.source_ref).state == "unavailable"
    assert refresh_folder(first)["deleted"] == 2
    remove_folder(first)
    assert refresh_folder(first)["deleted"] == 1
    assert not query_context("impeller", workspace_id=first, source_scope=("workspace_documents",), store=store)["evidence"]


def test_bounds_symlinks_and_invalid_files(tmp_path: Path) -> None:
    initialize_database()
    workspace = _workspace("bounds")
    folder = tmp_path / "documents"
    folder.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("External secret marker", encoding="utf-8")
    (folder / "link.md").symlink_to(outside)
    (folder / "linked_dir").symlink_to(tmp_path, target_is_directory=True)
    (folder / "bad.txt").write_bytes(b"\xff")
    (folder / "binary.txt").write_bytes(b"hello\x00world")
    (folder / "huge.md").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    (folder / "valid.md").write_text("# Safe\n\nLocal text", encoding="utf-8")
    (folder / "long.txt").write_text("x" * 8_001, encoding="utf-8")
    with pytest.raises(ValueError):
        register_folder(workspace, folder / "linked_dir")
    register_folder(workspace, folder)
    result = scan_folder(workspace)
    assert [doc.text for doc in result.documents] == ["Local text"]
    reasons = {item["reason"] for item in result.skipped}
    assert {"symlink_directory", "symlink_or_special", "invalid_utf8", "binary", "file_too_large",
            "passage_too_large"} <= reasons
    assert "External secret marker" not in str(result)
    for index in range(MAX_FILES):
        (folder / f"extra-{index:03}.txt").write_text("text", encoding="utf-8")
    assert any(item["reason"].startswith("file_limit:") for item in scan_folder(workspace).skipped)


def test_heading_and_paragraph_split() -> None:
    assert [(title, body) for title, body, _, _ in extract_passages(
        "# One\n\nFirst.\n\n# Two\n\nSecond.", markdown=True)] == [("One", "First."), ("Two", "Second.")]
    assert extract_passages("A\n\nB", markdown=False)[0][1] == "A\n\nB"


def test_long_blank_line_block_is_split_into_bounded_passages(tmp_path: Path) -> None:
    lines = [f"line {index:03} " + "x" * 52 for index in range(1, 301)]
    text = "\n".join(lines)
    passages = extract_passages(text, markdown=False)

    assert passages
    assert all(len(passage) <= document_intake.MAX_PASSAGE_CHARS for _, passage, _, _ in passages)
    assert [line for _, passage, _, _ in passages for line in passage.splitlines()] == lines
    assert [start for _, _, start, _ in passages] == [1, 130, 259]
    source = tmp_path / "paper.txt"
    source.write_text(text, encoding="utf-8")
    result = document_intake._file_documents(tmp_path, Path("paper.txt"), "workspace")
    assert not result.skipped


def test_fitting_blocks_keep_existing_passage_shape_and_line_numbers() -> None:
    sample = "# Heading\n\nFirst paragraph.\n\nSecond paragraph.\n\n## Subheading\n\nThird paragraph."

    assert extract_passages(sample, markdown=True) == [
        ("Heading", "First paragraph.\n\nSecond paragraph.", 3, (0, 0)),
        ("Subheading", "Third paragraph.", 7, (0, 0)),
    ]


def test_passage_identity_survives_earlier_heading_and_content_edit(tmp_path: Path) -> None:
    initialize_database()
    workspace = _workspace("stable-headings")
    folder = tmp_path / "documents"
    folder.mkdir()
    source = folder / "notes.md"
    source.write_text("# Target\n\nOriginal body.\n", encoding="utf-8")
    register_folder(workspace, folder)
    original = scan_folder(workspace).documents[0]
    source.write_text("# Earlier\n\nOther body.\n\n# Target\n\nOriginal body.\n", encoding="utf-8")
    shifted = next(doc for doc in scan_folder(workspace).documents if doc.title == "notes.md — Target")
    assert shifted.source_ref.object_id == original.source_ref.object_id
    assert shifted.source_ref.content_digest == original.source_ref.content_digest
    source.write_text("# Earlier\n\nOther body.\n\n# Target\n\nEdited body.\n", encoding="utf-8")
    edited = next(doc for doc in scan_folder(workspace).documents if doc.title == "notes.md — Target")
    assert edited.source_ref.object_id == original.source_ref.object_id
    assert edited.source_ref.content_digest != original.source_ref.content_digest


def test_duplicate_headings_and_chunks_have_deterministic_ids(tmp_path: Path) -> None:
    initialize_database()
    workspace = _workspace("duplicate-headings")
    folder = tmp_path / "documents"
    folder.mkdir()
    source = folder / "notes.md"
    body = "A" * 5_000 + "\n\n" + "B" * 5_000
    source.write_text(f"# Repeated\n\n{body}\n\n# Repeated\n\nLast body.\n", encoding="utf-8")
    register_folder(workspace, folder)
    first = scan_folder(workspace).documents
    assert len(first) == 3
    ids = [doc.source_ref.object_id for doc in first]
    assert len(set(ids)) == 3
    assert ids[0].endswith("::d0::c0")
    assert ids[1].endswith("::d0::c1")
    assert ids[2].endswith("::d1::c0")
    source.write_text(f"# Earlier\n\nOther body.\n\n# Repeated\n\n{body}\n\n"
                      "# Repeated\n\nLast body.\n", encoding="utf-8")
    assert [doc.source_ref.object_id for doc in scan_folder(workspace).documents[1:]] == ids


def test_uppercase_extension_resolves_from_registered_file(tmp_path: Path) -> None:
    initialize_database()
    workspace = _workspace("uppercase-extension")
    folder = tmp_path / "documents"
    folder.mkdir()
    (folder / "NOTES.MD").write_text("# Topic\n\nVisible text", encoding="utf-8")
    register_folder(workspace, folder)
    ref = scan_folder(workspace).documents[0].source_ref
    assert document_intake.resolve_passage(ref).text == "Visible text"


def test_query_metadata_refresh_and_one_file_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_database()
    workspace = _workspace("query-cost")
    folder = tmp_path / "documents"
    folder.mkdir()
    for index in range(6):
        (folder / f"item-{index}.md").write_text(
            f"# Topic {index}\n\nMarker{index} body.\n", encoding="utf-8")
    register_folder(workspace, folder)
    refresh_folder(workspace)
    reads: list[str] = []
    original_read = document_intake._read_file

    def counted_read(root: Path, relative: Path) -> bytes | None:
        reads.append(relative.as_posix())
        return original_read(root, relative)

    monkeypatch.setattr(document_intake, "_read_file", counted_read)
    store = SQLiteIndexStore()
    result = query_context("Marker3", workspace_id=workspace,
                           source_scope=("workspace_documents",), store=store)
    assert result["evidence"]
    assert reads and set(reads) == {"item-3.md"}
    assert len(reads) <= 2
    reads.clear()
    (folder / "item-3.md").write_text("# Topic 3\n\nMarker3 edited.\n", encoding="utf-8")
    result = query_context("Marker3", workspace_id=workspace,
                           source_scope=("workspace_documents",), store=store)
    assert any("edited" in item["excerpt"] for item in result["evidence"])
    assert "item-3.md" in reads
    reads.clear()
    (folder / "item-3.md").unlink()
    assert not query_context("Marker3", workspace_id=workspace,
                             source_scope=("workspace_documents",), store=store)["evidence"]
    assert "item-3.md" not in reads


def test_resolve_refuses_file_replaced_by_symlink_during_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_database()
    workspace = _workspace("race")
    folder = tmp_path / "documents"
    folder.mkdir()
    source = folder / "source.md"
    source.write_text("# Safe\n\nLocal text", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("External secret marker", encoding="utf-8")
    register_folder(workspace, folder)
    ref = scan_folder(workspace).documents[0].source_ref
    original_read = document_intake._read_file

    def replace_then_read(root: Path, relative: Path) -> bytes | None:
        source.unlink()
        source.symlink_to(outside)
        return original_read(root, relative)

    monkeypatch.setattr(document_intake, "_read_file", replace_then_read)
    assert document_intake.resolve_passage(ref) is None


def test_resolve_refuses_parent_directory_replaced_by_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_database()
    workspace = _workspace("parent-race")
    folder = tmp_path / "documents"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    (nested / "source.md").write_text("# Safe\n\nLocal text", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.md").write_text("# Safe\n\nExternal secret marker", encoding="utf-8")
    register_folder(workspace, folder)
    ref = scan_folder(workspace).documents[0].source_ref
    original_read = document_intake._read_file

    def replace_then_read(root: Path, relative: Path) -> bytes | None:
        (nested / "source.md").unlink()
        nested.rmdir()
        nested.symlink_to(outside, target_is_directory=True)
        return original_read(root, relative)

    monkeypatch.setattr(document_intake, "_read_file", replace_then_read)
    assert document_intake.resolve_passage(ref) is None


def test_document_owner_uses_existing_workspace_retrieval_grant() -> None:
    worker = SimpleNamespace(live_grants={})
    instructions = _install_hermes_retrieval_grant(
        worker, AIThreadSubmit(request_id="documents-grant", prompt="Find document", route_class="hermes:agent"),
        "workspace-a", "thread-a")
    grant = next(item for item in worker.live_grants.values() if item.capability_id == "jarvis.retrieval_query")
    assert instructions is not None and "workspace_documents" in instructions
    assert grant.scope.workspace_id == "workspace-a"
    assert grant.scope.jarvis_thread_id == "thread-a"
    assert grant.scope.object_refs == ()


def test_local_command_register_inspect_and_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    initialize_database()
    workspace = _workspace("command")
    folder = tmp_path / "operator-documents"
    folder.mkdir()
    (folder / "readme.md").write_text("# Topic\n\nVisible passage", encoding="utf-8")
    for action, extras in (("register", [str(folder)]), ("inspect", []), ("remove", [])):
        monkeypatch.setattr(sys, "argv", ["document_intake", action, workspace, *extras])
        main()
        output = capsys.readouterr().out
        assert str(folder) not in output
        if action == "inspect":
            assert '"included": 1' in output
        if action == "remove":
            assert '"registered": false' in output
