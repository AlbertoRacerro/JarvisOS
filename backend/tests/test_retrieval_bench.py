from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.modules.ai.retrieval_bench import evaluate_retrieval_request, retrieval_eval_descriptor
from app.modules.engineering.bench import BenchInputError, compare_runs, list_evaluators, run_evaluation


def _request(mode: str = "lexical") -> dict[str, object]:
    return {
        "mode": mode,
        "documents": [
            {"id": "alpha", "title": "Alpha", "text": "alpha retrieval evidence"},
            {"id": "beta", "title": "Beta", "text": "beta retrieval evidence"},
            {"id": "gamma", "title": "Gamma", "text": "gamma retrieval evidence"},
        ],
        "questions": [
            {"id": "q1", "query": "alpha", "expected_ids": ["alpha"]},
            {"id": "q2", "query": "beta", "expected_ids": ["beta"]},
            {"id": "q3", "query": "not present", "expected_ids": ["missing"]},
        ],
    }


def test_list_includes_retrieval_evaluator() -> None:
    row = next(item for item in list_evaluators() if item["id"] == "retrieval.eval")
    assert row["backend_kind"] == "specialist"
    assert row["availability_state"] == "available"
    assert row["request_kind"] == "retrieval_eval"
    assert row["required_checks"] == [
        "RETRIEVAL_CORPUS_NONEMPTY",
        "RETRIEVAL_QUESTIONS_NONEMPTY",
        "RETRIEVAL_EVAL_COMPLETE",
    ]


def test_retrieval_answer_check_descriptor() -> None:
    descriptor = retrieval_eval_descriptor()
    assert descriptor.advisory_checks == ("RETRIEVAL_EXPECTED_IN_TOP3",)
    assert descriptor.required_checks == (
        "RETRIEVAL_CORPUS_NONEMPTY",
        "RETRIEVAL_QUESTIONS_NONEMPTY",
        "RETRIEVAL_EVAL_COMPLETE",
    )


@pytest.mark.parametrize("mode", ["lexical", "hybrid"])
def test_inline_retrieval_metrics(mode: str, tmp_path: Path) -> None:
    result, verdict, payload = evaluate_retrieval_request(_request(mode), root=tmp_path, out_dir=tmp_path)
    values = {item.name: item.value.value for item in result.outputs}
    assert verdict == "usable_with_warnings"
    answer_check = next(check for check in result.checks if check.id == "RETRIEVAL_EXPECTED_IN_TOP3")
    assert answer_check.status == "warn"
    assert values["top1_hit_rate"] == pytest.approx(2 / 3)
    assert values["top3_hit_rate"] == pytest.approx(2 / 3)
    assert values["top10_hit_rate"] == pytest.approx(2 / 3)
    assert values["mrr"] == pytest.approx(2 / 3)
    assert values["zero_hit_count"] == (1 if mode == "lexical" else 0)
    assert len(payload["questions"]) == 3


def test_corpus_directory_is_ingested(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "guide.md").write_text("# Heading\n\nretrieval corpus fact\n", encoding="utf-8")
    result, verdict, _ = evaluate_retrieval_request(
        {
            "corpus_dir": str(corpus),
            "questions": [{"id": "q", "query": "corpus fact", "expected_ids": ["guide.md"]}],
        },
        root=tmp_path,
        out_dir=tmp_path,
    )
    assert verdict == "usable"
    assert result.outputs[0].name == "top1_hit_rate"
    assert result.outputs[0].value.value == 1
    answer_check = next(check for check in result.checks if check.id == "RETRIEVAL_EXPECTED_IN_TOP3")
    assert answer_check.status == "pass"
    assert answer_check.actual == 1.0
    assert answer_check.limit == 1.0


@pytest.mark.parametrize(
    "eval_request",
    [{"questions": []}, {"documents": [], "questions": [{"query": "x", "expected_ids": ["x"]}]}],
)
def test_empty_inputs_are_not_usable(eval_request: dict[str, object], tmp_path: Path) -> None:
    result, verdict, _ = evaluate_retrieval_request(eval_request, root=tmp_path, out_dir=tmp_path)
    assert result.status == "succeeded"
    assert verdict == "not_usable"
    assert any(check.status == "fail" for check in result.checks)
    answer_check = next(check for check in result.checks if check.id == "RETRIEVAL_EXPECTED_IN_TOP3")
    assert answer_check.status == "not_evaluated"
    assert answer_check.actual is None
    assert answer_check.limit is None
    run, view = run_evaluation("retrieval.eval", eval_request, tmp_path / "run", root=tmp_path)
    assert run["verdict"] == "not_usable"
    assert run["outputs"] == []
    assert view["outputs"] == "withheld (verdict not_usable)"


def test_evaluation_error_is_not_evaluated(tmp_path: Path) -> None:
    request = {
        "documents": [{"id": "a", "text": "apple"}],
        "questions": [{"query": 123, "expected_ids": ["a"]}],
    }
    result, verdict, payload = evaluate_retrieval_request(request, root=tmp_path, out_dir=tmp_path)
    assert result.status == "failed"
    assert verdict == "not_usable"
    assert payload["errors"]
    assert result.outputs == ()
    answer_check = next(check for check in result.checks if check.id == "RETRIEVAL_EXPECTED_IN_TOP3")
    assert answer_check.status == "not_evaluated"
    assert answer_check.actual is None
    assert answer_check.limit is None


@pytest.mark.parametrize("field", ["corpus_dir", "index_db_path"])
def test_paths_outside_root_are_rejected(field: str, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"outside-{field}"
    if field == "corpus_dir":
        outside.mkdir()
    else:
        outside.write_bytes(b"")
    with pytest.raises(BenchInputError):
        run_evaluation("retrieval.eval", {field: str(outside)}, tmp_path / "run", root=tmp_path)


def test_compare_retrieval_runs(tmp_path: Path) -> None:
    run_a, _ = run_evaluation("retrieval.eval", _request(), tmp_path / "a", root=tmp_path)
    run_b, _ = run_evaluation("retrieval.eval", {**_request(), "mode": "hybrid"}, tmp_path / "b", root=tmp_path)
    compared = compare_runs(tmp_path / "a", tmp_path / "b", root=tmp_path)
    assert run_a["evaluator_id"] == run_b["evaluator_id"] == "retrieval.eval"
    assert "top1_hit_rate" in compared["outputs"]
    assert json.loads((tmp_path / "a" / "per_question.json").read_text())["questions"]


def test_unrelated_hits_are_not_zero_hits(tmp_path: Path) -> None:
    data = {"mode": "lexical", "documents": [{"id": "a", "text": "apple"}],
            "questions": [{"id": "q", "query": "apple", "expected_ids": ["missing"]}]}
    result, verdict, payload = evaluate_retrieval_request(data, root=tmp_path, out_dir=tmp_path)
    values = {item.name: item.value.value for item in result.outputs}
    assert verdict == "usable_with_warnings"
    answer_check = next(check for check in result.checks if check.id == "RETRIEVAL_EXPECTED_IN_TOP3")
    assert answer_check.status == "warn"
    assert payload["questions"][0]["hit_count"] == 1
    assert values["zero_hit_count"] == 0
    assert values["top10_hit_rate"] == values["mrr"] == 0


def test_index_identity_and_uri_path(tmp_path: Path) -> None:
    from app.modules.ai.retrieval_index import HashingEmbedder, SQLiteIndexStore

    path = tmp_path / "index ?#%.sqlite"
    SQLiteIndexStore(db_path=path, embedder=HashingEmbedder(16), documents=lambda: ()).rebuild()
    data = {"index_db_path": str(path), "questions": []}
    with pytest.raises(BenchInputError, match="embedder identity"):
        run_evaluation("retrieval.eval", data, tmp_path / "bad", root=tmp_path)
    run, _ = run_evaluation("retrieval.eval", {**data, "mode": "lexical"}, tmp_path / "ok", root=tmp_path)
    assert run["verdict"] == "not_usable"
    SQLiteIndexStore(db_path=path, documents=lambda: ()).rebuild()
    run, _ = run_evaluation("retrieval.eval", data, tmp_path / "compatible", root=tmp_path)
    assert run["verdict"] == "not_usable"
    assert not (tmp_path / "index ").exists()


def test_corpus_symlink_and_large_inputs_rejected(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "link.txt").symlink_to(tmp_path.parent / "outside.txt")
    with pytest.raises(BenchInputError, match="symlink"):
        run_evaluation("retrieval.eval", {"corpus_dir": str(corpus)}, tmp_path / "out", root=tmp_path)
    with pytest.raises(BenchInputError, match="limit"):
        run_evaluation("retrieval.eval", {"limit": 1001}, tmp_path / "out", root=tmp_path)
    with pytest.raises(BenchInputError, match="size limit"):
        run_evaluation("retrieval.eval", {"documents": [{"id": "a", "text": "x" * 2_000_001}]},
                       tmp_path / "out", root=tmp_path)


def test_combined_index_and_documents_preserves_index_and_matches_title(tmp_path: Path) -> None:
    from app.modules.ai.retrieval_index import SQLiteIndexStore

    path = tmp_path / "index.sqlite"
    SQLiteIndexStore(db_path=path, documents=lambda: ()).rebuild()
    original = path.read_bytes()
    data = {"index_db_path": str(path), "documents": [{"id": "a", "title": "Example title", "text": "apple"}],
            "questions": [{"id": "q", "query": "apple", "expected_ids": ["Example title"]}]}
    result, verdict, _ = evaluate_retrieval_request(data, root=tmp_path, out_dir=tmp_path)
    assert verdict == "usable"
    assert result.outputs[0].value.value == 1
    assert path.read_bytes() == original


def test_read_only_index_rejects_writes_and_backup_preserves_punctuated_source(tmp_path: Path) -> None:
    import sqlite3

    from app.modules.ai.retrieval_index import SQLiteIndexStore

    path = tmp_path / 'index ?#%.sqlite'
    SQLiteIndexStore(db_path=path, documents=lambda: ()).rebuild()
    original = path.read_bytes()
    source = SQLiteIndexStore(db_path=path, read_only=True)
    with source._open_connection() as db, pytest.raises(sqlite3.OperationalError, match='readonly'):
        db.execute("INSERT INTO meta VALUES ('test', 'value')")
    data = {"index_db_path": str(path), "documents": [{"id": "a", "text": "apple"}],
            "questions": [{"query": "apple", "expected_ids": ["a"]}]}
    result, verdict, _ = evaluate_retrieval_request(data, root=tmp_path, out_dir=tmp_path)
    assert verdict == 'usable'
    assert result.outputs[0].value.value == 1
    assert path.read_bytes() == original
    assert not (tmp_path / 'index ').exists()


def test_title_lookups_are_cached_across_questions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.ai.retrieval_index import SQLiteIndexStore

    calls = 0
    original = SQLiteIndexStore.document_title

    def counted(self: SQLiteIndexStore, ref: object) -> str | None:
        nonlocal calls
        calls += 1
        return original(self, ref)

    monkeypatch.setattr(SQLiteIndexStore, 'document_title', counted)
    request = {"mode": "lexical", "documents": [{"id": "a", "title": "Apple", "text": "apple"}],
               "questions": [{"query": "apple", "expected_ids": ["missing", "Apple"]}] * 3}
    _, verdict, payload = evaluate_retrieval_request(request, root=tmp_path, out_dir=tmp_path)
    assert verdict == 'usable'
    assert [q['first_matching_rank'] for q in payload['questions']] == [1, 1, 1]
    assert calls == 1


def test_accepted_ids_count_for_rank_and_are_reported(tmp_path: Path) -> None:
    request = _request()
    request["questions"] = [
        {"id": "q1", "query": "alpha", "expected_ids": ["missing"], "accepted_ids": ["alpha"]},
        {"id": "q2", "query": "beta", "expected_ids": ["beta"]},
    ]
    _, _, report = evaluate_retrieval_request(request, root=tmp_path, out_dir=tmp_path)
    first, second = report["questions"]
    assert first["accepted_ids"] == ["alpha"] and first["first_matching_rank"] == 1
    assert second["accepted_ids"] == [] and second["first_matching_rank"] == 1
    assert not report["errors"]


@pytest.mark.parametrize("accepted", ["alpha", [""], [1], [None]])
def test_accepted_ids_are_validated_like_expected_ids(tmp_path: Path, accepted: object) -> None:
    request = _request()
    request["questions"] = [{"id": "q", "query": "alpha", "expected_ids": ["alpha"], "accepted_ids": accepted}]
    _, _, report = evaluate_retrieval_request(request, root=tmp_path, out_dir=tmp_path)
    assert report["errors"] and "accepted_ids" in report["errors"][0]["error"]
