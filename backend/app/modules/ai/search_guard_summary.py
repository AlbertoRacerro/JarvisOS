"""Search guard blocked query aggregation and operator transparency (spec 364)."""

from __future__ import annotations

import json
import re
from collections import deque
from pathlib import Path

from app.core import paths
from app.modules.ai.models import SearchGuardBlockedEntryRead, SearchGuardBlockedSummaryRead

REASON_MESSAGES_IT: dict[str, str] = {
    "pack_term": "Termine di progetto protetto rilevato",
    "exact_combination": "Combinazione protetta di progetto rilevata",
    "protected_dimension": "Dimensione protetta di progetto rilevata",
    "guard_groups": "Co-occorrenza di termini protetti rilevata",
    "sanitized_diff": "Rilevato potenziale segreto o dato sensibile nella richiesta",
    "sensitivity_floor": "Livello di riservatezza elevato (S2-S4)",
    "invalid_query": "Sintassi della richiesta di ricerca non valida",
    "pack_load_error": "Errore di caricamento della configurazione del pacchetto",
    "vault_load_error": "Errore di caricamento della configurazione privata (vault)",
    "guard_error": "Errore interno del filtro di protezione",
    "legacy_blocked": "Richiesta bloccata dal filtro (formato precedente)",
}


def read_search_guard_blocked_summary(
    log_file: Path | None = None,
    limit: int = 50,
) -> SearchGuardBlockedSummaryRead:
    """Read recent blocked query entries in plain Italian without exposing secrets or raw queries."""
    target_path = log_file or paths.build_paths().search_guard_log_file
    if not target_path.is_file():
        return SearchGuardBlockedSummaryRead(total_blocked=0, recent_blocked=[])

    total_blocked = 0
    recent_blocked: deque[SearchGuardBlockedEntryRead] = deque(maxlen=max(1, min(limit, 50)))

    def safe_prefix(value: object) -> str:
        prefix = str(value)
        return prefix if re.fullmatch(r"[0-9a-f]{16}", prefix) else ""

    def safe_ts(value: object) -> str:
        timestamp = str(value)
        return timestamp if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)", timestamp) else ""

    try:
        with open(target_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                # Try structured JSONL
                if line.startswith("{") and line.endswith("}"):
                    try:
                        data = json.loads(line)
                        if isinstance(data, dict) and data.get("status") == "blocked":
                            total_blocked += 1
                            raw_code = data.get("reason_code")
                            code = raw_code if isinstance(raw_code, str) and raw_code in REASON_MESSAGES_IT else "legacy_blocked"
                            reason_it = REASON_MESSAGES_IT[code]
                            rule_idx = data.get("rule_index")
                            recent_blocked.append(
                                SearchGuardBlockedEntryRead(
                                    ts=safe_ts(data.get("ts", "")),
                                    sha256_prefix=safe_prefix(data.get("sha256_prefix", "")),
                                    reason_code=code,
                                    reason_it=reason_it,
                                    rule_index=rule_idx if isinstance(rule_idx, int) and not isinstance(rule_idx, bool) and rule_idx >= 0 else None,
                                )
                            )
                        continue
                    except json.JSONDecodeError:
                        pass

                # Legacy format: "<timestamp> <status> <sha256_prefix>"
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "blocked":
                    total_blocked += 1
                    recent_blocked.append(
                        SearchGuardBlockedEntryRead(
                            ts=safe_ts(parts[0]),
                            sha256_prefix=safe_prefix(parts[2]),
                            reason_code="legacy_blocked",
                            reason_it=REASON_MESSAGES_IT["legacy_blocked"],
                            rule_index=None,
                        )
                    )
    except Exception:  # noqa: BLE001 - fail safe on read failure
        return SearchGuardBlockedSummaryRead(total_blocked=0, recent_blocked=[])

    return SearchGuardBlockedSummaryRead(total_blocked=total_blocked, recent_blocked=list(reversed(recent_blocked)))
