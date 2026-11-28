#!/usr/bin/env python3
"""Offline process miner and capability analyzer for Spec 388.

Stdlib only. Mines tool transition matrices, frequent tool chains, cross-model
common vs divergent steps, loops/retries, parallel bursts, and wish-list items
annotated with capability reuse checks from exploration logs.
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from scripts.benchmarks.agent_eval.explorer import _compute_duration_ms

FALLBACK_CAPABILITIES: dict[str, str] = {
    "jarvis_retrieval_query": "jarvis.retrieval_query",
    "jarvis_decide": "jarvis.decide",
    "jarvis_dev_read": "jarvis.dev_read",
    "jarvis_repo_read": "jarvis.repo_read",
    "jarvis_knowledge_read": "jarvis.knowledge_read",
    "jarvis_paper_search": "jarvis.paper_search",
    "paper_search": "jarvis.paper_search",
    "jarvis_patent_read": "jarvis.patent_read",
    "patent_read": "jarvis.patent_read",
    "jarvis_browser_page_read": "jarvis.browser_page_read",
    "browser_page_read": "jarvis.browser_page_read",
    "jarvis_paper_pdf_read": "jarvis.paper_pdf_read",
    "paper_pdf_read": "jarvis.paper_pdf_read",
    "jarvis_turn_plan": "jarvis.turn_plan",
    "turn_plan": "jarvis.turn_plan",
    "jarvis_consult": "jarvis.consult",
    "jarvis_consult_read": "jarvis.consult_read",
    "jarvis_process_read": "jarvis.process_read",
    "jarvis_process_act": "jarvis.process_act",
    "jarvis_bluecad_read": "jarvis.bluecad_read",
    "jarvis_bluecad_act": "jarvis.bluecad_act",
    "jarvis_studio_read": "jarvis.studio_read",
    "jarvis_studio_act": "jarvis.studio_act",
    "jarvis_bench_read": "jarvis.bench_read",
    "jarvis_bench_run": "jarvis.bench_run",
    "jarvis_python": "jarvis.calc_run",
}

_STOP_WORDS = frozenset({
    "the", "and", "tool", "tools", "need", "needed", "missing", "for", "read",
    "with", "from", "that", "this", "would", "like", "able", "allow", "support",
    "using", "into", "can", "could", "should", "want", "more", "have", "been",
    "will", "when", "what", "which", "where", "how", "all", "any", "some",
})


def load_known_capabilities() -> dict[str, str]:
    """Parse _TOOL_CAPABILITIES from hermes supervisor.py via AST, with fallback."""
    supervisor_path = (
        Path(__file__).resolve().parents[3]
        / "backend"
        / "app"
        / "modules"
        / "agents"
        / "hermes"
        / "supervisor.py"
    )
    if supervisor_path.is_file():
        try:
            content = supervisor_path.read_text(encoding="utf-8")
            tree = ast.parse(content)
            for stmt in tree.body:
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name) and target.id == "_TOOL_CAPABILITIES":
                            if isinstance(stmt.value, ast.Dict):
                                caps: dict[str, str] = {}
                                for k, v in zip(stmt.value.keys, stmt.value.values, strict=False):
                                    k_val = getattr(k, "value", None) or getattr(k, "s", None)
                                    v_val = getattr(v, "value", None) or getattr(v, "s", None)
                                    if isinstance(k_val, str) and isinstance(v_val, str):
                                        caps[k_val] = v_val
                                if caps:
                                    return caps
        except Exception:
            pass

    return dict(FALLBACK_CAPABILITIES)


def _normalize_text(text: str) -> str:
    """Normalize bullet items for deduplication."""
    clean = re.sub(r"[^\w\s-]", "", text.strip().lower())
    return " ".join(clean.split())


def _extract_meaningful_tokens(text: str) -> set[str]:
    """Extract informative alphanumeric tokens length >= 3 excluding stop words."""
    words = re.findall(r"[a-z0-9_-]+", text.lower())
    return {w for w in words if len(w) >= 3 and w not in _STOP_WORDS}


def _check_reuse(wish: str, known_capabilities: dict[str, str]) -> list[str]:
    """Match wish text against known capabilities using token overlap."""
    wish_tokens = _extract_meaningful_tokens(wish)
    if not wish_tokens:
        return []

    matched: set[str] = set()
    for tool_name, cap_id in known_capabilities.items():
        cap_tokens = _extract_meaningful_tokens(f"{tool_name} {cap_id}".replace(".", " ").replace("_", " "))
        overlap = wish_tokens.intersection(cap_tokens)
        if overlap:
            matched.add(cap_id)

    return sorted(matched)


def mine_exploration_logs(
    log_paths: list[Path | str],
    *,
    min_support: int = 2,
    known_capabilities: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Mine deterministic processes, chains, bursts, and wish lists from exploration logs."""
    if not log_paths:
        raise ValueError("log_paths must contain at least one path")

    if known_capabilities is None:
        known_capabilities = load_known_capabilities()

    # Load and organize logs
    # logs_by_model: model_name -> list of request records
    logs_by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    requests_by_id: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)

    for p in log_paths:
        path = Path(p)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        model_name = str(data.get("model") or path.stem)
        reqs = data.get("requests", []) if isinstance(data, dict) else []
        for req in reqs:
            if not isinstance(req, dict):
                continue
            r_model = str(req.get("model") or model_name)
            req_id = str(req.get("request_id", ""))
            logs_by_model[r_model].append(req)
            if req_id:
                requests_by_id[req_id][r_model] = req

    # 1. Transitions and Frequent Chains across all traces
    all_traces: list[tuple[str, list[dict[str, Any]]]] = []
    for _model_name, reqs in logs_by_model.items():
        for req in reqs:
            req_id = str(req.get("request_id", ""))
            trace = req.get("tool_trace", [])
            if isinstance(trace, list):
                all_traces.append((req_id, trace))

    pair_counts: Counter[tuple[str, str]] = Counter()
    from_totals: Counter[str] = Counter()
    chain_counts: Counter[tuple[str, ...]] = Counter()

    for _, trace in all_traces:
        tool_names = [str(call.get("tool_name", "")).strip() for call in trace if call.get("tool_name")]
        for i in range(len(tool_names) - 1):
            a, b = tool_names[i], tool_names[i + 1]
            pair_counts[(a, b)] += 1
            from_totals[a] += 1

        for n in range(2, 5):
            for i in range(len(tool_names) - n + 1):
                sub = tuple(tool_names[i : i + n])
                chain_counts[sub] += 1

    transitions: list[dict[str, Any]] = []
    for (a, b), count in pair_counts.most_common():
        total = from_totals[a]
        prob = round(count / total, 4) if total > 0 else 0.0
        transitions.append({
            "from": a,
            "to": b,
            "count": count,
            "probability": prob,
        })

    frequent_chains: list[dict[str, Any]] = []
    for chain, count in chain_counts.most_common():
        if count >= min_support:
            frequent_chains.append({
                "chain": list(chain),
                "count": count,
                "support": count,
            })
    # Sort by count desc, length desc, chain asc
    frequent_chains.sort(key=lambda x: (-x["count"], -len(x["chain"]), x["chain"]))

    # 2. Cross-model comparison
    cross_model_details: list[dict[str, Any]] = []
    agreement_rates: list[float] = []

    for req_id, models_dict in sorted(requests_by_id.items()):
        by_model_tools: dict[str, list[str]] = {}
        tool_sets: list[set[str]] = []
        for m_name, req in models_dict.items():
            t_names = [str(c.get("tool_name", "")).strip() for c in req.get("tool_trace", []) if c.get("tool_name")]
            by_model_tools[m_name] = t_names
            tool_sets.append(set(t_names))

        if len(tool_sets) >= 2:
            common = set.intersection(*tool_sets)
            union = set.union(*tool_sets)
            divergent = union - common
            rate = len(common) / len(union) if union else 1.0
            agreement_rates.append(rate)
        elif tool_sets:
            common = tool_sets[0]
            divergent = set()
            rate = 1.0
            agreement_rates.append(rate)
        else:
            common = set()
            divergent = set()
            rate = 1.0

        cross_model_details.append({
            "request_id": req_id,
            "by_model": by_model_tools,
            "common_tools": sorted(common),
            "divergent_tools": sorted(divergent),
            "agreement_rate": round(rate, 4),
        })

    mean_agreement = (
        round(sum(agreement_rates) / len(agreement_rates), 4)
        if agreement_rates
        else 1.0
    )

    # 3. Loops and Retries
    consecutive_repeats: list[dict[str, Any]] = []
    failed_retries: list[dict[str, Any]] = []

    for req_id, trace in all_traces:
        i = 0
        while i < len(trace):
            tool = str(trace[i].get("tool_name", "")).strip()
            repeat_count = 1
            while i + 1 < len(trace) and str(trace[i + 1].get("tool_name", "")).strip() == tool:
                repeat_count += 1
                i += 1
            if repeat_count > 1 and tool:
                consecutive_repeats.append({
                    "request_id": req_id,
                    "tool": tool,
                    "repeats": repeat_count,
                })
            i += 1

        for j in range(len(trace) - 1):
            call_a = trace[j]
            call_b = trace[j + 1]
            status_a = str(call_a.get("status", "")).lower()
            err_a = call_a.get("error_code")
            if status_a not in ("succeeded", "success") or err_a is not None:
                failed_retries.append({
                    "request_id": req_id,
                    "tool": str(call_a.get("tool_name", "")).strip(),
                    "error_code": err_a,
                    "retried_with": str(call_b.get("tool_name", "")).strip(),
                })

    # 4. Parallel Bursts (overlapping execution windows)
    parallel_bursts: list[dict[str, Any]] = []
    for req_id, trace in all_traces:
        for k in range(len(trace) - 1):
            curr_call = trace[k]
            next_call = trace[k + 1]
            curr_end = curr_call.get("completed_at")
            next_start = next_call.get("started_at")
            if curr_end and next_start:
                # If next_start happened before curr_end
                overlap = _compute_duration_ms(next_start, curr_end)
                if overlap is not None and overlap > 0:
                    parallel_bursts.append({
                        "request_id": req_id,
                        "tools": [
                            str(curr_call.get("tool_name", "")).strip(),
                            str(next_call.get("tool_name", "")).strip(),
                        ],
                        "overlap_ms": overlap,
                    })

    # 5. Workarounds and Wish List
    workaround_counts: Counter[str] = Counter()
    workaround_examples: dict[str, list[str]] = defaultdict(list)

    wish_counts: Counter[str] = Counter()
    wish_examples: dict[str, list[str]] = defaultdict(list)

    for req_id, models_dict in requests_by_id.items():
        for _, req in models_dict.items():
            wr = req.get("work_report", {})
            if isinstance(wr, dict):
                for wa in wr.get("workarounds", []):
                    norm = _normalize_text(str(wa))
                    if norm:
                        workaround_counts[norm] += 1
                        if req_id not in workaround_examples[norm]:
                            workaround_examples[norm].append(req_id)

                for w in wr.get("wish_list", []):
                    norm = _normalize_text(str(w))
                    if norm:
                        wish_counts[norm] += 1
                        if req_id not in wish_examples[norm]:
                            wish_examples[norm].append(req_id)

    workarounds_res: list[dict[str, Any]] = []
    for item, count in workaround_counts.most_common():
        workarounds_res.append({
            "workaround": item,
            "count": count,
            "example_request_ids": sorted(workaround_examples[item])[:5],
        })

    wish_res: list[dict[str, Any]] = []
    for item, count in wish_counts.most_common():
        reuse_matches = _check_reuse(item, known_capabilities)
        wish_res.append({
            "wish": item,
            "count": count,
            "example_request_ids": sorted(wish_examples[item])[:5],
            "reuse_check": reuse_matches,
        })

    return {
        "transitions": transitions,
        "frequent_chains": frequent_chains,
        "cross_model": {
            "mean_agreement_rate": mean_agreement,
            "per_request": cross_model_details,
        },
        "loops_and_retries": {
            "consecutive_repeats": consecutive_repeats,
            "failed_retries": failed_retries,
        },
        "parallel_bursts": parallel_bursts,
        "workarounds": workarounds_res,
        "wish_list": wish_res,
    }


def format_mining_report(report: dict[str, Any]) -> str:
    """Format mining results into a concise Markdown summary."""
    lines: list[str] = [
        "# Agent Eval Exploration Mining Report",
        "",
        "## 1. Top Tool Transitions & Frequent Chains",
        "",
    ]

    transitions = report.get("transitions", [])[:10]
    if transitions:
        lines.append("| From | To | Count | P(To \\| From) |")
        lines.append("| --- | --- | --- | --- |")
        for t in transitions:
            lines.append(f"| `{t['from']}` | `{t['to']}` | {t['count']} | {t['probability']:.2f} |")
        lines.append("")
    else:
        lines.append("No transitions observed.\n")

    chains = report.get("frequent_chains", [])[:8]
    if chains:
        lines.append("### Frequent Tool Chains (Recipe Candidates)")
        for c in chains:
            chain_str = " -> ".join(f"`{t}`" for t in c["chain"])
            lines.append(f"- **{chain_str}** (count: {c['count']})")
        lines.append("")

    lines.append("## 2. Cross-Model Comparison & Agreement")
    lines.append("")
    cm = report.get("cross_model", {})
    mean_rate = cm.get("mean_agreement_rate", 1.0)
    lines.append(f"- **Mean Agreement Rate**: {mean_rate * 100:.1f}%")
    per_req = cm.get("per_request", [])
    if per_req:
        lines.append(f"- **Requests Analyzed**: {len(per_req)}")
    lines.append("")

    lines.append("## 3. Loops, Retries & Parallel Bursts")
    lines.append("")
    lr = report.get("loops_and_retries", {})
    repeats = lr.get("consecutive_repeats", [])
    retries = lr.get("failed_retries", [])
    bursts = report.get("parallel_bursts", [])
    lines.append(f"- **Consecutive Repeats**: {len(repeats)}")
    lines.append(f"- **Failed Retries**: {len(retries)}")
    lines.append(f"- **Parallel Bursts (Overlapping Calls)**: {len(bursts)}")
    lines.append("")

    lines.append("## 4. Wish List & Capability Reuse Check")
    lines.append("")
    wishes = report.get("wish_list", [])[:10]
    if wishes:
        lines.append("| Missing Capability / Wish | Count | Existing Jarvis Reuse Matches |")
        lines.append("| --- | --- | --- |")
        for w in wishes:
            reuse = ", ".join(f"`{r}`" for r in w["reuse_check"]) if w["reuse_check"] else "*(none)*"
            lines.append(f"| {w['wish']} | {w['count']} | {reuse} |")
        lines.append("")
    else:
        lines.append("No wish-list items reported.\n")

    return "\n".join(lines)
