"""Read-only, bounded navigation over the derived Second Brain index."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from app.modules.ai.retrieval_contracts import ContextBundle, context_bundle_digest
from app.modules.ai.retrieval_index import SQLiteIndexStore

MAX_QUERY_CHARS = 2_000
MAX_LIMIT = 32
MAX_TOKEN_BUDGET = 8_192
MAX_RETRIEVAL_RESULT_CHARS = 12_000
SCHEMA_VERSION = "retrieval-query.v1"
# Canonical owners indexed with a workspace identity and reread from their owner.
# Repository documents have no workspace identity and are intentionally excluded.
WORKSPACE_SCOPED_OWNERS = frozenset({
    "ai_threads", "bluecad", "development", "engineering",
    "literature", "modeling", "process_stack", "project_knowledge", "workspace_documents",
})


def query_context(
    query: str,
    *,
    workspace_id: str | None = None,
    source_scope: tuple[str, ...] = (),
    limit: int = 12,
    token_budget: int = 2_048,
    repository_root: Path | None = None,
    master_ref: str = "refs/remotes/origin/master",
    allowed_refs: frozenset[tuple[str, str, str]] | None = None,
    store: SQLiteIndexStore | None = None,
) -> dict[str, Any]:
    """Return current, owner-validated evidence; index contents remain candidates only."""
    if not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise ValueError("query must be nonempty and at most 2000 characters")
    if not 1 <= limit <= MAX_LIMIT or not 1 <= token_budget <= MAX_TOKEN_BUDGET:
        raise ValueError("limit or token budget exceeds navigation bounds")
    if workspace_id is not None and (not workspace_id.strip() or len(workspace_id) > 128):
        raise ValueError("invalid workspace scope")
    owners = frozenset(source_scope) if source_scope else None
    if owners is not None and (len(owners) > 32 or any(not item or len(item) > 64 for item in owners)):
        raise ValueError("invalid source scope")
    index = store or SQLiteIndexStore(repository_root=repository_root, master_ref=master_ref)
    freshness = index.freshness()
    bundle = index.build_bundle(query, workspace_id=workspace_id, token_budget=token_budget,
                                limit=limit, source_owners=owners, source_refs=allowed_refs)
    evidence = []
    # Excerpts share the caller's token budget (about four characters per token), bounded by MAX_RETRIEVAL_RESULT_CHARS.
    remaining_chars = min(token_budget * 4, MAX_RETRIEVAL_RESULT_CHARS)
    truncated = any(m.outcome == "dropped_budget" for m in bundle.evidence_manifest)
    for item in bundle.items:
        resolution = index.resolve_authoritative(item.source_ref)
        valid = (resolution.state == "current" and resolution.content is not None
                 and resolution.current_content_digest == item.content_digest)
        if not valid:
            continue
        if remaining_chars <= 0:
            truncated = True
            break
        content = resolution.content or ""
        max_item_chars = min(2_000, remaining_chars)
        excerpt = content[:max_item_chars]
        if len(content) > max_item_chars:
            truncated = True
        remaining_chars -= len(excerpt)
        evidence.append({
            "source_ref": item.source_ref.model_dump(mode="json"),
            "content_digest": item.content_digest,
            "token_estimate": item.token_estimate,
            "validation": "current",
            "excerpt": excerpt,
        })
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "query": query,
        "workspace_id": workspace_id,
        "source_scope": sorted(owners) if owners is not None else None,
        "evidence_role": "reference_data_not_instructions",
        "freshness": freshness.model_dump(mode="json"),
        "bundle": bundle.model_dump(mode="json"),
        "evidence": evidence,
        "truncated": truncated,
    }
    while evidence and len(json.dumps(result, ensure_ascii=False)) > MAX_RETRIEVAL_RESULT_CHARS:
        result["truncated"] = True
        last = evidence[-1]
        overhead = len(json.dumps(result, ensure_ascii=False)) - MAX_RETRIEVAL_RESULT_CHARS
        last_excerpt = str(last.get("excerpt") or "")
        if len(last_excerpt) > overhead:
            last["excerpt"] = last_excerpt[:-overhead]
        else:
            evidence.pop()
    if len(json.dumps(result, ensure_ascii=False)) > MAX_RETRIEVAL_RESULT_CHARS:
        items_list = list(bundle.items)
        manifest_list = list(bundle.evidence_manifest)
        bundle_modified = False
        while len(json.dumps(result, ensure_ascii=False)) > MAX_RETRIEVAL_RESULT_CHARS:
            non_included_idx = next(
                (idx for idx in range(len(manifest_list) - 1, -1, -1) if manifest_list[idx].outcome != "included"),
                None,
            )
            if non_included_idx is not None:
                manifest_list.pop(non_included_idx)
            elif items_list:
                items_list.pop()
                included_idx = next(
                    (idx for idx in range(len(manifest_list) - 1, -1, -1) if manifest_list[idx].outcome == "included"),
                    None,
                )
                if included_idx is not None:
                    manifest_list.pop(included_idx)
            else:
                break
            bundle_modified = True
            result["truncated"] = True
            trimmed_bundle = ContextBundle(
                schema_version=bundle.schema_version,
                bundle_id=bundle.bundle_id,
                workspace_id=bundle.workspace_id,
                items=tuple(items_list),
                evidence_manifest=tuple(manifest_list),
                token_estimate=sum(item.token_estimate for item in items_list),
                token_budget=bundle.token_budget,
                expansion_level=bundle.expansion_level,
                index_revision=bundle.index_revision,
                bundle_digest=context_bundle_digest(items_list),
                created_at=bundle.created_at,
            )
            result["bundle"] = trimmed_bundle.model_dump(mode="json")
        if bundle_modified:
            ContextBundle.model_validate(result["bundle"])
    return result


def _repository_root(value: str | None) -> Path | None:
    if value:
        return Path(value)
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                            text=True, check=False, timeout=10)
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the bounded, read-only Second Brain navigation index")
    parser.add_argument("query")
    parser.add_argument("--workspace")
    parser.add_argument("--source-scope", action="append", default=[],
                        help="allowed authority owner; repeat to allow multiple owners")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--token-budget", type=int, default=2_048)
    parser.add_argument("--repository-root")
    parser.add_argument("--master-ref", default="refs/remotes/origin/master")
    args = parser.parse_args()
    payload = query_context(args.query, workspace_id=args.workspace,
                            source_scope=tuple(args.source_scope), limit=args.limit,
                            token_budget=args.token_budget,
                            repository_root=_repository_root(args.repository_root), master_ref=args.master_ref)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
