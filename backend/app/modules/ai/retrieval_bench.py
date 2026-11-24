"""Headless retrieval benchmark evaluator for the engineering bench."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import nullcontext
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from app.core.database import open_sqlite_connection
from app.modules.ai.document_intake import (
    MAX_DIRECTORIES,
    MAX_FILES,
    MAX_PASSAGES,
    _read_file,
    extract_passages,
)
from app.modules.ai.jarvis_context_models import SourceRef
from app.modules.ai.retrieval_contracts import IndexDocument, RetrievalHit
from app.modules.ai.retrieval_index import (
    Embedder,
    FastEmbedEmbedder,
    FastEmbedReranker,
    HashingEmbedder,
    SQLiteIndexStore,
    default_embedder,
    default_reranker,
)
from app.modules.engineering.evaluator_contracts import (
    EvaluationFailure,
    EvaluationResult,
    EvaluatorDescriptor,
    NamedQuantity,
    ResultCheck,
    Verdict,
    result_verdict,
)
from app.modules.engineering.refs import EvaluationRequestRef, EvaluationResultRef, Quantity


def retrieval_eval_descriptor() -> EvaluatorDescriptor:
    return EvaluatorDescriptor(
        evaluator_id="retrieval.eval",
        backend_kind="specialist",
        backend_name="retrieval",
        backend_version="1",
        fidelity="screening",
        required_checks=(
            "RETRIEVAL_CORPUS_NONEMPTY",
            "RETRIEVAL_QUESTIONS_NONEMPTY",
            "RETRIEVAL_EVAL_COMPLETE",
        ),
        advisory_checks=("RETRIEVAL_EXPECTED_IN_TOP3",),
    )


def _digest(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _path_within_root(value: object, root: Path, label: str, *, directory: bool) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a path string")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} is outside root") from exc
    if directory and not resolved.is_dir():
        raise ValueError(f"{label} must be an existing directory")
    if not directory and not resolved.is_file():
        raise ValueError(f"{label} must be an existing file")
    return resolved


def _inline_documents(raw: object) -> list[IndexDocument]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("documents must be a list")
    documents: list[IndexDocument] = []
    if len(raw) > MAX_PASSAGES:
        raise ValueError("document limit exceeded")
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("each document requires a non-empty id")
        text = item.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("each document requires non-empty text")
        owner = item.get("source_owner", "retrieval_bench")
        source_id = item.get("source_id", item["id"])
        workspace_id = item.get("workspace_id")
        if not isinstance(owner, str) or not isinstance(source_id, str):
            raise ValueError("document source fields must be strings")
        if workspace_id is not None and not isinstance(workspace_id, str):
            raise ValueError("document workspace_id must be a string")
        ref = SourceRef(
            authority_owner=owner,
            object_type="document",
            object_id=source_id,
            workspace_id=workspace_id,
            content_digest=_digest(text),
        )
        title = item.get("title")
        if title is not None and not isinstance(title, str):
            raise ValueError("document title must be a string")
        documents.append(IndexDocument(source_ref=ref, title=title, text=text))
    return documents


def _corpus_documents(corpus_dir: Path) -> list[IndexDocument]:
    documents: list[IndexDocument] = []
    candidates: list[Path] = []
    for count, (directory, dirs, files) in enumerate(os.walk(corpus_dir, followlinks=False), 1):
        if count > MAX_DIRECTORIES:
            raise ValueError("corpus directory limit exceeded")
        if any((Path(directory) / name).is_symlink() for name in dirs):
            raise ValueError("corpus symlinks are not allowed")
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError("corpus symlinks are not allowed")
            if path.suffix.lower() in {".md", ".txt"}:
                candidates.append(path.relative_to(corpus_dir))
                if len(candidates) > MAX_FILES:
                    raise ValueError("corpus file limit exceeded")
    for relative_path in sorted(candidates):
        relative = relative_path.as_posix()
        raw = _read_file(corpus_dir, relative_path)
        if raw is None:
            raise ValueError("corpus file is unreadable, too large, or a symlink")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("corpus file must be UTF-8") from exc
        for heading, passage, start, _ in extract_passages(text, markdown=relative_path.suffix.lower() == ".md"):
            if len(documents) >= MAX_PASSAGES:
                raise ValueError("corpus passage limit exceeded")
            end = start + len(passage.splitlines()) - 1
            source_id = f"{relative}#L{start}-{end}"
            title = f"{relative} — {heading}" if heading else relative
            documents.append(
                IndexDocument(
                    source_ref=SourceRef(
                        authority_owner="workspace_documents",
                        object_type="passage",
                        object_id=source_id,
                        content_digest=_digest(passage),
                    ),
                    title=title,
                    text=passage,
                )
            )
    return documents


def _matches(expected: str, hit: RetrievalHit, title: str | None) -> bool:
    source_id = hit.source_ref.object_id
    values = {source_id, title or "", f"{hit.source_ref.authority_owner}:{source_id}"}
    if "#L" in source_id:
        values.add(source_id.split("#L", 1)[0])
    # Document passages: also the file path, fragment and heading the Memory search screen shows.
    if hit.source_ref.authority_owner == "workspace_documents":
        values.add(source_id.split("::", 1)[0])
        fragment = hit.source_ref.location.fragment if hit.source_ref.location else None
        if fragment:
            values.add(fragment)
            if "#" in fragment:
                values.add(fragment.split("#", 1)[1])
    return expected in values


def _output(name: str, value: float) -> NamedQuantity:
    return NamedQuantity(name=name, value=Quantity(value=value, unit="1"))


def evaluate_retrieval_request(
    request_data: dict[str, Any], *, root: Path, out_dir: Path
) -> tuple[EvaluationResult, Verdict, dict[str, Any]]:
    del out_dir
    root = root.resolve()
    if not isinstance(request_data, dict):
        raise ValueError("request must be an object")
    if len(json.dumps(request_data, ensure_ascii=True, default=str)) > 2_000_000:
        raise ValueError("request size limit exceeded")
    mode = request_data.get("mode", "hybrid")
    if mode not in {"lexical", "vector", "hybrid"}:
        raise ValueError("mode must be lexical, vector, or hybrid")
    limit = request_data.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 10 <= limit <= 1000:
        raise ValueError("limit must be an integer between 10 and 1000")
    default_ws = request_data.get("workspace_id")
    if default_ws is not None and not isinstance(default_ws, str):
        raise ValueError("workspace_id must be a string")

    corpus_dir = None
    if request_data.get("corpus_dir") is not None:
        corpus_dir = _path_within_root(request_data["corpus_dir"], root, "corpus_dir", directory=True)
    index_path = None
    if request_data.get("index_db_path") is not None:
        index_path = _path_within_root(request_data["index_db_path"], root, "index_db_path", directory=False)
    questions = request_data.get("questions", [])
    if not isinstance(questions, list):
        raise ValueError("questions must be a list")

    if len(questions) > 1000:
        raise ValueError("question limit exceeded")
    inline = _inline_documents(request_data.get("documents"))
    corpus = _corpus_documents(corpus_dir) if corpus_dir is not None else []
    documents = [*inline, *corpus]
    errors: list[dict[str, str]] = []
    per_question: list[dict[str, Any]] = []

    embedder_choice = request_data.get("embedder")
    chosen_embedder: Embedder | None = None
    if isinstance(embedder_choice, str):
        if embedder_choice not in {"hashing", "fastembed", "auto", "none"}:
            raise ValueError("embedder must be hashing, fastembed, auto, or none")
        if embedder_choice == "hashing":
            chosen_embedder = HashingEmbedder()
        elif embedder_choice == "fastembed":
            chosen_embedder = FastEmbedEmbedder()
        elif embedder_choice == "auto":
            chosen_embedder = default_embedder()
        elif embedder_choice == "none":
            if mode != "lexical":
                raise ValueError("embedder 'none' is only allowed in lexical mode")
            chosen_embedder = HashingEmbedder()
    elif embedder_choice is not None:
        if not hasattr(embedder_choice, "embed"):
            raise ValueError("embedder must be hashing, fastembed, auto, or none")
        chosen_embedder = embedder_choice

    _RERANKER_UNSET = object()
    chosen_reranker: Any = _RERANKER_UNSET
    reranker_choice = request_data.get("reranker")
    if isinstance(reranker_choice, str):
        if reranker_choice not in {"none", "fastembed", "auto"}:
            raise ValueError("reranker must be none, fastembed, or auto")
        if reranker_choice == "none":
            chosen_reranker = None
        elif reranker_choice == "fastembed":
            chosen_reranker = FastEmbedReranker()
        elif reranker_choice == "auto":
            chosen_reranker = default_reranker()
    elif reranker_choice is not None:
        if not hasattr(reranker_choice, "score"):
            raise ValueError("reranker must be none, fastembed, or auto")
        chosen_reranker = reranker_choice

    store_kwargs: dict[str, Any] = {}
    if chosen_embedder is not None:
        store_kwargs["embedder"] = chosen_embedder
    if chosen_reranker is not _RERANKER_UNSET:
        store_kwargs["reranker"] = chosen_reranker

    with TemporaryDirectory(dir=root) if index_path is None or documents else nullcontext() as temp_dir:
        if index_path is not None:
            source = SQLiteIndexStore(db_path=index_path, read_only=True, **store_kwargs)
            if (mode != "lexical" or documents) and not source.has_compatible_embedder():
                raise ValueError("index embedder identity is missing or incompatible")
        if index_path is None or documents:
            assert temp_dir is not None
            db_path = Path(temp_dir) / "index.sqlite3"
            if index_path is not None:
                with source._open_connection() as src, open_sqlite_connection(db_path) as dst:
                    src.backup(dst)
            store = SQLiteIndexStore(db_path=db_path, documents=lambda: (), **store_kwargs)
            store.upsert(documents)
        else:
            store = source
        document_count = store.document_count()
        embedder_identity = store._embedder_identity()
        reranker_identity = getattr(store.reranker, "identity", None) or "none"

        @lru_cache(maxsize=1024)
        def title_for(ref_json: str) -> str | None:
            return store.document_title(SourceRef.model_validate_json(ref_json))

        for question in questions:
            try:
                if not isinstance(question, dict) or not isinstance(question.get("query"), str):
                    raise ValueError("question requires query")
                expected = question.get("expected_ids", [])
                if not isinstance(expected, list) or not expected or any(
                    not isinstance(item, str) or not item for item in expected
                ):
                    raise ValueError("question expected_ids must be non-empty strings")
                accepted = question.get("accepted_ids", [])
                if not isinstance(accepted, list) or any(not isinstance(item, str) or not item for item in accepted):
                    raise ValueError("question accepted_ids must be non-empty strings")
                q_ws = question.get("workspace_id", default_ws)
                if q_ws is not None and not isinstance(q_ws, str):
                    raise ValueError("question workspace_id must be a string")
                search = getattr(store, f"search_{mode}")
                hits = search(question["query"], limit=limit, workspace_id=q_ws)
                rank, via_accepted = next(
                    ((position, not any(_matches(item, hit, title) for item in expected))
                     for position, hit in enumerate(hits, 1)
                     for title in (title_for(hit.source_ref.model_dump_json()),)
                     if any(_matches(item, hit, title) for item in (*expected, *accepted))),
                    (None, False),
                )
                per_question.append({
                    "id": question.get("id"),
                    "query": question["query"],
                    "expected_ids": expected,
                    "accepted_ids": accepted,
                    "first_matching_rank": rank,
                    "matched_accepted": via_accepted,
                    "hit_count": len(hits),
                })
            except Exception as exc:
                question_id = str(question.get("id", "")) if isinstance(question, dict) else ""
                errors.append({"id": question_id, "error": str(exc)})

    question_count = len(questions)
    ranks = [item["first_matching_rank"] for item in per_question]
    top1 = sum(rank is not None and rank <= 1 for rank in ranks)
    top3 = sum(rank is not None and rank <= 3 for rank in ranks)
    top10 = sum(rank is not None and rank <= 10 for rank in ranks)
    mrr = sum(1.0 / rank for rank in ranks if rank is not None)
    denominator = question_count or 1
    zero_hit = sum(item["hit_count"] == 0 for item in per_question)
    answer_measured = document_count > 0 and question_count > 0 and not errors
    checks = (
        ResultCheck(
            id="RETRIEVAL_CORPUS_NONEMPTY", family="input",
            status="pass" if document_count > 0 else "fail",
            actual=float(document_count), limit=1.0, unit="1",
            hint="The retrieval corpus contains indexed documents.", source="constant:retrieval_bench",
        ),
        ResultCheck(
            id="RETRIEVAL_QUESTIONS_NONEMPTY", family="input",
            status="pass" if question_count > 0 else "fail",
            actual=float(question_count), limit=1.0, unit="1",
            hint="The request contains evaluation questions.", source="constant:retrieval_bench",
        ),
        ResultCheck(
            id="RETRIEVAL_EVAL_COMPLETE", family="numerical",
            status="fail" if errors else "pass",
            actual=float(len(errors)), limit=0.0, unit="1",
            hint="Every retrieval question completed without an evaluation error." if not errors
            else "One or more retrieval questions raised an evaluation error.",
            source="constant:retrieval_bench",
        ),
        ResultCheck(
            id="RETRIEVAL_EXPECTED_IN_TOP3", family="reference",
            status="not_evaluated" if not answer_measured else "pass" if top3 == question_count else "warn",
            actual=float(top3) / question_count if answer_measured else None,
            limit=1.0 if answer_measured else None, unit="1" if answer_measured else None,
            hint="Not evaluated: the other retrieval checks failed." if not answer_measured
            else "Every retrieval question found its expected answer within the top three."
            if top3 == question_count
            else f"{question_count - top3} retrieval questions missed an expected answer in the top three.",
            source="constant:retrieval_bench",
        ),
    )
    descriptor = retrieval_eval_descriptor()
    request_ref = EvaluationRequestRef(
        authority_owner="bench", object_id="retrieval-eval-request",
        workspace_id=default_ws or "retrieval-bench", revision="1",
    )
    result = EvaluationResult(
        result_ref=EvaluationResultRef(
            authority_owner="bench", object_id="retrieval-eval-result",
            workspace_id=request_ref.workspace_id, revision="1",
        ),
        request_ref=request_ref,
        evaluator_id=descriptor.evaluator_id,
        backend_version=descriptor.backend_version,
        status="failed" if errors else "succeeded",
        failure=EvaluationFailure(
            category="internal_error", backend_code="RETRIEVAL_EVAL_ERROR",
            message="One or more retrieval questions failed.",
        ) if errors else None,
        fidelity=descriptor.fidelity,
        outputs=tuple(
            _output(
                name,
                value / denominator
                if name in {"top1_hit_rate", "top3_hit_rate", "top10_hit_rate"}
                else value,
            )
            for name, value in (
                ("top1_hit_rate", float(top1)),
                ("top3_hit_rate", float(top3)),
                ("top10_hit_rate", float(top10)),
                ("mrr", mrr / denominator),
                ("question_count", float(question_count)),
                ("zero_hit_count", float(zero_hit)),
            )
        ) if not errors else (),
        checks=checks,
        completed_at=datetime.now(UTC),
    )
    return (
        result,
        result_verdict(descriptor, result),
        {
            "questions": per_question,
            "errors": errors,
            "embedder_identity": embedder_identity,
            "reranker_identity": reranker_identity,
        },
    )
