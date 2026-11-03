#!/usr/bin/env python3
"""Read-only semantic view of the spec 201 mission owner (spec 205).

One adapter derives a snapshot of what the owner is doing from its authoritative durable state (mission
definitions and stores, `roadmap.json`, `STOP`, the owner lock and the latest Relay turn state). The host-side
`relay_continuity.py watch` loop turns it into an Italian Slack digest and edge-triggered incident alerts; the
`follow` command renders the live turn stream in a terminal. Nothing here takes a lock, writes owner, mission or
Relay state, or calls a provider. GitHub activity is never read as evidence that the owner is alive.

Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import importlib.util
import json
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_RC_PATH = Path(__file__).resolve().parent / "relay_continuity.py"
if "relay_continuity" in sys.modules:
    rc = sys.modules["relay_continuity"]
else:
    _spec = importlib.util.spec_from_file_location("relay_continuity", _RC_PATH)
    assert _spec and _spec.loader
    rc = importlib.util.module_from_spec(_spec)
    sys.modules["relay_continuity"] = rc
    _spec.loader.exec_module(rc)
devctx = rc.devctx

STATES = ("running", "queued", "checkpointed", "verified_complete", "held", "paused", "waiting", "idle", "stopped",
          "unknown")
REPO_URL = "https://github.com/AlbertoRacerro/JarvisOS"
WAIT_ALERT_AFTER = timedelta(hours=6)  # a provider/CI wait becomes an incident only when prolonged
MAX_MESSAGE = 3500

STATE_IT = {
    "running": "in esecuzione",
    "queued": "in coda, in attesa che il proprietario la avvii",
    "checkpointed": "checkpoint salvato; il compito resta aperto e riprende al prossimo turno",
    "verified_complete": "completata e verificata",
    "held": "FERMA in attesa di revisione del maintainer",
    "paused": "roadmap in pausa",
    "waiting": "in attesa (limite provider o conflitto con lavoro attivo)",
    "idle": "nessun lavoro in sospeso (inattività verificata)",
    "stopped": "proprietario delle missioni fermo",
    "unknown": "stato sconosciuto: la telemetria non è leggibile",
}
REASON_IT = {
    "effects_uncertain": "un'operazione interrotta potrebbe aver avuto effetti esterni incerti",
    "auth_failure": "errore di autenticazione o permessi del provider",
    "no_provider_available": "nessun provider disponibile da troppo tempo",
    "wait_exceeded": "attesa oltre il limite",
    "task_blocked": "l'agente ha dichiarato il compito bloccato",
    "task_check_failed": "la verifica indipendente del compito non è soddisfatta",
    "task_report_unverified": "l'esito dichiarato dall'agente non è verificabile",
    "permissions_changed": "i permessi di Relay sono cambiati",
    "state_corrupt": "stato del supervisore illeggibile",
    "coordinator_blocked": "il coordinatore ha dichiarato un blocco (RELAY_STATUS blocked)",
    "job_wait": "in attesa dei job registrati; ripresa automatica al loro termine",
    "jobs_ready": "job conclusi; il coordinatore riprende al prossimo controllo del supervisor",
    "jobs_recoverable": "job conclusi con errore recuperabile dal coordinatore; ripresa automatica",
    "job_observation_uncertain": "stato dei job incerto; conservare le evidenze e non duplicare i calcoli",
    "no_progress": "tre cicli senza progressi sui branch remoti",
    "maintainer_stop": "fermata dal maintainer",
    "cycle_cancelled": "ciclo annullato",
    "repl_active": "una sessione REPL interattiva è attiva sul repository",
    "live_work": "c'è lavoro attivo di un altro processo sul repository",
}


# ---------------------------------------------------------------------------
# Bounded, scrubbed text


_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Any scheme link, `www.` host or bare domain on a common TLD: Slack would render or auto-link them.
_URL_RE = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]{1,15}://\S*|(?:mailto|tel|slack|file):\S+|www\.\S+"
                     r"|(?:[a-z0-9-]+\.)+(?:com|net|org|io|dev|app|ai|co|me|info|biz|xyz|sh|gg|ly|so|site|online|link"
                     r"|cloud|tech|example|eu|us|uk|de|it|fr|ru|cn)\b(?:[/?#]\S*)?)")
_PATH_RE = re.compile(r"(?<![\w.~])(?:~|/(?:home|mnt|root|etc|tmp|run|var|proc))/\S*")
_SLACK_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))


def clean(text: Any, limit: int = 240) -> str | None:
    """Agent-written text made safe for an outbound message: one line, no control characters, links, host paths
    or Slack markup, bounded. None when it is empty or looks like it carries a secret."""
    if not isinstance(text, str):
        return None
    value = re.sub(re.escape(REPO_URL) + r"/(?:issues|pull)/(\d{1,6})\b\S*", r"#\1", _CTRL_RE.sub(" ", text))
    value = _PATH_RE.sub("[path]", _URL_RE.sub("[link]", value))
    for raw, escaped in _SLACK_ESCAPES:  # no `<!channel>`, `<@U…>` or `<url|label>` markup survives
        value = value.replace(raw, escaped)
    value = re.sub(r"\s+", " ", value).strip()
    if not value or devctx.find_secret(value):
        return None
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def links(text: str) -> str:
    """`#123` references in an already cleaned text become links to this repository's issues/PRs."""
    return re.sub(r"(?<![\w/&])#(\d{1,6})\b", lambda m: f"<{REPO_URL}/issues/{m.group(1)}|#{m.group(1)}>", text)


# ---------------------------------------------------------------------------
# Owner liveness without touching its lock


def owner_alive(lock_path: Path, proc_locks: Path = Path("/proc/locks")) -> bool | None:
    """Whether some process holds the owner's flock, read from the kernel lock table (never by locking).
    None when the table is unreadable."""
    try:
        st = lock_path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        table = proc_locks.read_text()
    except OSError:
        return None
    ident = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    for line in table.splitlines():
        parts = line.split()
        if "FLOCK" in parts and "->" not in parts and any(p.lower() == ident for p in parts):
            return True
    return False


# ---------------------------------------------------------------------------
# Snapshot


def _turn_state(repo: Path, session: str | None) -> dict[str, Any]:
    turn = rc.last_turn_dir(repo / ".agent-relay" / "sessions" / session) if session else None
    if turn is None:
        return {}
    try:
        data = json.loads((turn / "state.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"turn": turn.name, "running": True}
    except (OSError, json.JSONDecodeError):
        return {"turn": turn.name}
    out = {"turn": turn.name, "status": data.get("status") if data.get("status") in ("continue", "propose_done",
                                                                                     "blocked") else None,
           "at": data.get("captured_at") if isinstance(data.get("captured_at"), str) else None,
           "reason": clean(data.get("reason")), "next_step": clean(data.get("next_step"))}
    verification = data.get("verification") if isinstance(data.get("verification"), list) else []
    out["verification"] = [v for v in (clean(x, 160) for x in verification[:3]) if v]
    return out


def live_session(repo: Path, state: dict[str, Any] | None) -> str | None:
    """The session of the mission's current attempt: the chain head, or, before the first turn of an attempt is
    recorded, the newest session whose last turn prompt carries the pending attempt's nonce."""
    state = state or {}
    nonce = (state.get("pending") or {}).get("nonce")
    sessions = repo / ".agent-relay" / "sessions"
    if isinstance(nonce, str) and sessions.is_dir():
        for session in sorted((p for p in sessions.iterdir() if p.is_dir()), reverse=True)[:20]:
            if session.name == state.get("head_session"):
                break
            turn = rc.last_turn_dir(session)
            try:
                with open(turn / "prompt.md", encoding="utf-8", errors="replace") as handle:  # type: ignore[operator]
                    if nonce in handle.read(8192):
                        return session.name
            except (OSError, TypeError):
                continue
    return state.get("head_session")


def _task_label(text: Any) -> str | None:
    if isinstance(text, str) and (m := re.match(r"Roadmap continuation cycle (\d+)", text)):
        return f"ciclo {m.group(1)} della roadmap autonoma (sviluppo JarvisOS dalla coda di STATUS.md)"
    first = text.split("\n", 1)[0] if isinstance(text, str) else None
    return clean(first, 200)


def _ledger(repo: Path) -> list[dict[str, Any]]:
    try:
        return devctx.read_ledger(devctx.default_env(repo))
    except Exception:  # noqa: BLE001 - the ledger only enriches the summary
        return []


def _latest(entries: list[dict[str, Any]], kinds: tuple[str, ...], since: str | None) -> str | None:
    for entry in reversed(entries):
        if entry.get("kind") in kinds and (not since or str(entry.get("ts") or "") >= since):
            return clean(entry.get("text"), 220)
    return None


def snapshot(root: Path, repo: Path, now: datetime, *, alive: Callable[[Path], bool | None] = owner_alive,
             ledger: Callable[[Path], list[dict[str, Any]]] = _ledger) -> dict[str, Any]:
    """The owner's current semantic state. Every field derives from durable owner state, never from GitHub."""
    snap: dict[str, Any] = {"at": rc.iso(now), "owner": alive(root / "serve.lock"), "stop": (root / "STOP").exists(),
                            "state": "unknown", "mission": None, "reason": None, "turn": {}, "delivered": [],
                            "problems": []}
    paths = rc.host_ops.Paths(Path.home())
    try:
        stale = any(rc.host_ops.REQUEST_NAME_RE.fullmatch(p.name) and
                    now.timestamp() - p.lstat().st_mtime > 120 for p in paths.requests.iterdir())
    except OSError:
        stale = False
    beat = rc.host_ops._heartbeat(paths)
    at = rc.parse_ts((beat or {}).get("at"))
    if stale and (at is None or now - at > timedelta(minutes=2)):
        snap["problems"].append("hostops_down")
    road = rc.load_roadmap(root)
    if road and not road.get("corrupt"):
        snap["roadmap"] = {"enabled": bool(road.get("enabled")), "cycle": road.get("cycle"),
                           "paused": road.get("paused") if road.get("paused") in rc.ROADMAP_PAUSES else None}
    elif road:
        snap["problems"].append("roadmap_corrupt")
    try:
        missions = rc.load_missions(root)
    except OSError:
        snap["problems"].append("missions_unreadable")
        return snap
    active = None
    for mission, mission_id, _ in missions:
        if mission is None:
            snap["problems"].append("mission_corrupt")
            snap["mission"] = {"id": mission_id}
            return snap
        status, state = rc.mission_status(rc.mission_store(root, mission_id))
        if status == "corrupt":
            snap["problems"].append("mission_corrupt")
            snap["mission"] = {"id": mission_id}
            return snap
        rec = (state or {}).get("mission") or {}
        tasks = mission["tasks"]
        for done in rec.get("done") or []:
            index = done.get("task")
            text = tasks[index].get("text") if isinstance(index, int) and 0 <= index < len(tasks) else None
            snap["delivered"].append({"mission": mission_id, "session": done.get("session"), "at": done.get("at"),
                                      "outcome": rc.task_outcome(done), "label": _task_label(text)})
        if status not in ("complete", "cancelled") and active is None:
            active = (mission, status, state)
    if active is None:
        paused = (snap.get("roadmap") or {}).get("paused")
        last = max(snap["delivered"], key=lambda d: d.get("at") or "", default=None)
        verified = last is not None and last["outcome"] == "verified"
        snap["state"] = ("paused" if paused else "stopped" if snap["stop"] else "unknown" if snap["owner"] is None
                         else "verified_complete" if verified else "idle")
        snap["reason"] = paused or ("maintainer_stop" if snap["stop"] else None)
        if snap["state"] in ("idle", "verified_complete") and snap["owner"] is False and (snap.get("roadmap") or {}).get("enabled"):
            snap["state"] = "stopped"  # roadmap mode is on, but no owner is there to queue its next cycle
            snap["problems"].append("owner_down")
        snap["turn"] = _turn_state(repo, last.get("session")) if last else {}
        if paused == "coordinator_blocked":
            job_ids = road.get("job_wait") if road and road.get("enabled") else None
            if isinstance(job_ids, list) and job_ids and all(
                    isinstance(j, str) and rc.relay_jobs.ID_RE.fullmatch(j) for j in job_ids):
                jobs = []
                for job_id in job_ids:
                    job = rc.relay_jobs.load(job_id, root)
                    jobs.append({"id": job_id, "status": rc.relay_jobs.effective_status(job) if job else "unknown",
                                 "exit_code": job.get("exit_code") if job else None})
                snap["jobs"], snap["wait_kind"] = jobs, "jobs"
                statuses = {j["status"] for j in jobs}
                if statuses <= set(rc.relay_jobs.TERMINAL):
                    reason = "jobs_ready" if statuses == {"succeeded"} else "jobs_recoverable"
                elif statuses - set(rc.relay_jobs.ACTIVE) - set(rc.relay_jobs.TERMINAL):
                    reason = "job_observation_uncertain"
                else:
                    reason = "job_wait"
                snap["state"], snap["reason"] = "waiting", reason
                if snap["stop"] or snap["owner"] is False:
                    snap["state"] = "stopped"
                    if not snap["stop"]:
                        snap["problems"].append("owner_down")
                elif snap["owner"] is None:
                    snap["state"] = "unknown"
            else:
                snap["question"] = _latest(ledger(repo), ("question", "blocker"), last.get("at") if last else None)
        return snap
    mission, status, state = active
    state = state or {}
    rec = state.get("mission") or {}
    index = rec.get("next", 0)
    task = mission["tasks"][index] if index < len(mission["tasks"]) else {}
    snap["mission"] = {"id": mission["id"], "task": f"{min(index + 1, len(mission['tasks']))}/{len(mission['tasks'])}",
                       "label": _task_label(task.get("text")), "authority": mission.get("authority"),
                       "agent": state.get("agent") if state.get("agent") in rc.AGENTS else None}
    snap["turn"] = _turn_state(repo, live_session(repo, state))
    entries = ledger(repo)
    snap["focus"] = _latest(entries, ("claim",), mission.get("submitted_at"))
    snap["question"] = _latest(entries, ("question", "blocker"), mission.get("submitted_at"))
    if status in ("held", "waiting") and snap["owner"] is False and not snap["stop"]:
        snap["problems"].append("owner_down")  # nothing will resume the wait or pick up a release
    if status == "held":
        snap["state"], snap["reason"] = "held", (state.get("hold") or {}).get("reason")
    elif status == "waiting":
        snap["state"], snap["reason"] = "waiting", None
        since = rc.parse_ts(state.get("waiting_since"))
        snap["waiting_since"] = rc.iso(since) if since else None
    elif snap["stop"] or snap["owner"] is False:
        snap["state"] = "stopped"
        snap["reason"] = "maintainer_stop" if snap["stop"] else None
        if not snap["stop"]:
            snap["problems"].append("owner_down")
    elif snap["owner"] is None:
        snap["state"] = "unknown"
    elif state.get("pending"):
        snap["state"] = "running"
    elif state.get("chain"):
        snap["state"] = "checkpointed"
    else:
        snap["state"] = "queued"
    return snap


# ---------------------------------------------------------------------------
# Incidents (edge-triggered) and the digest


def incidents(snap: dict[str, Any], now: datetime) -> dict[str, str]:
    """Actionable incidents now present: stable key -> Italian one-line description."""
    found: dict[str, str] = {}
    mission = (snap.get("mission") or {}).get("id") or "-"
    reason = snap.get("reason")
    if snap["state"] == "held":
        found[f"held|{mission}|{reason}"] = f"missione {mission} ferma: {REASON_IT.get(reason, reason or 'motivo non registrato')}"
    if snap["state"] == "paused":
        found[f"paused|{reason}"] = f"roadmap in pausa: {REASON_IT.get(reason, reason or 'motivo non registrato')}"
    if "owner_down" in snap["problems"]:
        found[f"owner_down|{mission}"] = ("il proprietario delle missioni non è in esecuzione ma "
                                          + (f"la missione {mission} ha lavoro in sospeso" if mission != "-"
                                             else "la roadmap autonoma è attiva"))
    if snap["state"] == "waiting" and snap.get("wait_kind") != "jobs" and (since := rc.parse_ts(snap.get("waiting_since"))) and now - since >= WAIT_ALERT_AFTER:
        found[f"waiting|{mission}|{rc.iso(since)}"] = f"missione {mission} in attesa da più di {int(WAIT_ALERT_AFTER.total_seconds() // 3600)} ore"
    for problem in snap["problems"]:
        if problem != "owner_down":
            found[f"problem|{problem}"] = {"mission_corrupt": f"definizione o stato della missione {mission} illeggibile",
                                           "roadmap_corrupt": "roadmap.json illeggibile",
                                           "hostops_down": "esecutore host non disponibile: richieste in attesa da oltre due minuti",
                                           "missions_unreadable": "cartella delle missioni illeggibile"}.get(problem, problem)
    if snap["state"] == "unknown" and not snap["problems"]:
        found["unknown|owner"] = "impossibile stabilire se il proprietario delle missioni è vivo (telemetria sconosciuta)"
    return found


def _local(ts: str | None) -> str:
    when = rc.parse_ts(ts)
    if when is None:
        return "?"
    try:
        return when.astimezone(ZoneInfo("Europe/Rome")).strftime("%d/%m %H:%M")
    except ZoneInfoNotFoundError:
        return when.strftime("%d/%m %H:%M UTC")


OUTCOME_IT = {"verified": "verificato in modo indipendente", "cycle_settled": "ciclo chiuso (non è prova di consegna)",
              "maintainer_attested": "attestato dal maintainer", "legacy_unverified": "non verificato"}


def digest_text(snap: dict[str, Any], *, since: str | None, merges: list[tuple[str, str]],
                delivery_failures: int = 0) -> str:
    """Italian semantic digest: what, why, delivered, next, problem. Plain language, few lines, repo links only."""
    lines = [f"*JarvisOS — aggiornamento delle {_local(snap['at'])}*"]
    mission = snap.get("mission") or {}
    state = snap["state"]
    head = "in attesa dei job registrati" if state == "waiting" and snap.get("wait_kind") == "jobs" else STATE_IT[state]
    if mission.get("label"):
        who = f" ({mission['agent']})" if mission.get("agent") else ""
        lines.append(f"*In corso:* {head}{who}: {links(mission['label'])} — compito {mission.get('task', '?')}, "
                     f"missione `{mission['id']}`.")
    else:
        lines.append(f"*In corso:* {head}.")
    if snap.get("focus"):
        lines.append(f"  lavoro attuale: {links(snap['focus'])}")
    if snap.get("reason") and state in ("held", "paused", "waiting", "stopped"):
        lines.append(f"  motivo: {REASON_IT.get(snap['reason'], snap['reason'])}")
    if road := snap.get("roadmap"):
        lines.append(f"  perché: roadmap autonoma {'attiva' if road['enabled'] else 'disattivata'}"
                     f" (ciclo {road.get('cycle')}) per far avanzare lo sviluppo di JarvisOS senza supervisione continua.")
    if snap.get("jobs"):
        for job in snap["jobs"]:
            code = "" if job["exit_code"] is None else f" (exit {job['exit_code']})"
            lines.append(f"  job `{job['id']}`: {job['status']}{code}.")
    done = [d for d in snap.get("delivered") or [] if not since or (d.get("at") or "") > since]
    turn = snap.get("turn") or {}
    lines.append("*Fatto dall'ultimo aggiornamento:*")
    if not done and not merges:
        lines.append("  • nessun risultato nuovo registrato.")
    for d in done[-4:]:
        lines.append(f"  • {links(d.get('label') or 'compito')} — {OUTCOME_IT.get(d['outcome'], d['outcome'])} ({_local(d.get('at'))})")
    for pr, title in merges[-5:]:
        lines.append(f"  • unito su master: <{REPO_URL}/pull/{pr}|#{pr}> {title}")
    if turn.get("reason") and turn.get("status") in ("continue", "propose_done", "blocked"):
        lines.append(f"  ultimo turno ({turn['status']}): {links(turn['reason'])}")
        for v in turn.get("verification") or []:
            lines.append(f"    – {links(v)}")
    nxt = turn.get("next_step") if turn.get("next_step") != turn.get("reason") else None
    if snap.get("wait_kind") == "jobs":
        nxt = ("il coordinatore legge risultati e diagnosi al prossimo controllo del supervisor"
               if snap["reason"] in ("jobs_ready", "jobs_recoverable")
               else "attendere evidenze verificabili dei job senza rilanciare le simulazioni")
    if state == "running":
        nxt = nxt or "il turno in corso decide il prossimo passo"
    lines.append(f"*Prossimo:* {links(nxt) if nxt else 'nessun passo dichiarato'}")
    asks = list(incidents(snap, rc.parse_ts(snap["at"]) or rc.now_utc()).values())
    if delivery_failures:
        asks.append(f"{delivery_failures} invii Slack falliti dall'ultimo aggiornamento")
    lines.append("*Serve intervento:* " + ("; ".join(asks) if asks else "no."))
    text = "\n".join(lines)
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"


def alert_text(description: str, snap: dict[str, Any]) -> str:
    text = f":red_circle: *JarvisOS — serve intervento* ({_local(snap['at'])})\n{description}."
    road = snap.get("roadmap") or {}
    if snap.get("question"):
        text += f"\nRichiesta registrata: {links(snap['question'])}"
    if snap["state"] == "held":
        text += "\nRivedere le prove del turno e poi `relay_continuity.py release`."
    elif snap["state"] == "paused" and road.get("paused") == "coordinator_blocked" and not snap.get("question"):
        text += "\nLa domanda è registrata nel ledger (`devctx.py recover`)."
    elif "owner_down" in snap["problems"]:
        text += "\nRiavviare il servizio del proprietario (systemd --user) dopo aver controllato il suo log."
    return text


def merges_since(repo: Path, since: str | None, run: Callable[..., tuple[int, str]] = devctx.run) -> list[tuple[str, str]]:
    """PRs merged to origin/master since `since`, from the local remote-tracking ref (the watcher fetches it)."""
    if not since:
        return []
    code, out = run(["git", "log", "--first-parent", f"--since={since}", "--format=%s%x1f%b%x1e", "origin/master"],
                    cwd=repo, timeout=30)
    found = []
    for record in out.split("\x1e") if code == 0 else []:
        subject, _, body = record.strip().partition("\x1f")
        if m := re.match(r"Merge pull request #(\d+)", subject):
            found.append((m.group(1), clean(body.strip().split("\n", 1)[0], 120) or ""))
    return list(reversed(found))


# ---------------------------------------------------------------------------
# Watch-loop integration: one observation step


def observe_step(obs: dict[str, Any], snap: dict[str, Any], now: datetime, *, send: Callable[[str, str], bool],
                 digest_every: timedelta, merges: Callable[[str | None], list[tuple[str, str]]],
                 failures: int = 0) -> None:
    """Send alerts for incidents that appeared since the last step, and the digest when due. `obs` is the
    watcher's durable record (`active` incidents, `last_digest`, `failures_reported`); an incident is announced
    once until it clears, also across watcher restarts. `send(kind, text)` must never raise."""
    active = obs.setdefault("active", {})
    onsets = obs.setdefault("onset", {})
    current = incidents(snap, now)
    for key, description in current.items():
        if key not in active:
            # The onset makes each occurrence a distinct notifier key, so a recurrence is not swallowed by the
            # notifier's time-based dedupe; resends of one undelivered occurrence still share it.
            onset = onsets.setdefault(key, rc.iso(now))
            if send(f"alert|{key}|{onset}", alert_text(description, snap)):
                active[key] = rc.iso(now)
    for key in [k for k in active if k not in current]:
        del active[key]
    for key in [k for k in onsets if k not in current]:
        del onsets[key]
    last = rc.parse_ts(obs.get("last_digest"))
    if last is None or now - last >= digest_every:
        failed = max(0, failures - int(obs.get("failures_reported") or 0))
        since = obs.get("last_digest") or rc.iso(now - digest_every)  # a first digest covers one interval
        text = digest_text(snap, since=since, merges=merges(since),
                           delivery_failures=failed)
        if send(f"digest|{rc.iso(now)}", text):
            obs["last_digest"] = rc.iso(now)
            obs["failures_reported"] = failures


# ---------------------------------------------------------------------------
# Terminal monitor


class Tail:
    """Incremental JSONL reader: whole lines only, so a torn final line is read once it is complete."""

    def __init__(self, path: Path, offset: int = 0):
        self.path = path
        self.offset = offset
        self.buffer = b""

    def read(self) -> list[dict[str, Any]]:
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read(1 << 20)
        except OSError:
            return []
        self.offset += len(chunk)
        self.buffer += chunk
        *complete, self.buffer = self.buffer.split(b"\n")
        events = []
        for raw in complete:
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                events.append(event)
        return events


_WEBHOOK_RE = re.compile(r"https://hooks\.slack\.com/\S+")
_BROWSER_PATH_RE = re.compile(
    r"(?<![\w./\\])(?:[A-Za-z]:[\\/]|\\\\[\w.-]+\\[\w.-]+\\|~?/(?!/))(?:[^\s\"'<>])*"
)
DETAIL_LIMIT = 2000


def _redact(text: Any, limit: int, *, paths: bool = False, lines: bool = False) -> str:
    """Secrets (and, for the browser, host paths beyond their last component) removed, bounded. `lines` keeps
    line breaks for an on-demand detail; otherwise the value is one line."""
    value = _CTRL_RE.sub(" ", str(text or "")).strip()
    for pattern in (*devctx.SECRET_PATTERNS, _WEBHOOK_RE):
        value = pattern.sub("[redacted]", value)
    if paths:
        value = _BROWSER_PATH_RE.sub(
            lambda m: "…/" + re.split(r"[\\/]", m.group(0).rstrip("/\\"))[-1], value
        )
    value = re.sub(r"[ \t]+", " ", value) if lines else re.sub(r"\s+", " ", value)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def event_items(event: dict[str, Any], *, paths: bool = False) -> list[dict[str, Any]]:
    """One Claude, Codex or native Argon event as timeline items: what the stream states, never reasoning.
    Each item has a kind, a terminal mark, a bounded one-line text and, when that line was shortened, a bounded
    multi-line detail. `paths` scrubs host paths (browser)."""
    def item(kind: str, mark: str, raw: Any, limit: int, *, wrap: str = "{}", failed: bool = False) -> dict[str, Any]:
        text = wrap.format(_redact(raw, limit, paths=paths))
        out: dict[str, Any] = {"kind": kind, "mark": mark, "text": text}
        full = _redact(raw, DETAIL_LIMIT, paths=paths, lines=True)
        if len(re.sub(r"\s+", " ", full)) > limit or "\n" in full.strip():
            out["detail"] = full
        if failed:
            out["failed"] = True
        return out

    native = event.get("event")
    if native == "step_update":
        step = event.get("step_update")
        if not isinstance(step, dict) or step.get("step_type") != "tool":
            return []
        info = step.get("tool_info")
        info = info if isinstance(info, dict) else {}
        params = info.get("parameters")
        params = params if isinstance(params, dict) else {}
        name = _redact(step.get("tool_name"), 60, paths=paths)
        if step.get("state") == "ACTIVE":
            detail = params.get("CommandLine") or params.get("TargetFile") or params.get("AbsolutePath") or ""
            return [item("tool", "⏺", detail, 200, wrap=name.replace("{", "{{").replace("}", "}}") + "({})")]
        if step.get("state") in ("DONE", "ERROR", "CANCELLED") and info.get("output"):
            failed = step.get("state") != "DONE" or bool(step.get("error") or info.get("error"))
            exit_match = re.match(r"Command exited with code (-?\d+)\.", str(info["output"]))
            failed = failed or bool(exit_match and int(exit_match[1]) != 0)
            return [item("output", "✗" if failed else "⎿", info["output"], 240, failed=failed)]
        return []
    if native == "result":
        result = event.get("result")
        if not isinstance(result, dict):
            return [item("error", "✗", "Risultato Argon non leggibile", 240)]
        response = result.get("response")
        failed = (result.get("status") != "SUCCESS" or bool(result.get("denied_actions"))
                  or not isinstance(response, str) or not response.strip())
        entries = [item("message", "●", response, 600)] if isinstance(response, str) and response.strip() else []
        entries.append({"kind": "done", "mark": "✓", "text": "Argon: " + _redact(result.get("status"), 60, paths=paths),
                        **({"failed": True} if failed else {})})
        return entries
    kind = event.get("type")
    out: list[dict[str, Any]] = []
    if kind == "assistant" and not event.get("parent_tool_use_id"):
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "text":
                out.append(item("message", "●", block.get("text"), 600))
            elif block.get("type") == "tool_use":
                args = block.get("input") or {}
                detail = args.get("command") or args.get("file_path") or args.get("pattern") or args.get("description") or ""
                name = _redact(block.get("name"), 60, paths=paths)
                out.append(item("tool", "⏺", detail, 200, wrap=name.replace("{", "{{").replace("}", "}}") + "({})"))
    elif kind == "user":
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
                failed = bool(block.get("is_error"))
                out.append(item("output", "✗" if failed else "⎿", content, 240, failed=failed))
    elif kind == "result":
        out.append({"kind": "done", "mark": "✓", "text": _redact(event.get("subtype") or "result", 60, paths=paths)}
                   | ({"failed": True} if event.get("is_error") else {}))
    elif kind in ("item.started", "item.completed"):
        entry = event.get("item") or {}
        if entry.get("type") == "command_execution":
            if kind == "item.started":
                out.append(item("tool", "⏺", entry.get("command"), 200, wrap="Bash({})"))
            else:
                failed = entry.get("exit_code") not in (0, None)
                out.append(item("output", "✗" if failed else "⎿", entry.get("aggregated_output"), 240, failed=failed))
        elif entry.get("type") == "agent_message" and kind == "item.completed":
            out.append(item("message", "●", entry.get("text"), 600))
        elif entry.get("type") == "file_change" and kind == "item.completed":
            changed = ", ".join(_redact(c.get("path"), 80, paths=paths) for c in entry.get("changes") or []
                                if isinstance(c, dict))
            out.append({"kind": "edit", "mark": "⏺", "text": f"Edit({changed})"})
    elif kind == "turn.failed" or kind == "error":
        out.append(item("error", "✗", (event.get("error") or {}).get("message") or event.get("message"), 240))
    return out


def render_event(event: dict[str, Any]) -> list[str]:
    """Terminal lines for one provider event. Only what the stream states; no reasoning."""
    lines = []
    for entry in event_items(event):
        if entry["kind"] == "done":
            lines.append(f"✓ turno terminato: {entry['text']}" + (" (errore)" if entry.get("failed") else ""))
        elif entry["kind"] == "error":
            lines.append(f"✗ errore: {entry['text']}")
        else:
            lines.append(("  " if entry["kind"] == "output" else "") + f"{entry['mark']} {entry['text']}")
    return lines


def _current_mission(root: Path) -> tuple[str | None, dict[str, Any] | None]:
    """(id, state) of the mission the owner is executing, else of the latest valid one."""
    chosen: tuple[str | None, dict[str, Any] | None] = (None, None)
    for mission, mission_id, _ in rc.load_missions(root):
        status, state = rc.mission_status(rc.mission_store(root, mission_id))
        if mission is None or status == "corrupt":
            continue
        chosen = (mission_id, state)
        if status not in ("complete", "cancelled"):
            break
    return chosen


def active_turn(root: Path, repo: Path) -> tuple[str | None, Path | None, dict[str, Any] | None]:
    """(mission id, latest turn dir, mission state) of the mission the owner is executing, else the latest one."""
    mission_id, state = _current_mission(root)
    if mission_id is None:
        return None, None, None
    session = live_session(repo, state)
    turn = rc.last_turn_dir(repo / ".agent-relay" / "sessions" / session) if session else None
    return mission_id, turn, state


# ---------------------------------------------------------------------------
# Timeline pages (spec 206): the events `follow` shows, as bounded items after an opaque cursor


_SESSION_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
_TURN_RE = re.compile(r"turn-(\d{1,6})")
_MISSION_RE = re.compile(r"m-[0-9A-Za-z-]{1,60}")
PAGE_ITEMS = 200
PAGE_CHARS = 120_000  # item text and detail per page
PAGE_READ = 1 << 20  # raw JSONL bytes read per page


class CursorInvalid(ValueError):
    pass


def encode_cursor(mission: str, session: str | None, turn: str | None, offset: int, item: int = 0) -> str:
    raw = json.dumps([mission, session, turn, offset, item], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(text: str) -> tuple[str, str | None, str | None, int, int]:
    """A cursor names only a mission id, a session id, a turn name and a byte offset, each strictly validated:
    it can never address a file outside the repository's relay session tree."""
    try:
        decoded = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        mission, session, turn, offset, item = (*decoded, 0) if len(decoded) == 4 else decoded
    except (ValueError, TypeError, binascii.Error) as exc:
        raise CursorInvalid("cursor_invalid") from exc
    valid = (isinstance(mission, str) and _MISSION_RE.fullmatch(mission)
             and (session is None and turn is None and offset == 0
                  or isinstance(session, str) and _SESSION_RE.fullmatch(session)
                  and isinstance(turn, str) and _TURN_RE.fullmatch(turn))
             and type(offset) is int and 0 <= offset < 1 << 40
             and type(item) is int and 0 <= item < 1 << 20)
    if not valid:
        raise CursorInvalid("cursor_invalid")
    return mission, session, turn, offset, item


def mission_turns(repo: Path, state: dict[str, Any] | None) -> list[tuple[str, str]]:
    """(session, turn) of a mission in execution order: its session chain, then the live session if newer."""
    sessions = [*((state or {}).get("chain") or []), live_session(repo, state)]
    out: list[tuple[str, str]] = []
    for session in dict.fromkeys(s for s in sessions if isinstance(s, str) and _SESSION_RE.fullmatch(s)):
        try:
            names = [p.name for p in (repo / ".agent-relay" / "sessions" / session / "turns").iterdir() if p.is_dir()]
        except OSError:
            continue
        found = sorted((int(m.group(1)), name) for name in names if (m := _TURN_RE.fullmatch(name)))
        out.extend((session, name) for _, name in found)
    return out


def _mission_state(root: Path, mission_id: str) -> dict[str, Any] | None:
    status, state = rc.mission_status(rc.mission_store(root, mission_id))
    return None if status == "corrupt" else state


def _marker(kind: str, text: str, session: str | None = None, turn: str | None = None) -> dict[str, Any]:
    return {"kind": kind, "mark": "──", "text": text, "session": session, "turn": turn, "offset": 0}


def _mission_marker(mission_id: str, state: dict[str, Any] | None) -> dict[str, Any]:
    state = state or {}
    rec = state.get("mission") or {}
    nxt, total = rec.get("next"), rec.get("total")
    task = f"{min(nxt + 1, total)}/{total}" if isinstance(nxt, int) and isinstance(total, int) else "?"
    agent = state.get("agent") if state.get("agent") in rc.AGENTS else "?"
    return _marker("mission", f"{mission_id} · task {task} · {clean(state.get('phase'), 40) or '?'} · {agent}")


def timeline_page(root: Path, repo: Path, cursor: str | None = None, *, start: str = "turn",
                  limit: int = PAGE_ITEMS) -> dict[str, Any]:
    """The next bounded page of the owner's timeline after `cursor` (or from the start of the live turn, or of
    the live mission when `start` is "mission"), and the cursor to resume from. Whole JSONL lines only, so a torn
    final line is returned once it is complete. A handoff to the next turn or session, or the start of the next
    mission, appears as a marker in order. Nothing is locked or written."""
    limit = max(1, min(limit, PAGE_ITEMS))
    current, current_state = _current_mission(root)
    items: list[dict[str, Any]] = []
    if cursor is None:
        if current is None:
            return {"mission": None, "items": [], "cursor": None, "more": False}
        turns = mission_turns(repo, current_state)
        mission_id, state = current, current_state
        items.append(_mission_marker(current, current_state))
        if not turns:
            return {"mission": current, "items": items, "cursor": encode_cursor(current, None, None, 0), "more": False}
        session, turn = turns[0] if start == "mission" else turns[-1]
        offset, item_index = 0, 0
        items.append(_marker("turn", f"{session}/{turn}", session, turn))
    else:
        mission_id, session, turn, offset, item_index = decode_cursor(cursor)
        state = current_state if mission_id == current else _mission_state(root, mission_id)
        turns = mission_turns(repo, state)
        if session is not None and (session, turn) not in turns:
            # the cursor's turn is gone: resume at the live turn and say so rather than guess
            items.append(_marker("gap", "the earlier position is no longer available"))
            mission_id, state, turns = current or mission_id, current_state, mission_turns(repo, current_state)
            session, turn, offset = (*turns[-1], 0) if turns else (None, None, 0)
            item_index = 0
            if session:
                items.append(_marker("turn", f"{session}/{turn}", session, turn))
    chars = sum(len(i["text"]) for i in items)
    more = False
    while True:
        if session is None:
            if not turns:
                break
            session, turn, offset = (*turns[0], 0)
            item_index = 0
            items.append(_marker("turn", f"{session}/{turn}", session, turn))
            continue
        path = repo / ".agent-relay" / "sessions" / session / "turns" / str(turn) / "output.jsonl"
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                chunk = handle.read(PAGE_READ)
        except OSError:
            chunk = b""
        complete = chunk[: chunk.rfind(b"\n") + 1]
        if not complete and len(chunk) == PAGE_READ:  # one event beyond the read bound: skip it, say so
            items.append({**_marker("gap", "an oversized event was skipped", session, turn), "offset": offset})
            offset += len(chunk)  # the rest of that line fails to parse and is skipped like a corrupt line
            more = True
            break
        for raw in complete.splitlines(keepends=True):
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                event = None
            found = [{**i, "session": session, "turn": turn, "offset": offset}
                     for i in (event_items(event, paths=True) if isinstance(event, dict) else [])]
            if item_index > len(found):
                raise CursorInvalid("cursor_invalid")
            for index in range(item_index, len(found)):
                entry = found[index]
                size = len(entry["text"]) + len(entry.get("detail", ""))
                if len(items) >= limit or chars + size > PAGE_CHARS:
                    item_index = index
                    more = True
                    break
                items.append(entry)
                chars += size
            if more:
                break
            offset += len(raw)
            item_index = 0
        if more or len(chunk) == PAGE_READ:
            more = True
            break
        position = turns.index((session, turn)) if (session, turn) in turns else len(turns)
        if position + 1 < len(turns):
            session, turn = turns[position + 1]
            offset = 0
            item_index = 0
            items.append(_marker("turn", f"{session}/{turn}", session, turn))
        elif mission_id != current and current is not None:
            mission_id, state, turns = current, current_state, mission_turns(repo, current_state)
            session, turn, offset = None, None, 0
            item_index = 0
            items.append(_mission_marker(current, current_state))
        else:
            break
        if len(items) >= limit:
            more = True
            break
    return {"mission": mission_id, "items": items, "cursor": encode_cursor(mission_id, session, turn, offset, item_index),
            "more": more}


def follow(root: Path, repo: Path, *, write: Callable[[str], None], poll: float = 1.0,
           stop: Callable[[], bool] = lambda: False, replay: bool = True) -> None:
    """Follow the owner's live turn. A new turn or session (handoff, provider switch, next task) is announced and
    followed; nothing is locked or written."""
    current: Path | None = None
    tail: Tail | None = None
    last_state = None
    while not stop():
        mission_id, turn, state = active_turn(root, repo)
        snap_state = ((state or {}).get("phase"), (state or {}).get("agent"), ((state or {}).get("mission") or {}).get("next"))
        if snap_state != last_state and state is not None:
            phase, agent, nxt = snap_state
            write(f"── missione {mission_id} · compito {(nxt or 0) + 1}/{((state or {}).get('mission') or {}).get('total', '?')}"
                  f" · {phase} · {agent or '?'}")
            last_state = snap_state
        if turn is not None and turn != current:
            current = turn
            write(f"── {turn.parent.parent.name}/{turn.name}")
            path = turn / "output.jsonl"
            tail = Tail(path, 0 if replay else (path.stat().st_size if path.exists() else 0))
            replay = True  # later turns are always shown from their start
        for event in tail.read() if tail else []:
            for line in render_event(event):
                write(line)
        time.sleep(poll)


# ---------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay_observe.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status", help="print the semantic snapshot as JSON")
    sub.add_parser("digest", help="print the Italian digest that the watcher would send now (nothing is sent)")
    p = sub.add_parser("follow", help="follow the owner's live turn in this terminal (read-only; Ctrl-C closes)")
    p.add_argument("--poll", type=float, default=1.0)
    p.add_argument("--no-replay", action="store_true", help="start at the end of the current turn")
    args = parser.parse_args(argv)
    root, repo, now = rc.control_root(), rc.repo_root(), rc.now_utc()
    if args.action == "status":
        snap = snapshot(root, repo, now)
        print(json.dumps({**snap, "incidents": incidents(snap, now)}, indent=1, ensure_ascii=False))
    elif args.action == "digest":
        since = rc.iso(now - timedelta(hours=2))
        print(digest_text(snapshot(root, repo, now), since=since, merges=merges_since(repo, since)))
    else:
        try:
            follow(root, repo, write=lambda line: print(line, flush=True), poll=args.poll, replay=not args.no_replay)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
