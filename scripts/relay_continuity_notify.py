"""Outbound notification for relay_continuity (spec 265)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

if "relay_continuity_common" not in sys.modules:
    _SPEC = importlib.util.spec_from_file_location(
        "relay_continuity_common", Path(__file__).resolve().parent / "relay_continuity_common.py"
    )
    assert _SPEC and _SPEC.loader
    _MOD = importlib.util.module_from_spec(_SPEC)
    sys.modules["relay_continuity_common"] = _MOD
    _SPEC.loader.exec_module(_MOD)
from relay_continuity_common import (  # noqa: E402
    AGENTS,
    HOLD_REASONS,
    MISSION_ID_RE,
    OUTCOMES,
    PHASES,
    ROADMAP_PAUSES,
    SLACK_BOT_FILE,
    STOP_REASONS,
    WEBHOOK_FILE,
    devctx,
    iso,
    parse_ts,
)

# ---------------------------------------------------------------------------
# Outbound notification (Phase 2): fixed templates, allowlisted structured fields only

ISO_RE = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
FIELD_RULES: dict[str, Callable[[str], bool]] = {
    "session": lambda v: v == "none" or bool(re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", v)),
    "agent": lambda v: v in AGENTS,
    "from_agent": lambda v: v in AGENTS,
    "outcome": lambda v: v in OUTCOMES,
    "action": lambda v: v in ("switch", "retry", "wait", "hold", "idle", "notify"),
    "reason": lambda v: v in HOLD_REASONS + STOP_REASONS + ROADMAP_PAUSES,
    "reset_at": lambda v: v in ("none", "manual") or bool(re.fullmatch(ISO_RE, v)),
    "next_check_at": lambda v: v == "none" or bool(re.fullmatch(ISO_RE, v)),
    "claude_until": lambda v: v in ("none", "manual") or bool(re.fullmatch(ISO_RE, v)),
    "codex_until": lambda v: v in ("none", "manual") or bool(re.fullmatch(ISO_RE, v)),
    "argon_until": lambda v: v in ("none", "manual") or bool(re.fullmatch(ISO_RE, v)),
    "reset_source": lambda v: v in ("limits", "message", "estimate", "manual", "auth"),
    "attempt": lambda v: bool(re.fullmatch(r"\d{1,4}", v)),
    "attempts": lambda v: bool(re.fullmatch(r"\d{1,6}", v)),
    "holds": lambda v: bool(re.fullmatch(r"\d{1,6}", v)),
    "count": lambda v: bool(re.fullmatch(r"\d{1,3}", v)),
    "sha": lambda v: bool(re.fullmatch(r"[0-9a-f]{7,40}", v)),
    "pr": lambda v: v == "none" or bool(re.fullmatch(r"\d{1,6}", v)),
    "phase": lambda v: v in PHASES,
    "mission": lambda v: bool(MISSION_ID_RE.fullmatch(v)),
    "task": lambda v: bool(re.fullmatch(r"\d{1,2}/\d{1,2}", v)),
    "owner": lambda v: v == "control-plane",
}
OPTIONAL_FIELDS = ("reset_at", "next_check_at", "claude_until", "codex_until", "argon_until", "pr")
EVENTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "supervisor_started": ("JarvisOS ha ripreso il lavoro.", ("session", "agent")),
    "supervisor_stopped": ("JarvisOS ha interrotto il lavoro.", ("session", "reason", "phase")),
    "provider_switched": ("JarvisOS ha cambiato assistente e continua il lavoro.", ("session", "from_agent", "agent", "outcome")),
    "provider_exhausted": ("Un assistente non è disponibile; JarvisOS prova a proseguire.", ("agent", "outcome", "reset_at", "reset_source")),
    "all_exhausted": ("Gli assistenti non sono disponibili; JarvisOS riprenderà quando possibile.", ("next_check_at",)),
    "turn_failed": ("Alcuni tentativi di lavoro di seguito non sono riusciti; JarvisOS riprova.", ("session", "agent", "outcome", "action")),
    "effects_parked": ("JarvisOS ha messo da parte un'operazione non verificabile e continua con il resto del lavoro.",
                       ("session", "agent", "count")),
    "hold": ("JarvisOS ha messo in pausa il lavoro e attende una decisione in Jarvis.", ("session", "reason", "attempt")),
    "circuit_open": ("JarvisOS ha sospeso i tentativi e attende una decisione in Jarvis.", ("session", "reason")),
    "turn_completed": ("JarvisOS ha completato un passaggio di lavoro.", ("session", "agent")),
    "master_advanced": ("Una modifica è stata integrata in JarvisOS.", ("sha", "pr")),
    "repl_turn_failed": ("Un tentativo interattivo non è riuscito; controlla la chat.", ("session", "agent", "outcome", "reset_at")),
    "repl_claude_dont_ask": ("La sessione interattiva potrebbe non poter eseguire comandi: controlla la chat.", ("session",)),
    "test": ("Prova delle notifiche di JarvisOS.", ()),
    "task_completed": ("JarvisOS ha completato una parte del lavoro richiesto.", ("mission", "task", "agent", "session")),
    "mission_completed": ("JarvisOS ha completato il lavoro richiesto.", ("mission", "task", "session")),
    "mission_waiting": ("JarvisOS è in attesa di poter proseguire.",
                        ("owner", "mission", "task", "agent", "reason", "next_check_at")),
    "mission_held": ("Il lavoro richiesto è in pausa e attende una decisione in Jarvis.",
                     ("owner", "mission", "task", "agent", "session", "reason")),
    "roadmap_paused": ("Il lavoro programmato è in pausa e attende una decisione in Jarvis.", ("owner", "reason")),
}


def render(event: str, fields: dict[str, Any]) -> str | None:
    """Fixed template text, or None when any field is missing, unexpected or fails its validator."""
    if event not in EVENTS:
        return None
    title, allowed = EVENTS[event]
    if set(fields) - set(allowed):
        return None
    for key in allowed:
        value = fields.get(key)
        if value is None and key in OPTIONAL_FIELDS:
            value = "none"
        if value is None:
            return None
        value = str(value)
        if not FIELD_RULES[key](value):
            return None
    text = title
    return None if devctx.find_secret(text) else text


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201 - stdlib signature
        raise urllib.error.HTTPError(req.full_url, code, "redirect rejected", headers, fp)


# Proxies disabled and redirects rejected: the webhook body only ever reaches hooks.slack.com directly.
_WEBHOOK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())
_SLACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())


def load_slack_bot(path: Path = SLACK_BOT_FILE) -> dict[str, str] | None:
    """A host-only credential file; reject symlinks and any permissions beyond 0600."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, encoding="utf-8") as handle:
            st = os.fstat(handle.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o777 != 0o600:
                return None
            raw = handle.read(8193) if st.st_size <= 8192 else ""
            data = json.loads(raw) if len(raw) <= 8192 and raw else None
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k] for k in
                                               ("bot_token", "channel_id", "maintainer_user_id")):
        return None
    return {k: data[k] for k in ("bot_token", "channel_id", "maintainer_user_id")}


def slack_api(method: str, config: dict[str, str], params: dict[str, Any], *, opener: Any = _SLACK_OPENER,
              timeout: float = 10.0, base_url: str = "https://slack.com/api/") -> dict[str, Any] | None:
    """Only fixed Slack Web API methods; no response or exception containing credentials escapes."""
    if (method not in ("chat.postMessage", "conversations.history", "conversations.replies")
            or base_url != "https://slack.com/api/"):
        return None
    url = f"{base_url}{method}"
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or parts.hostname != "slack.com" or parts.port is not None
            or parts.username or parts.password or not parts.path.startswith("/api/")
            or parts.query or parts.fragment):
        return None
    request = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {config['bot_token']}",
                                              "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener.open(request, timeout=timeout) as response:
            data = json.loads(response.read(1 << 20))
        return data if isinstance(data, dict) and data.get("ok") is True else None
    except Exception:  # noqa: BLE001 - credentials and Slack responses must never escape to logs
        return None


def slack_post(config: dict[str, str], text: str, *, thread_ts: str | None = None,
               api: Callable[..., dict[str, Any] | None] = slack_api) -> str | None:
    params = {"channel": config["channel_id"], "text": text}
    if thread_ts:
        params["thread_ts"] = thread_ts
    result = api("chat.postMessage", config, params)
    ts = result.get("ts") if result else None
    return ts if isinstance(ts, str) and ts else None


def mention_urgent(kind: str, text: str, bot: dict[str, str] | None) -> str:
    if bot and kind.startswith(("question|", "alert|")):
        return f"<@{bot['maintainer_user_id']}> {text}"
    return text


def poll_slack_questions(obs: dict[str, Any], config: dict[str, str], root: Path, repo: Path, observe: Any,
                         now: datetime, *, api: Callable[..., dict[str, Any] | None] = slack_api,
                         post: Callable[..., str | None] = slack_post) -> None:
    """Poll sent question threads and channel messages, with durable per-stream cursors."""
    last = parse_ts(obs.get("last_slack_poll"))
    if last is not None and now - last < timedelta(seconds=30):
        return
    obs["last_slack_poll"] = iso(now)
    sent = {qid: ts for qid, ts in obs.get("sent_questions", {}).items()
            if isinstance(ts, str) and re.fullmatch(r"\d+\.\d+", ts)}
    open_ids = {q["id"] for q in observe.parse_open_questions(devctx.read_ledger(devctx.default_env(repo)))}
    start_ts = obs.setdefault("slack_started_ts", f"{now.timestamp():.6f}")
    cursors = obs.setdefault("slack_cursors", {})
    pending = obs.setdefault("slack_pending_confirmations", [])
    confirmation = "Ricevuto, l'ho registrato come tua risposta."
    remaining = []
    for item in pending:
        first_failed = parse_ts(item["first_failed_at"])
        if (item["attempts"] >= 5 or first_failed is None or now - first_failed >= timedelta(hours=24)):
            continue
        if post(config, confirmation, thread_ts=item["thread_ts"], api=api) is None:
            item["attempts"] += 1
            if item["attempts"] < 5:
                remaining.append(item)
    pending[:] = remaining

    def handle(message: dict[str, Any], root_ts: str | None) -> bool:
        ts = message.get("ts")
        if not isinstance(ts, str):
            return True
        if (message.get("channel", config["channel_id"]) != config["channel_id"]
                or message.get("user") != config["maintainer_user_id"] or message.get("bot_id")
                or message.get("subtype")):
            return True
        answer = message.get("text")
        if not isinstance(answer, str) or not 1 <= len(answer) <= 1000 or devctx.find_secret(answer):
            return True
        answer = answer.strip()
        if not answer:
            return True
        questions = observe.parse_open_questions(devctx.read_ledger(devctx.default_env(repo)))
        if root_ts:
            qid = next((qid for qid, posted in sent.items() if posted == root_ts
                        and any(q["id"] == qid for q in questions)), None)
            if qid is None:
                return True
        elif len(questions) == 1:
            qid = questions[0]["id"]
            posted = sent.get(qid)
            if posted is None or re.fullmatch(r"\d+\.\d+", ts) is None or Decimal(ts) <= Decimal(posted):
                return True
        else:
            reply = ("Rispondi nel thread della domanda." if questions else "Non ci sono domande aperte.")
            return post(config, reply, thread_ts=ts, api=api) is not None
        result = observe.record_answer(root, repo, qid, answer, source="Slack", author="maintainer (slack)")
        if result["state"] != "ok":
            return result.get("reason") == "question_not_open"
        reply_ts = root_ts or ts
        if post(config, confirmation, thread_ts=reply_ts, api=api) is None:
            pending.append({"qid": qid, "thread_ts": reply_ts, "attempts": 1, "first_failed_at": iso(now)})
            del pending[:-100]
        return True

    streams = [("history", None, min(sent.values(), default=start_ts))] + [
        (f"thread:{ts}", ts, ts) for qid, ts in sent.items() if qid in open_ids]  # answered threads: no more calls
    for key, thread_ts, initial in streams:
        cursor = cursors.get(key, initial)
        params = {"channel": config["channel_id"], "oldest": cursor, "limit": 100}
        if thread_ts:
            params["ts"] = thread_ts
        messages: list[dict[str, Any]] = []
        page_cursors: set[str] = set()
        for _ in range(100):
            result = api("conversations.replies" if thread_ts else "conversations.history", config, params)
            if result is None or not isinstance(result.get("messages"), list):
                messages = []
                break
            messages.extend(m for m in result["messages"] if isinstance(m, dict))
            next_page = (result.get("response_metadata") or {}).get("next_cursor")
            if not result.get("has_more") and not next_page:
                break
            if not isinstance(next_page, str) or not next_page or next_page in page_cursors:
                messages = []
                break
            page_cursors.add(next_page)
            params["cursor"] = next_page
        else:
            messages = []  # leave the durable cursor unchanged if Slack has more than the bounded batch
        if not messages:
            continue
        for message in sorted(messages, key=lambda m: m.get("ts", "")):
            if not isinstance(message, dict) or not isinstance(message.get("ts"), str) or message["ts"] <= cursor:
                continue
            # History observes thread roots only; replies are handled by their own stream.
            if not thread_ts and message.get("thread_ts") and message["thread_ts"] != message["ts"]:
                cursors[key] = message["ts"]
                continue
            if not handle(message, thread_ts):
                break
            cursors[key] = message["ts"]


def _valid_webhook_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname == "hooks.slack.com" and port is None
            and parts.username is None and parts.password is None and parts.path.startswith("/services/")
            and not parts.query and not parts.fragment)


def post_webhook(text: str, *, webhook_file: Path = WEBHOOK_FILE, timeout: float = 10.0) -> str | None:
    """POST to the incoming webhook. Returns None on success, else an error class (never the URL)."""
    try:
        url = webhook_file.read_text().strip()
    except OSError:
        return "webhook_missing"
    if not _valid_webhook_url(url):
        return "webhook_invalid"
    request = urllib.request.Request(url, data=json.dumps({"text": text}).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with _WEBHOOK_OPENER.open(request, timeout=timeout) as response:
            response.read(256)
        return None
    except urllib.error.HTTPError as exc:
        return f"http_{exc.code}"
    except Exception as exc:  # noqa: BLE001 - delivery must never raise into the supervisor
        return type(exc).__name__


class Notifier:
    def __init__(self, state: dict[str, Any], *, sender: Callable[[str], str | None] | None,
                 log: Callable[[str], None], max_per_hour: int = 12,
                 dedupe: timedelta = timedelta(hours=6)):
        self.state = state
        self.sender = sender
        self.log = log
        self.max_per_hour = max_per_hour
        self.dedupe = dedupe

    def send(self, event: str, now: datetime, *, dedupe_key: str | None = None, **fields: Any) -> bool:
        if self.sender is None:
            return False
        notify = self.state["notify"]
        text = render(event, fields)
        if text is None:
            notify["dropped"] = notify.get("dropped", 0) + 1
            self.log(f"notify: dropped {event} (field validation)")
            return False
        key = hashlib.sha256((dedupe_key or f"{event}|{json.dumps(fields, sort_keys=True)}").encode()).hexdigest()[:20]
        return self._post(event, text, key, now, {"event": event, "fields": fields, "key": key})

    def send_text(self, kind: str, text: str, now: datetime) -> bool:
        """A semantic message composed by `relay_observe` (spec 205): same secret check, dedupe on `kind`, rate
        limit, failure accounting and single retry as the fixed templates."""
        if self.sender is None:
            return False
        if not text or len(text) > 3800 or devctx.find_secret(text):
            self.state["notify"]["dropped"] = self.state["notify"].get("dropped", 0) + 1
            self.log("notify: dropped semantic message (validation)")
            return False
        key = hashlib.sha256(kind.encode()).hexdigest()[:20]
        return self._post(kind.split("|", 1)[0], text, key, now, None)  # the observer re-sends until delivered

    def _post(self, event: str, text: str, key: str, now: datetime, retry: dict[str, Any] | None) -> bool:
        notify = self.state["notify"]
        last = parse_ts(notify["sent"].get(key))
        if last and now - last < self.dedupe:
            return False
        window = [t for t in notify["window"] if (ts := parse_ts(t)) and now - ts < timedelta(hours=1)]
        if len(window) >= self.max_per_hour:
            notify["window"] = window
            notify["dropped"] = notify.get("dropped", 0) + 1
            self.log(f"notify: rate-limited {event}")
            return False
        error = self._deliver(text)
        if error:
            notify["failures"] = notify.get("failures", 0) + 1
            notify["retry"] = retry if notify.get("retry") is None else notify["retry"]
            self.log(f"notify: delivery failed ({error})")
            return False
        window.append(iso(now))
        notify["window"] = window
        notify["sent"][key] = iso(now)
        notify["sent"] = {k: v for k, v in notify["sent"].items() if (ts := parse_ts(v)) and now - ts < timedelta(days=8)}
        return True

    def retry_pending(self, now: datetime) -> None:
        notify = self.state["notify"]
        pending = notify.get("retry")
        if not pending or self.sender is None:
            return
        notify["retry"] = None  # at most one retry; dropped afterwards whatever happens
        window = [t for t in notify["window"] if (ts := parse_ts(t)) and now - ts < timedelta(hours=1)]
        if len(window) >= self.max_per_hour:
            notify["window"] = window
            notify["dropped"] = notify.get("dropped", 0) + 1
            self.log(f"notify: rate-limited retry of {pending['event']}")
            return
        text = render(pending["event"], pending["fields"])
        if text and not self._deliver(text):
            notify["sent"][pending["key"]] = iso(now)
            notify["window"] = window + [iso(now)]

    def _deliver(self, text: str) -> str | None:
        try:
            return self.sender(text) if self.sender else "no_sender"
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__


