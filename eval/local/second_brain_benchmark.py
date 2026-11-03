"""Deterministic offline retrieval benchmark over fixtures and an exact Git commit.

Run from the repository root: python eval/local/second_brain_benchmark.py --embedder e5
(e5 needs sentence-transformers and sqlite-vec; the default hashing embedder needs neither).
Baseline of the last full run: eval/local/second_brain_baseline.md. The repository queries
are hand-registered and some target modules that no longer exist: update them before
trusting recall numbers.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.core.config import get_settings
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.jarvis_context_models import SourceRef
from app.modules.ai.retrieval_contracts import IndexDocument
from app.modules.ai.retrieval_index import (
    E5Embedder,
    Embedder,
    HashingEmbedder,
    SQLiteIndexStore,
    repository_file_documents,
    repository_symbol_documents,
)

_SEEDED = (
    ("pump-sizing", "pump sizing pressure head decision engineering"),
    ("valve-cavitation", "valve cavitation margin assumption engineering"),
    ("mesh-quality", "mesh quality elements nodes evidence"),
    ("reactor-heat", "reactor heat transfer coefficient requirement"),
    ("budget-rule", "project budget zero paid provider policy"),
    ("run-provenance", "simulation run provenance solver version"),
    ("material-strength", "material strength yield stress parameter"),
    ("literature-citation", "literature citation source doi evidence"),
    ("thread-history", "thread interaction history assistant response"),
    ("snapshot-reconcile", "project snapshot reconcile validation digest"),
)


def _percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5))]


# Natural-language queries pre-registered (wave 3) before any benchmark run; not tuned to results.
_REPOSITORY_QUERIES = (
    ("reciprocal rank fusion of lexical and vector search", "backend/app/modules/ai/retrieval_index.py"),
    ("fail when type errors increase compared to a base commit", "scripts/check_typecheck_ratchet.py"),
    ("pressure load integration for the CalculiX FEM adapter",
     "backend/app/modules/bluecad/fem_pressure_integration.py"),
    ("parameter supersedes lifecycle", "backend/app/modules/modeling/parameter_lifecycle.py"),
    ("process stream composition and flow rate", "backend/app/modules/process_kernel/streams.py"),
    ("how to recover the data root after corruption", "docs/DATA_ROOT_RECOVERY.md"),
    ("which spec is active and its hard dependencies", "docs/specs/STATUS.md"),
    ("how work is delivered: roles, spec lifecycle, pull request evidence and merge", "docs/PROCESS.md"),
)
_REPOSITORY_ROOTS = ("backend/app", "backend/tests", "docs", "scripts")


def _run_case(label: str, docs: list[IndexDocument], cases: list[tuple[str, str]], embedder: Embedder,
              repo: Path | None = None) -> dict[str, object]:
    store = SQLiteIndexStore(embedder=embedder, documents=lambda: docs, repository_root=repo,
                             resolver=(lambda ref: next((doc for doc in docs if doc.source_ref == ref), None))
                             if repo is None else None)
    started = time.perf_counter()
    revision = store.rebuild()
    rebuild_seconds = round(time.perf_counter() - started, 3)
    recalls: list[bool] = []
    misses: list[str] = []
    estimates: list[int] = []
    expanded_estimates: list[int] = []
    for query, expected_id in cases:
        hits = store.search_hybrid(query, limit=10)
        found = any(hit.source_ref.object_id == expected_id for hit in hits)
        recalls.append(found)
        if not found:
            misses.append(expected_id)
        estimates.append(store.build_bundle(query, workspace_id=None, token_budget=4000).token_estimate)
        expanded_estimates.append(store.build_bundle(query, workspace_id=None, token_budget=4000,
                                                     expansion_level=1).token_estimate)
    return {
        "case": label, "documents": len(docs), "index_revision": revision,
        "rebuild_seconds": rebuild_seconds, "sqlite_vec_used": store.use_sqlite_vec,
        "queries": len(cases), "recall_at_10": f"{sum(recalls)}/{len(recalls)}",
        "misses": misses,
        "token_estimate_median": statistics.median(estimates),
        "token_estimate_p90": _percentile(estimates, 0.9), "token_estimates": estimates,
        "expanded_token_estimate_median": statistics.median(expanded_estimates),
        "expanded_token_estimate_p90": _percentile(expanded_estimates, 0.9),
        "expanded_token_estimates": expanded_estimates,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embedder", choices=("hashing", "e5"), default="hashing")
    args = parser.parse_args()
    embedder: Embedder = E5Embedder() if args.embedder == "e5" else HashingEmbedder()
    repo = Path(__file__).resolve().parents[2]
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    # Every non-empty, non-binary (per git) tracked file under the indexed roots at HEAD.
    grep = subprocess.run(["git", "-C", str(repo), "grep", "-I", "--name-only", "-e", "", sha, "--",
                           *_REPOSITORY_ROOTS], capture_output=True, text=True, check=True)
    paths = sorted(line.split(":", 1)[1] for line in grep.stdout.splitlines())
    required = (
        "backend/app/modules/ai/execution.py",
        "backend/app/modules/ai/thread_service.py",
        "backend/app/modules/modeling/service.py",
        "backend/app/modules/bluecad/evidence.py",
        "backend/app/modules/project_knowledge/service.py",
    )
    repo_docs = repository_file_documents(repo, sha, paths) + repository_symbol_documents(repo, sha, paths)
    repo_cases = [
        ("run_ai_task", "backend/app/modules/ai/execution.py::run_ai_task"),
        ("get_thread", "backend/app/modules/ai/thread_service.py::get_thread"),
        ("create_decision", "backend/app/modules/modeling/service.py::create_decision"),
        ("get_evidence_record", "backend/app/modules/bluecad/evidence.py::get_evidence_record"),
        ("get_snapshot", "backend/app/modules/project_knowledge/service.py::get_snapshot"),
        *((path, path) for path in required),
        *_REPOSITORY_QUERIES,
    ]
    seeded_docs = [
        IndexDocument(source_ref=SourceRef(
            authority_owner="fixture", object_type="record", object_id=object_id,
            workspace_id="benchmark", content_digest=canonical_digest(content),
        ), text=content)
        for object_id, content in _SEEDED
    ]
    seeded_cases = [(content, object_id) for object_id, content in _SEEDED]
    with TemporaryDirectory() as temporary:
        os.environ["JARVISOS_DATA_ROOT"] = temporary
        get_settings.cache_clear()
        seeded_result = _run_case("seeded_workspace", seeded_docs, seeded_cases, embedder)
        os.environ["JARVISOS_DATA_ROOT"] = str(Path(temporary) / "repository")
        get_settings.cache_clear()
        repository_result = _run_case("repository_at_head", repo_docs, repo_cases, embedder, repo)
        get_settings.cache_clear()
    print(json.dumps({"sha": sha, "embedder": args.embedder, "indexed_roots": list(_REPOSITORY_ROOTS),
                      "repository_files": len(paths), "cases": [seeded_result, repository_result]},
                     sort_keys=True))


if __name__ == "__main__":
    main()
