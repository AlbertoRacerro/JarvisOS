"""Owner mediated canonical projections and transactional retrieval synchronization."""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from typing import TYPE_CHECKING

from app.core.database import open_retrieval_index_connection, open_sqlite_connection
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.jarvis_context_models import SourceRef
from app.modules.ai.retrieval_contracts import IndexDocument
from app.modules.workspaces.service import list_workspaces

if TYPE_CHECKING:
    from app.modules.ai.retrieval_index import SQLiteIndexStore

_MAX_DOCUMENT_CHARS = 32_000


def _doc(owner: str, kind: str, object_id: str, workspace_id: str,
         record: object, *, revision: str | None = None, title: str | None = None) -> IndexDocument:
    data = record.model_dump(mode="json") if hasattr(record, "model_dump") else record
    text = data if isinstance(data, str) else json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    digest = canonical_digest(data)
    if len(text) > _MAX_DOCUMENT_CHARS:
        text = text[:_MAX_DOCUMENT_CHARS] + f"\n[TRUNCATED: full owner record digest sha256:{digest}]"
    ref = SourceRef(authority_owner=owner, object_type=kind, object_id=object_id,
                    workspace_id=workspace_id, revision=revision, content_digest=digest)
    return IndexDocument(source_ref=ref, title=title, text=text)


def _records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def canonical_owner_documents(owners: set[str] | None = None) -> Iterable[IndexDocument]:
    """Project the accepted owner records named by Second Brain capabilities 3 and 4."""
    from app.modules.ai.retrieval_index import _evidence_document, _model_document, _record_document
    from app.modules.ai.thread_service import get_thread, list_threads
    from app.modules.bluecad.evidence import get_evidence_record
    from app.modules.bluecad.ledger import list_attempts, list_candidates
    from app.modules.development.service import list_calendar_allocations, list_roadmap_items
    from app.modules.memory.literature_service import list_literature_sources
    from app.modules.modeling.model_dossier import get_model_dossier, list_model_dossier_index
    from app.modules.modeling.service import (
        list_assumptions,
        list_decisions,
        list_model_specs,
        list_parameters,
        list_requirements,
        list_simulation_runs,
    )
    from app.modules.project_knowledge.service import get_snapshot, list_revisions

    def selected(owner: str) -> bool:
        return owners is None or owner in owners

    for workspace in sorted(list_workspaces(include_archived=True), key=lambda item: item.id):
        wid = workspace.id
        if selected("modeling"):
            for kind, records in (
                ("model_spec", list_model_specs(wid)), ("decision", list_decisions(wid)),
                ("assumption", list_assumptions(wid)), ("parameter", list_parameters(wid)),
                ("requirement", list_requirements(wid)),
            ):
                for record in records:
                    yield _model_document(kind, record, wid)
            for dossier_entry in list_model_dossier_index(wid):
                for version in dossier_entry.versions:
                    dossier = get_model_dossier(wid, version.model_version_id)
                    if dossier is not None:
                        yield _record_document("modeling", "dossier", dossier, wid,
                                               object_id=version.model_version_id)
        if selected("bluecad"):
            for candidate in list_candidates(wid):
                yield _record_document("bluecad", "candidate", candidate, wid)
                for attempt in list_attempts(candidate.id):
                    yield _record_document("bluecad", "attempt", attempt, wid)
            with open_sqlite_connection() as connection:
                evidence_ids = [str(row[0]) for row in connection.execute(
                    "SELECT id FROM evidence_records WHERE workspace_id=? ORDER BY id", (wid,))]
            for evidence_id in evidence_ids:
                evidence_record = get_evidence_record(evidence_id)
                if evidence_record is not None and evidence_record.workspace_id == wid:
                    yield _evidence_document(evidence_record, wid)
        thread_offset = 0
        while selected("ai_threads"):
            threads = list_threads(workspace_id=wid, limit=50, offset=thread_offset).threads
            for thread in threads:
                yield _doc("ai_threads", "thread", thread.id, wid, thread,
                           revision=thread.last_activity_at)
                interaction_offset = 0
                while True:
                    detail = get_thread(workspace_id=wid, thread_id=thread.id,
                                        interaction_limit=100, interaction_offset=interaction_offset)
                    for interaction in detail.interactions:
                        yield _doc("ai_threads", "interaction", interaction.id, wid, interaction,
                                   revision=interaction.updated_at, title=thread.id)
                    if len(detail.interactions) < 100:
                        break
                    interaction_offset += len(detail.interactions)
            if len(threads) < 50:
                break
            thread_offset += len(threads)
        for page_offset in (range(0, 1_000_000, 50) if selected("literature") else []):
            page = list_literature_sources(wid, offset=page_offset, limit=50)
            for source in page.items:
                yield _record_document("literature", "source", source, wid)
                for entry in source.entries:
                    yield _doc("literature", "entry", entry.id, wid, entry,
                               revision=entry.updated_at, title=source.title)
            if page.next_offset is None:
                break
        for working_revision in (list_revisions(wid) if selected("project_knowledge") else []):
            yield _doc("project_knowledge", "revision", working_revision.id, wid, working_revision,
                       revision=working_revision.projected_state_digest)
        with open_sqlite_connection() as connection:
            snapshot_ids = [str(row[0]) for row in connection.execute(
                "SELECT id FROM project_knowledge_reconciled_snapshots WHERE workspace_id=?", (wid,)
            )] if selected("project_knowledge") else []
        for snapshot_id in snapshot_ids:
            snapshot = get_snapshot(wid, snapshot_id)
            data = snapshot.model_dump(mode="json")
            full_text = json.dumps(data, sort_keys=True, ensure_ascii=False)
            digest = canonical_digest(data)
            ref = SourceRef(authority_owner="project_knowledge", object_type="snapshot",
                            object_id=snapshot_id, workspace_id=wid, revision=snapshot_id,
                            content_digest=digest)
            text = (full_text if len(full_text) <= 200_000 else
                    full_text[:_MAX_DOCUMENT_CHARS] + f"\n[TRUNCATED: full owner record digest sha256:{digest}]")
            yield IndexDocument(source_ref=ref, text=text)
        for roadmap_item in (list_roadmap_items(wid) if selected("development") else []):
            yield _doc("development", "roadmap_item", str(roadmap_item["id"]), wid, roadmap_item,
                       revision=str(roadmap_item.get("revision", roadmap_item.get("updated_at", ""))))
        for allocation in (list_calendar_allocations(wid) if selected("development") else []):
            yield _doc("development", "calendar_allocation", str(allocation["id"]), wid, allocation,
                       revision=str(allocation.get("revision", allocation.get("updated_at", ""))))
        for run in (list_simulation_runs(wid) if selected("modeling") else []):
            yield _doc("modeling", "simulation_run", run.id, wid, run,
                       revision=run.created_at)
        if selected("engineering") or selected("process_stack"):
            yield from _filesystem_documents(wid, owners=owners)


def _filesystem_documents(workspace_id: str, *, owners: set[str] | None = None) -> Iterable[IndexDocument]:
    from app.core.paths import build_paths
    from app.modules.engineering.operator_service import read_record, record_paths
    from app.modules.engineering.studies import StudyRun
    from app.modules.process_stack.editor import list_cases, list_revisions

    root = build_paths().workspaces_dir / workspace_id / "engineering" / "studies"
    if (owners is None or "engineering" in owners) and root.is_dir():
        for study_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            for path in record_paths(study_dir / "runs", ""):
                run = read_record(path, StudyRun)
                if run is not None:
                    yield _doc("engineering", "study_run", f"{study_dir.name}:{run.content_digest}",
                                workspace_id, run, revision=run.content_digest)
    if owners is not None and "process_stack" not in owners:
        return
    try:
        cases = list_cases(workspace_id)
    except (OSError, ValueError):
        return
    for case in cases:
        try:
            revisions = list_revisions(workspace_id, case.case_id)
        except (OSError, ValueError):
            continue
        for rev in revisions:
            # Index only owner metadata. The .dwxmz payload remains outside retrieval storage.
            data = {"case_id": case.case_id, "revision": rev.revision, "seq": rev.seq,
                    "case_sha256": rev.case_sha256, "command_kind": rev.command_kind,
                    "created_at": rev.created_at, "readback": rev.readback,
                    "projection_summary": rev.readback}
            yield _doc("process_stack", "case_revision", f"{case.case_id}:{rev.seq}",
                        workspace_id, data, revision=rev.revision)


def resolve_owner_document(ref: SourceRef) -> IndexDocument | None:
    """Reread new owner kinds by exact identity and workspace."""
    wid = ref.workspace_id
    if wid is None:
        return None
    if ref.authority_owner == "project_knowledge" and ref.object_type == "snapshot":
        from app.modules.project_knowledge.service import get_snapshot

        try:
            snapshot = get_snapshot(wid, ref.object_id)
        except ValueError:
            return None
        data = snapshot.model_dump(mode="json")
        full_text = json.dumps(data, sort_keys=True, ensure_ascii=False)
        digest = canonical_digest(data)
        text = full_text if len(full_text) <= 200_000 else (
            full_text[:_MAX_DOCUMENT_CHARS] + f"\n[TRUNCATED: full owner record digest sha256:{digest}]"
        )
        return IndexDocument(source_ref=SourceRef(
            authority_owner="project_knowledge", object_type="snapshot", object_id=ref.object_id,
            workspace_id=wid, revision=ref.object_id, content_digest=digest,
        ), text=text)
    if ref.authority_owner == "literature" and ref.object_type == "entry":
        from app.modules.memory.literature_service import get_literature_entry

        try:
            entry = get_literature_entry(wid, ref.object_id)
        except ValueError:
            return None
        return _doc("literature", "entry", entry.id, wid, entry,
                    revision=entry.updated_at, title=entry.provenance_ref)
    if ref.authority_owner == "project_knowledge" and ref.object_type == "revision":
        from app.modules.project_knowledge.service import get_revision

        try:
            revision_record = get_revision(wid, ref.object_id)
        except ValueError:
            return None
        return _doc("project_knowledge", "revision", revision_record.id, wid, revision_record,
                    revision=revision_record.projected_state_digest)
    if ref.authority_owner == "development":
        from app.modules.development.service import (
            DevelopmentError,
            get_calendar_allocation,
            get_roadmap_item,
        )

        try:
            if ref.object_type == "roadmap_item":
                record = get_roadmap_item(wid, ref.object_id)
                return _doc("development", "roadmap_item", ref.object_id, wid, record,
                            revision=str(record.get("revision", record.get("updated_at", ""))))
            if ref.object_type == "calendar_allocation":
                record = get_calendar_allocation(wid, ref.object_id)
                return _doc("development", "calendar_allocation", ref.object_id, wid, record,
                            revision=str(record.get("revision", record.get("updated_at", ""))))
        except DevelopmentError:
            return None
    wanted = (ref.authority_owner, ref.object_type)
    if ref.authority_owner == "ai_threads":
        from app.modules.ai.thread_service import AIThreadError, get_interaction, list_threads

        try:
            if ref.object_type == "thread":
                offset = 0
                while True:
                    rows = list_threads(workspace_id=wid, limit=50, offset=offset).threads
                    thread_record = next((item for item in rows if item.id == ref.object_id), None)
                    if thread_record is not None:
                        return _doc("ai_threads", "thread", ref.object_id, wid, thread_record,
                                    revision=thread_record.last_activity_at)
                    if len(rows) < 50:
                        break
                    offset += len(rows)
            if ref.object_type == "interaction":
                interaction_record = get_interaction(workspace_id=wid, interaction_id=ref.object_id)
                return _doc("ai_threads", "interaction", ref.object_id, wid, interaction_record,
                            revision=interaction_record.updated_at)
        except (AIThreadError, ValueError):
            return None
    for document in canonical_owner_documents({ref.authority_owner}):
        current = document.source_ref
        if (current.workspace_id == wid and (current.authority_owner, current.object_type) == wanted
                and current.object_id == ref.object_id):
            return document
    return None


def _manifest_key(ref: SourceRef) -> str:
    location = ref.location.model_dump(mode="json") if ref.location else None
    return canonical_digest((ref.authority_owner, ref.object_type, ref.object_id, ref.workspace_id, location))


def _owner_fingerprints() -> dict[str, str]:
    """Cheap mutation hints from canonical table watermarks and filesystem listings."""
    tables = {
        "literature": ("literature_sources", "literature_entries"),
        "project_knowledge": ("project_knowledge_revisions", "project_knowledge_reconciled_snapshots"),
        "development": ("roadmap_items", "calendar_allocations"),
        "ai_threads": ("ai_threads", "ai_thread_interactions"),
        "modeling": ("model_specs", "decisions", "assumptions", "parameters", "requirements",
                     "model_versions", "simulation_runs"),
        "bluecad": ("bluecad_candidates", "bluecad_attempts", "evidence_records"),
    }
    result: dict[str, str] = {}
    with open_sqlite_connection() as connection:
        for owner, names in tables.items():
            values: list[object] = []
            for table in names:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if exists is None:
                    values.append((table, 0, None, None))
                    continue
                rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                values.append((table, [tuple(bytes(value).hex() if isinstance(value, bytes) else value
                                               for value in row) for row in rows]))
            result[owner] = canonical_digest(values)

    from app.core.paths import build_paths
    root = build_paths().workspaces_dir
    for owner, relative in (("engineering", "engineering/studies"), ("process_stack", "process")):
        files = []
        if root.is_dir():
            for workspace in sorted(path for path in root.iterdir() if path.is_dir()):
                directory = workspace / relative
                if directory.is_dir():
                    for path in sorted(directory.rglob("*")):
                        if path.is_file() and path.suffix.lower() != ".dwxmz":
                            stat = path.stat()
                            files.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns))
        result[owner] = canonical_digest(files)
    return result


def owner_catch_up(store: SQLiteIndexStore) -> dict[str, dict[str, int]]:
    """Synchronize canonical-owner projections atomically; unchanged records are not embedded."""
    store._last_owner_changes = []
    fingerprints = _owner_fingerprints()
    with open_sqlite_connection() as canonical_db:
        workspaces_ready = canonical_db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspaces'").fetchone() is not None
    if not workspaces_ready:
        return {owner: {"changed": 0, "deleted": 0, "embedded": 0} for owner in fingerprints}
    with open_retrieval_index_connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS owner_manifest (key TEXT PRIMARY KEY, owner TEXT NOT NULL, object_type TEXT NOT NULL, workspace_id TEXT NOT NULL, content_digest TEXT NOT NULL)")
        previous = {str(row[0]).removeprefix("owner_fingerprint:"): str(row[1]) for row in db.execute(
            "SELECT name,value FROM meta WHERE name LIKE 'owner_fingerprint:%'"
        )}
        if previous and previous == fingerprints:
            return {owner: {"changed": 0, "deleted": 0, "embedded": 0} for owner in fingerprints}
    changed_owners = {owner for owner, fingerprint in fingerprints.items()
                      if previous.get(owner) != fingerprint}
    if not changed_owners:
        return {owner: {"changed": 0, "deleted": 0, "embedded": 0} for owner in fingerprints}
    documents = list(canonical_owner_documents(changed_owners))
    desired = {_manifest_key(doc.source_ref): doc for doc in documents}
    grouped: dict[str, list[tuple[str, IndexDocument]]] = defaultdict(list)
    for key, doc in desired.items():
        grouped[doc.source_ref.authority_owner].append((key, doc))
    result: dict[str, dict[str, int]] = {}
    with open_retrieval_index_connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS owner_manifest (key TEXT PRIMARY KEY, owner TEXT NOT NULL, object_type TEXT NOT NULL, workspace_id TEXT NOT NULL, content_digest TEXT NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        old_rows = db.execute("SELECT key, owner, object_type, workspace_id, content_digest FROM owner_manifest").fetchall()
        previous_digests = {str(row[0]): str(row[4]) for row in old_rows}
        owners = changed_owners
        changed_docs: list[IndexDocument] = []
        deleted_keys: set[str] = set()
        embedded_count: dict[str, int] = defaultdict(int)
        for owner in owners:
            current = {key: doc for key, doc in grouped.get(owner, [])}
            previous = {str(row[0]): str(row[4]) for row in old_rows if str(row[1]) == owner}
            changed = [doc for key, doc in current.items() if previous.get(key) != doc.source_ref.content_digest]
            deleted = set(previous) - set(current)
            changed_docs.extend(changed)
            deleted_keys.update(deleted)
            result[owner] = {"changed": len(changed), "deleted": len(deleted), "embedded": 0}
        stale_refs: list[SourceRef] = []
        changed_manifest_keys = {
            key for owner in changed_owners for key, doc in grouped.get(owner, [])
            if previous_digests.get(key) != doc.source_ref.content_digest
        }
        changed_manifest_keys.update(key for key in deleted_keys)
        desired_digests = {key: doc.source_ref.content_digest for key, doc in desired.items()}
        for key in changed_manifest_keys:
            row = db.execute("SELECT ref FROM docs WHERE key=?", (key,)).fetchone()
            if row is not None:
                old_ref = SourceRef.model_validate_json(row[0])
                if key in deleted_keys or desired_digests.get(key) != old_ref.content_digest:
                    stale_refs.append(old_ref)
        store._last_owner_changes = stale_refs
        # Owner docs and their summaries are maintained in the same SQLite transaction.
        if changed_docs:
            for owner in changed_owners:
                owner_docs = [doc for doc in changed_docs if doc.source_ref.authority_owner == owner]
                if owner_docs:
                    embedded_count[owner] += store._upsert(db, owner_docs)
        for key in deleted_keys:
            store._delete_keys(db, [key])
            summary_rows = db.execute("SELECT key FROM docs WHERE object_id=?", (key,)).fetchall()
            store._delete_keys(db, [str(row[0]) for row in summary_rows])
        affected = {doc.source_ref.authority_owner for doc in changed_docs}
        affected.update(str(row[1]) for row in old_rows if str(row[0]) in deleted_keys)
        for owner in affected:
            current_docs = [doc for doc in desired.values() if doc.source_ref.authority_owner == owner]
            changed_owner_docs = [doc for doc in changed_docs if doc.source_ref.authority_owner == owner]
            summaries = [_document_summary(doc) for doc in changed_owner_docs]
            if summaries:
                embedded_count[owner] += store._upsert(db, summaries)
            groups: dict[str, list[IndexDocument]] = defaultdict(list)
            for doc in current_docs:
                groups[doc.source_ref.workspace_id or ""].append(doc)
            group_summaries = [
                _group_summary(items, owner, workspace_id or None)
                for workspace_id, items in groups.items()
            ]
            existing_groups = []
            for row in db.execute("SELECT key,ref FROM docs"):
                try:
                    ref = SourceRef.model_validate_json(row[1])
                except ValueError:
                    continue
                if ref.authority_owner == "retrieval" and ref.object_type == "group_summary" and ref.object_id == owner:
                    existing_groups.append(str(row[0]))
            live_group_keys = {_key(doc.source_ref) for doc in group_summaries}
            store._delete_keys(db, [key for key in existing_groups if key not in live_group_keys])
            if group_summaries:
                embedded_count[owner] += store._upsert(db, group_summaries)
            summary_by_key = {doc.source_ref.object_id: doc for doc in summaries}
            for doc in current_docs:
                summary = summary_by_key.get(_key(doc.source_ref))
                if summary is not None:
                    db.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?,?)",
                               (_key(summary.source_ref), _key(doc.source_ref), "summary_document", None))
            for group in group_summaries:
                for doc in groups[group.source_ref.workspace_id or ""]:
                    summary = summary_by_key.get(_key(doc.source_ref))
                    if summary is not None:
                        db.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?,?)",
                                   (_key(group.source_ref), _key(summary.source_ref), "group_summary", None))
            result.setdefault(owner, {"changed": 0, "deleted": 0, "embedded": 0})["embedded"] = embedded_count[owner]
            db.execute("DELETE FROM owner_manifest WHERE owner=?", (owner,))
            db.executemany("INSERT INTO owner_manifest VALUES (?,?,?,?,?)", [
                (_manifest_key(doc.source_ref), owner, doc.source_ref.object_type,
                 doc.source_ref.workspace_id or "", doc.source_ref.content_digest)
                for doc in current_docs
            ])
        # Rebuild relationship kinds only when their owner changed.
        if "ai_threads" in changed_owners:
            db.execute("DELETE FROM edges WHERE kind='interaction_thread'")
        owner_identities = {
            (doc.source_ref.authority_owner, doc.source_ref.object_type,
             doc.source_ref.object_id, doc.source_ref.workspace_id): doc
            for doc in documents
        }
        for doc in documents:
            ref = doc.source_ref
            if "ai_threads" in changed_owners and ref.object_type == "interaction":
                target = owner_identities.get(("ai_threads", "thread", doc.title or "", ref.workspace_id))
                if target is not None:
                    db.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?,?)",
                               (_key(ref), _key(target.source_ref), "interaction_thread", ref.revision))
        for owner, fingerprint in fingerprints.items():
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"owner_fingerprint:{owner}", fingerprint))
        store._revision(db)
        db.commit()
    return result


def _document_summary(doc: IndexDocument) -> IndexDocument:
    from app.modules.ai.retrieval_index import _document_summary as summarize
    return summarize(doc)


def _group_summary(docs: list[IndexDocument], owner: str, workspace_id: str | None) -> IndexDocument:
    from app.modules.ai.retrieval_index import _group_summary as summarize
    return summarize(docs, owner, workspace_id)


def _key(ref: SourceRef) -> str:
    from app.modules.ai.retrieval_index import _key as key
    return key(ref)
