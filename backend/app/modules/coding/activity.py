"""Spec 298: one read-only read model of running and recently finished long work.

Each source is read independently; an unreadable source becomes one group marked ``non disponibile`` and never
fails the panel. Nothing here starts, stops or writes anything.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from app.core.database import open_sqlite_connection
from app.modules.coding import provider_quota, relay_monitor
from app.modules.coding.control_room import RegistryRow, parse_registry, short_title
from app.modules.coding.runtime_truth import CANONICAL_RUNTIME_REPOSITORY
from app.modules.process_stack import dynamic_jobs

MAX_ROWS = 50
WINDOW = timedelta(hours=24)
LOG_LINES = 40
LOG_LINE_CHARS = 400
LOG_TAIL_BYTES = 65_536
WORKER_CLIS = frozenset({"claude", "codex", "gemini", "copilot", "agy"})
ACTIVE_MISSION = frozenset({"running", "checkpointed", "queued", "waiting"})
SCAN_FILES = 150

Runner = Literal["supervisione", "modello di lavoro", "motore locale", "lavoro notturno"]
State = Literal["running", "done", "failed", "unknown"]
ActivityKind = Literal[
    "supervisor",
    "worker",
    "job",
    "dynamic",
    "script",
    "generation",
    "ai_error",
    "download",
]


class ActivityProgress(BaseModel):
    done: float
    total: float
    unit: str


class ActivityLink(BaseModel):
    kind: Literal["pr", "run", "file"]
    label: str
    target: str


class ActivityRow(BaseModel):
    key: str
    kind: ActivityKind
    title: str
    runner: Runner
    started_at: str | None
    ended_at: str | None
    state: State
    state_word: str
    progress: ActivityProgress | None
    step: str | None
    outcome: str | None
    links: list[ActivityLink]


class ActivityGroup(BaseModel):
    id: str
    title: str
    state: Literal["ok", "non disponibile"]
    reason: str | None


class ActivityQuotaPool(BaseModel):
    label: str
    percent_free: int | None


class ActivityQuota(BaseModel):
    state: Literal["ok", "non disponibile"]
    pools: list[ActivityQuotaPool]


class ActivityRead(BaseModel):
    generated_at: str
    running: int
    rows: list[ActivityRow]
    groups: list[ActivityGroup]
    quota: ActivityQuota
    downloads: list[ActivityRow]
    downloads_empty: str


class ActivityDetailRead(BaseModel):
    state: Literal["ok", "refused", "not_found"]
    reason: str | None
    title: str | None
    label: str | None
    lines: list[str]


_ID_LIKE = re.compile(
    r"\b(?:j-\d{8}T\d{6}Z-[0-9a-f]{6}|m-\d{8}T\d{6}Z-[0-9a-f]{6}|L\d{14}-[0-9a-f]{4}|[0-9a-f]{7,40}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b"
)
_COMMAND_LIKE = re.compile(r"(?:^|\s)(?:\$ |python3?\b|git\s|pip\s|npm\s|node\s|bash\s|sh\s|scripts/|--[a-z])")
_SPEC_NUMBER = re.compile(r"\b(\d{3}[a-z]?)\b")

JOB_WORDS: dict[str, tuple[State, str]] = {
    "starting": ("running", "in avvio"),
    "running": ("running", "in corso"),
    "orphaned": ("unknown", "da verificare"),
    "unknown": ("unknown", "stato non verificabile da qui"),
    "succeeded": ("done", "completato"),
    "failed": ("failed", "non riuscito"),
    "cancelled": ("failed", "annullato"),
    "timeout": ("failed", "tempo scaduto"),
    "interrupted": ("failed", "interrotto"),
    "lost": ("failed", "perso"),
}
DYNAMIC_WORDS: dict[str, tuple[State, str]] = {
    "queued": ("running", "in coda"),
    "running": ("running", "in corso"),
    "succeeded": ("done", "completato"),
    "failed": ("failed", "non riuscito"),
    "cancelled": ("failed", "annullato"),
}
RUNNER_WORDS: dict[str, tuple[State, str]] = {**DYNAMIC_WORDS, "timeout": ("failed", "tempo scaduto")}
MISSION_WORDS = {
    "running": "in corso",
    "checkpointed": "in corso",
    "queued": "in coda",
    "waiting": "in attesa dei lavori registrati",
    "held": "in attesa di una tua verifica",
    "paused": "in pausa",
    "stopped": "fermo",
    "unknown": "stato non verificabile da qui",
}
AI_ERROR_WORDS = {
    "provider_error": "errore del fornitore",
    "route_unavailable": "nessun percorso disponibile",
    "validation_error": "risposta non valida",
    "config_error": "configurazione non valida",
}
NOT_VERIFIABLE = "stato non verificabile da qui"
UNAVAILABLE: Literal["non disponibile"] = "non disponibile"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _visible(text: str, fallback: str) -> str:
    """A fixed phrase or readable title that never carries an id, a SHA or a command."""
    return fallback if _ID_LIKE.search(text) or _COMMAND_LIKE.search(text) else text


def _recent(ended: str | None, now: datetime) -> bool:
    moment = _parse(ended)
    return moment is None or now - moment <= WINDOW


def _registry() -> dict[str, RegistryRow]:
    try:
        text = (relay_monitor.REPO_ROOT / "docs" / "specs" / "STATUS.md").read_text(encoding="utf-8")
    except OSError:
        return {}
    return parse_registry(text)[0]


def _spec_row(spec: Any, registry: dict[str, RegistryRow]) -> RegistryRow | None:
    match = _SPEC_NUMBER.search(str(spec or ""))
    return registry.get(match.group(1)) if match else None


def _titled(phrase: str, spec: RegistryRow | None) -> str:
    return _visible(f"{phrase}: {short_title(spec.name)}", phrase) if spec else phrase


def _spec_links(spec: RegistryRow | None) -> list[ActivityLink]:
    if spec is None:
        return []
    return [ActivityLink(kind="pr", label="Apri la modifica", target=f"https://github.com/{CANONICAL_RUNTIME_REPOSITORY}/pull/{n}")
            for n in spec.implementation_prs[:2]]


def _supervisor_rows(now: datetime, registry: dict[str, RegistryRow]) -> list[ActivityRow]:
    root = relay_monitor.control_root()
    if not root.is_dir():
        raise OSError("owner_state_missing")
    snap = relay_monitor.snapshot(now)
    if snap.get("state") == "refused":
        raise OSError("snapshot_refused")
    mission = snap.get("mission")
    if not isinstance(mission, dict) or not mission.get("task"):
        if snap.get("state") == "unknown" and snap.get("reason"):
            raise OSError(str(snap["reason"]))
        return []
    state = str(snap.get("state") or "unknown")
    word = MISSION_WORDS.get(state, NOT_VERIFIABLE)
    since = snap.get("waiting_since") or (snap.get("turn") or {}).get("at") or snap.get("at")
    rows = [ActivityRow(
        key="mission", kind="supervisor", title="Il supervisore sta portando avanti il lavoro",
        runner="supervisione", started_at=since, ended_at=None,
        state="running" if state in ACTIVE_MISSION else "unknown", state_word=word, progress=None,
        step=word, outcome=None, links=[])]
    if mission.get("agent") and state in ("running", "checkpointed"):
        rows.append(ActivityRow(
            key="worker", kind="worker", title="Un modello di lavoro sta svolgendo il compito",
            runner="modello di lavoro", started_at=since, ended_at=None, state="running", state_word="in corso",
            progress=None, step="sto lavorando al compito assegnato", outcome=None, links=[]))
    return rows


def _job_runner(job: dict[str, Any]) -> Runner:
    """A worker model when the job launches a worker CLI, otherwise local computation."""
    argv = " ".join(str(part) for part in job.get("argv") or [])
    return "modello di lavoro" if WORKER_CLIS & set(re.findall(r"[a-z]+", argv)) else "motore locale"


def _job_rows(now: datetime, registry: dict[str, RegistryRow]) -> list[ActivityRow]:
    rj = relay_monitor.observe().rc.relay_jobs
    root = relay_monitor.control_root()
    paths = sorted(rj.jobs_dir(root).glob("j-*.json"), reverse=True)[:SCAN_FILES]
    rows: list[ActivityRow] = []
    for path in paths:
        if len(rows) >= MAX_ROWS:
            break
        job = rj.load(path.stem, root)
        if job is None:
            continue
        status = str(rj.effective_status(job))
        kind, word = JOB_WORDS.get(status, ("unknown", NOT_VERIFIABLE))
        ended = job.get("ended_at") if kind in ("done", "failed") else None
        if kind in ("done", "failed") and not _recent(ended or job.get("created_at"), now):
            continue
        spec = _spec_row(job.get("spec"), registry)
        rows.append(ActivityRow(
            key=f"job:{job['id']}", kind="job", title=_titled("Un lavoro di calcolo registrato", spec),
            runner=_job_runner(job), started_at=job.get("started_at") or job.get("created_at"), ended_at=ended,
            state=kind, state_word=word, progress=None,
            step="sto elaborando" if kind == "running" else None,
            outcome=word if kind in ("done", "failed") else None, links=_spec_links(spec)))
    return rows


def _dynamic_rows(now: datetime, registry: dict[str, RegistryRow]) -> list[ActivityRow]:
    rows = []
    for record in dynamic_jobs.list_recent(MAX_ROWS):
        kind, word = DYNAMIC_WORDS.get(str(record.get("status")), ("unknown", NOT_VERIFIABLE))
        ended = record.get("finished_at") if kind in ("done", "failed") else None
        if kind in ("done", "failed") and not _recent(ended or record.get("created_at"), now):
            continue
        fraction = record.get("progress")
        progress = (ActivityProgress(done=round(min(1.0, max(0.0, float(fraction))) * 100), total=100, unit="%")
                    if kind == "running" and isinstance(fraction, (int, float)) and not isinstance(fraction, bool) else None)
        rows.append(ActivityRow(
            key=f"dynamic:{record.get('workspace_id')}/{record.get('job_id')}", kind="dynamic",
            title="Una simulazione dinamica", runner="motore locale",
            started_at=record.get("started_at") or record.get("created_at"), ended_at=ended, state=kind,
            state_word=word, progress=progress, step="sto simulando" if kind == "running" else None,
            outcome=word if kind in ("done", "failed") else None,
            links=[ActivityLink(kind="run", label="Apri il processo", target="/design/process")]))
    return rows


def _script_rows(now: datetime, registry: dict[str, RegistryRow]) -> list[ActivityRow]:
    cutoff = _iso(now - WINDOW)
    with open_sqlite_connection(read_only=True) as connection:
        found = connection.execute(
            "SELECT status, created_at, updated_at FROM runner_jobs "
            "WHERE status IN ('queued', 'running') OR updated_at >= ? ORDER BY created_at DESC LIMIT ?",
            (cutoff, MAX_ROWS)).fetchall()
    rows = []
    for index, record in enumerate(found):
        kind, word = RUNNER_WORDS.get(str(record["status"]), ("unknown", NOT_VERIFIABLE))
        rows.append(ActivityRow(
            key=f"script:{index}:{record['created_at']}", kind="script", title="Un'esecuzione di script",
            runner="motore locale", started_at=record["created_at"],
            ended_at=record["updated_at"] if kind in ("done", "failed") else None, state=kind, state_word=word,
            progress=None, step="sto eseguendo lo script" if kind == "running" else None,
            outcome=word if kind in ("done", "failed") else None,
            links=[ActivityLink(kind="run", label="Apri le esecuzioni", target="/runs")]))
    return rows


def _flow_rows(now: datetime, registry: dict[str, RegistryRow]) -> list[ActivityRow]:
    cutoff = _iso(now - WINDOW)
    with open_sqlite_connection(read_only=True) as connection:
        found = connection.execute(
            "SELECT created_at FROM ai_flows WHERE state = 'running' AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT ?", (cutoff, MAX_ROWS)).fetchall()
    return [ActivityRow(
        key=f"flow:{index}:{record['created_at']}", kind="generation", title="Una risposta in generazione",
        runner="motore locale", started_at=record["created_at"], ended_at=None, state="running",
        state_word="in corso", progress=None, step="sto generando il testo", outcome=None, links=[])
        for index, record in enumerate(found)]


def _ai_error_rows(now: datetime, registry: dict[str, RegistryRow]) -> list[ActivityRow]:
    cutoff = _iso(now - WINDOW)
    with open_sqlite_connection(read_only=True) as connection:
        found = connection.execute(
            "SELECT status, created_at FROM ai_jobs WHERE status IN (?, ?, ?, ?) AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT ?",
            (*AI_ERROR_WORDS, cutoff, MAX_ROWS)).fetchall()
    return [ActivityRow(
        key=f"ai:{index}:{record['created_at']}", kind="ai_error", title="Una chiamata AI non è andata a buon fine",
        runner="motore locale", started_at=record["created_at"], ended_at=record["created_at"], state="failed",
        state_word="non riuscito", progress=None, step=None, outcome=AI_ERROR_WORDS.get(str(record["status"])),
        links=[]) for index, record in enumerate(found)]


SOURCES: list[tuple[str, str, Callable[[datetime, dict[str, RegistryRow]], list[ActivityRow]]]] = [
    ("supervisor", "Supervisione", _supervisor_rows),
    ("jobs", "Lavori registrati", _job_rows),
    ("dynamic", "Simulazioni dinamiche", _dynamic_rows),
    ("scripts", "Esecuzioni di script", _script_rows),
    ("flows", "Generazioni locali", _flow_rows),
    ("ai_errors", "Chiamate AI non riuscite", _ai_error_rows),
]


def _quota() -> ActivityQuota:
    try:
        providers = provider_quota.quotas().get("providers") or []
        pools = []
        for provider in providers:
            free = [w["remaining_percent"] for w in provider.get("windows") or []
                    if isinstance(w.get("remaining_percent"), (int, float)) and not isinstance(w["remaining_percent"], bool)]
            pools.append(ActivityQuotaPool(label=str(provider.get("label") or provider.get("provider")),
                                           percent_free=round(min(free)) if free else None))
        return ActivityQuota(state="ok", pools=pools)
    except Exception:  # noqa: BLE001 - quota is one line; its failure must not fail the panel
        return ActivityQuota(state="non disponibile", pools=[])


def _sort_key(row: ActivityRow) -> datetime:
    return _parse(row.started_at) or _parse(row.ended_at) or datetime.min.replace(tzinfo=UTC)


def _download_rows(now: datetime) -> list[ActivityRow]:
    root = relay_monitor.control_root()
    d_dir = root / "downloads"
    if not d_dir.is_dir():
        return []
    paths = sorted(d_dir.glob("*.json"), reverse=True)[:SCAN_FILES]
    rows: list[ActivityRow] = []
    for path in paths:
        if len(rows) >= MAX_ROWS:
            break
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        state = str(data.get("state") or "")
        if state not in ("running", "succeeded", "failed"):
            continue
        ended = data.get("ended_at") if state in ("succeeded", "failed") else None
        if state in ("succeeded", "failed") and not _recent(ended or data.get("started_at"), now):
            continue

        label = str(data.get("label") or data.get("file_name") or "file")
        title = _visible(f"Download di {label}", "Download di un file")

        if state == "running":
            # A running download rewrites its record while bytes arrive; the writer's pid may live in another
            # namespace, so only the age of the last write is evidence, and silence proves nothing either way.
            try:
                is_stale = now - datetime.fromtimestamp(path.stat().st_mtime, tz=UTC) > timedelta(minutes=2)
            except OSError:
                is_stale = True
            if is_stale:
                row_state: State = "unknown"
                state_word = "da verificare"
                step = None
                outcome = None
            else:
                row_state = "running"
                state_word = "in corso"
                step = "sto scaricando"
                outcome = None
        elif state == "succeeded":
            row_state = "done"
            state_word = "completato"
            step = None
            outcome = "completato"
        elif state == "failed":
            row_state = "failed"
            state_word = "non riuscito"
            step = None
            reason = str(data.get("reason") or "").strip()
            outcome = _visible(f"non riuscito: {reason}", "non riuscito") if reason else "non riuscito"
        else:
            continue

        bytes_done = data.get("bytes_done")
        bytes_total = data.get("bytes_total")
        progress: ActivityProgress | None = None
        if (
            isinstance(bytes_total, (int, float))
            and not isinstance(bytes_total, bool)
            and bytes_total > 0
        ):
            done_val = (
                float(bytes_done)
                if isinstance(bytes_done, (int, float)) and not isinstance(bytes_done, bool)
                else 0.0
            )
            progress = ActivityProgress(
                done=round(done_val / (1024 * 1024), 1),
                total=round(float(bytes_total) / (1024 * 1024), 1),
                unit="MB",
            )

        row_id = str(data.get("id") or path.stem)
        rows.append(
            ActivityRow(
                key=f"download:{row_id}",
                kind="download",
                title=title,
                runner="motore locale",
                started_at=data.get("started_at"),
                ended_at=ended,
                state=row_state,
                state_word=state_word,
                progress=progress,
                step=step,
                outcome=outcome,
                links=[],
            )
        )
    rows.sort(key=_sort_key, reverse=True)
    return rows[:MAX_ROWS]


def read(now: datetime | None = None) -> ActivityRead:
    now = now or datetime.now(UTC)
    registry = _registry()
    rows: list[ActivityRow] = []
    groups: list[ActivityGroup] = []
    for source_id, title, reader in SOURCES:
        try:
            rows.extend(reader(now, registry))
            groups.append(ActivityGroup(id=source_id, title=title, state="ok", reason=None))
        except Exception:  # noqa: BLE001 - one unreadable source never fails the whole panel
            reason = "stato del supervisore non leggibile" if source_id == "supervisor" else "lettura non riuscita"
            groups.append(ActivityGroup(id=source_id, title=title, state=UNAVAILABLE, reason=reason))
    rows.sort(key=_sort_key, reverse=True)
    rows = rows[:MAX_ROWS]

    downloads: list[ActivityRow] = []
    try:
        downloads = _download_rows(now)
    except Exception:  # noqa: BLE001
        downloads = []

    return ActivityRead(
        generated_at=_iso(now),
        running=sum(1 for r in rows if r.state == "running"),
        rows=rows,
        groups=groups,
        quota=_quota(),
        downloads=downloads,
        downloads_empty="nessun download registrato",
    )


def _log_path(job: dict[str, Any], root: Path) -> Path | None:
    """The job's log, only when it lives inside the jobs directory."""
    try:
        path = Path(str(job.get("log"))).resolve()
        return path if path.parent == relay_monitor.observe().rc.relay_jobs.jobs_dir(root).resolve() else None
    except (OSError, ValueError):
        return None


def detail(key: str, now: datetime | None = None) -> ActivityDetailRead:
    """The last log lines of one registered job, refused on a secret and with host paths scrubbed."""
    empty = ActivityDetailRead(state="not_found", reason=None, title=None, label=None, lines=[])
    if not key.startswith("job:"):
        return empty
    rj = relay_monitor.observe().rc.relay_jobs
    root = relay_monitor.control_root()
    job_id = key[4:]
    if not rj.ID_RE.fullmatch(job_id) or (job := rj.load(job_id, root)) is None:
        return empty
    lines: list[str] = []
    path = _log_path(job, root)
    if path is not None:
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - LOG_TAIL_BYTES))
                tail = handle.read().decode("utf-8", errors="replace")
            lines = [line[:LOG_LINE_CHARS] for line in tail.splitlines()[-LOG_LINES:]]
        except OSError:
            lines = []
    checked = relay_monitor._checked({"label": str(job.get("label") or ""), "lines": lines}, snapshot_paths=True)
    if checked.get("state") == "refused":
        return ActivityDetailRead(state="refused", reason=str(checked.get("reason")), title=None, label=None, lines=[])
    spec = _spec_row(job.get("spec"), _registry())
    return ActivityDetailRead(state="ok", reason=None, title=_titled("Un lavoro di calcolo registrato", spec),
                              label=checked["label"] or None, lines=checked["lines"])
