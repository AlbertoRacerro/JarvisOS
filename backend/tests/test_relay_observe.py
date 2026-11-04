"""Spec 205: semantic owner snapshot, Italian digest, edge-triggered alerts and terminal monitor (offline)."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if "relay_continuity" not in sys.modules:
    _rc_spec = importlib.util.spec_from_file_location("relay_continuity", SCRIPTS / "relay_continuity.py")
    assert _rc_spec and _rc_spec.loader
    _rc = importlib.util.module_from_spec(_rc_spec)
    sys.modules["relay_continuity"] = _rc
    _rc_spec.loader.exec_module(_rc)
rc = sys.modules["relay_continuity"]
_spec = importlib.util.spec_from_file_location("relay_observe", SCRIPTS / "relay_observe.py")
assert _spec and _spec.loader
ro = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ro)

T0 = datetime(2026, 10, 22, 8, 0, tzinfo=UTC)
AUTH = "L20261021095156-016b"
SESSION = "20261022-080000-abcdef"
WEBHOOK = "https://hooks.slack.com/services/T000/B000/abcdefghijklmnopqrstuvwx"


def mission(root: Path, mid: str, tasks: list[str], *, at: datetime = T0) -> None:
    (root / "missions").mkdir(parents=True, exist_ok=True)
    (root / "missions" / f"{mid}.json").write_text(json.dumps({
        "schema": rc.MISSION_SCHEMA, "id": mid, "authority": AUTH, "submitted_at": rc.iso(at), "session": None,
        "repo": "/repo", "tasks": [{"text": t} for t in tasks]}))


def store_state(root: Path, mid: str, **changes: Any) -> None:
    state = rc.default_state()
    state["mission"] = {"id": mid, "next": 0, "total": 1, "done": []}
    for key, value in changes.items():
        if key == "mission":
            state["mission"].update(value)
        else:
            state[key] = value
    rc.Store(root / "missions" / mid).save(state, T0)


def turn(repo: Path, session: str, number: int, state: dict[str, Any] | None, output: str = "") -> Path:
    path = repo / ".agent-relay" / "sessions" / session / "turns" / f"turn-{number:03d}"
    path.mkdir(parents=True)
    if state is not None:
        (path / "state.json").write_text(json.dumps(state))
    (path / "output.jsonl").write_text(output)
    return path


def snap(root: Path, repo: Path, *, alive: bool | None = True, ledger: list[dict[str, Any]] | None = None,
         now: datetime = T0 + timedelta(hours=1)) -> dict[str, Any]:
    return ro.snapshot(root, repo, now, alive=lambda _p: alive, ledger=lambda _r: ledger or [])


M1 = "m-20261022T080000Z-aaaaaa"
M2 = "m-20261022T090000Z-bbbbbb"
ROADMAP_TASK = "Roadmap continuation cycle 4. Act as the coordinator described in docs/PROCESS.md."


# ---------------------------------------------------------------------------
# Classification


def test_classification_covers_every_owner_state(tmp_path: Path) -> None:
    root, repo = tmp_path / "c", tmp_path / "r"
    root.mkdir()
    assert snap(root, repo)["state"] == "idle"
    assert snap(root, repo, alive=None)["state"] == "unknown"

    mission(root, M1, [ROADMAP_TASK])
    assert snap(root, repo)["state"] == "queued"
    pending = {"nonce": "rc-x", "agent": "claude", "parent": None, "started_at": rc.iso(T0), "pid": 1, "pid_start": 1,
               "fingerprint": "f", "attempt": 1, "task": 0}
    store_state(root, M1, phase="running", pending=pending, agent="claude")
    s = snap(root, repo)
    assert s["state"] == "running" and s["mission"]["task"] == "1/1" and "sviluppo autonomo" in s["mission"]["label"]
    assert snap(root, repo, alive=False)["state"] == "stopped"
    assert "owner_down" in snap(root, repo, alive=False)["problems"]
    assert snap(root, repo, alive=None)["state"] == "unknown"

    store_state(root, M1, chain=[SESSION], head_session=SESSION)
    turn(repo, SESSION, 1, {"status": "continue", "reason": "PR #870 pushed", "next_step": "wait for CI on #870"})
    s = snap(root, repo)
    assert s["state"] == "checkpointed" and s["turn"]["next_step"] == "wait for CI on #870"

    store_state(root, M1, phase="held", chain=[SESSION], head_session=SESSION,
                hold={"reason": "task_check_failed", "at": rc.iso(T0), "attempt": 1, "turn": "t"})
    assert (snap(root, repo)["state"], snap(root, repo)["reason"]) == ("held", "task_check_failed")
    store_state(root, M1, phase="waiting", chain=[SESSION], waiting_since=rc.iso(T0))
    assert snap(root, repo)["state"] == "waiting"

    (root / "STOP").write_text("")
    store_state(root, M1, chain=[SESSION])
    assert snap(root, repo)["state"] == "stopped" and "owner_down" not in snap(root, repo, alive=False)["problems"]
    (root / "STOP").unlink()

    done = {"task": 0, "session": SESSION, "agent": "codex", "at": rc.iso(T0), "outcome": "verified"}
    store_state(root, M1, chain=[SESSION], mission={"next": 1, "done": [done], "completed_at": rc.iso(T0)})
    assert snap(root, repo)["state"] == "verified_complete"
    store_state(root, M1, chain=[SESSION], mission={"next": 1, "done": [{**done, "outcome": "cycle_settled"}],
                                                    "completed_at": rc.iso(T0)})
    assert snap(root, repo)["state"] == "idle"  # a settled cycle is not verified delivery
    rc.write_json_atomic(root / "roadmap.json", {"enabled": True, "cycle": 4, "paused": None})
    assert snap(root, repo)["state"] == "idle"
    down = snap(root, repo, alive=False)  # nobody left to queue the next roadmap cycle
    assert down["state"] == "stopped" and "owner_down|-" in ro.incidents(down, T0)
    assert "scrivi a Claude in chat" in ro.incidents(down, T0)["owner_down|-"]
    rc.write_json_atomic(root / "roadmap.json", {"enabled": False, "cycle": 4, "paused": "coordinator_blocked"})
    assert snap(root, repo)["state"] == "paused"


def test_github_activity_is_never_liveness_and_corrupt_state_is_a_problem(tmp_path: Path) -> None:
    root, repo = tmp_path / "c", tmp_path / "r"
    mission(root, M1, ["Fix #866"])
    (root / "missions" / M1).mkdir()
    (root / "missions" / M1 / "state.json").write_text("{not json")
    s = snap(root, repo)
    assert s["state"] == "unknown" and "mission_corrupt" in s["problems"]
    assert "problem|mission_corrupt" in ro.incidents(s, T0)


def test_owner_alive_reads_the_kernel_lock_table_without_locking(tmp_path: Path) -> None:
    lock = tmp_path / "serve.lock"
    assert ro.owner_alive(lock, tmp_path / "locks") is False
    lock.write_text("1")
    st = lock.stat()
    ident = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    table = tmp_path / "locks"
    table.write_text(f"1: POSIX  ADVISORY  WRITE 9 {ident} 0 EOF\n")
    assert ro.owner_alive(lock, table) is False
    table.write_text(f"1: POSIX  ADVISORY  WRITE 9 00:00:1 0 EOF\n2: FLOCK  ADVISORY  WRITE 77 {ident} 0 EOF\n")
    assert ro.owner_alive(lock, table) is True
    assert ro.owner_alive(lock, tmp_path / "missing") is None
    held = rc.Lock(lock)
    assert held.acquire()
    try:
        assert ro.owner_alive(lock) is True  # the real /proc/locks
    finally:
        held.release()
    assert ro.owner_alive(lock) is False


# ---------------------------------------------------------------------------
# Digest and alerts


def test_digest_answers_what_delivered_next_problem(tmp_path: Path) -> None:
    root, repo = tmp_path / "c", tmp_path / "r"
    mission(root, M1, ["Implement issue https://github.com/AlbertoRacerro/JarvisOS/issues/866 Slack digest"])
    done = {"task": 0, "session": SESSION, "agent": "claude", "at": rc.iso(T0 + timedelta(minutes=30)), "outcome": "verified"}
    store_state(root, M1, chain=[SESSION], head_session=SESSION,
                mission={"next": 1, "done": [done], "completed_at": rc.iso(T0 + timedelta(minutes=30))})
    turn(repo, SESSION, 1, {"status": "propose_done", "reason": f"Merged #871 ({WEBHOOK})",
                            "next_step": "Start #859 Phase B", "verification": ["CI green on #871", "ghp_" + "a" * 36]})
    rc.write_json_atomic(root / "roadmap.json", {"enabled": True, "cycle": 5, "paused": None})
    s = snap(root, repo, ledger=[{"kind": "note", "ts": rc.iso(T0 + timedelta(minutes=5)),
                                  "refs": {"lane": "maintainer"},
                                  "text": "Sto migliorando il riepilogo Slack. Prossimo: verificare la consegna."}])
    text = ro.digest_text(s, since=rc.iso(T0), merges=[("871", "feat(205): semantic digest (Spec 205)")])
    assert "*Sto facendo:* Sto migliorando il riepilogo Slack." in text
    assert "*Fatto:*" in text and "*Prossimo:* verificare la consegna." in text
    assert "*Serve il tuo intervento:* no." in text
    assert "pull/871|Semantic digest>" in text and "Implement issue" in text
    assert "hooks.slack.com" not in text and "ghp_" not in text and "CI green" not in text
    assert len(text.splitlines()) <= 14 and len(text) <= ro.MAX_MESSAGE
    # Nothing new since the last digest: said plainly.
    quiet = ro.digest_text(s, since=rc.iso(T0 + timedelta(hours=1)), merges=[])
    assert "nessuna novità" in quiet


def _visible(text: str) -> str:
    return re.sub(r"<[^|>]+\|", "<", text)


def test_update_boundary_and_machine_marker_fall_back_to_label(tmp_path: Path) -> None:
    root, repo = tmp_path / "c", tmp_path / "r"
    mission(root, M1, ["Migliorare il riepilogo Slack"])
    store_state(root, M1, chain=[SESSION], head_session=SESSION)
    turn(repo, SESSION, 1, {"status": "continue", "reason": "raw English claim in scripts/relay_observe.py",
                            "next_step": "run j-20261104T120000Z-aabbcc"})
    entries = [{"kind": "note", "ts": rc.iso(T0 - timedelta(seconds=1)), "refs": {"lane": "maintainer"},
                "text": "Sto cambiando un lavoro precedente."},
               {"kind": "claim", "ts": rc.iso(T0 + timedelta(seconds=1)), "text": "SHA abcdef0123 in scripts/relay_observe.py"}]
    s = snap(root, repo, ledger=entries)
    assert s["update"] is None
    fallback = ro.digest_text(s, since=None, merges=[])
    assert "*Sto facendo:* il compito che hai messo in coda: Migliorare il riepilogo Slack" in fallback
    assert "raw English" not in fallback and "abcdef0123" not in fallback and "j-" not in fallback
    entries.append({"kind": "note", "ts": rc.iso(T0 + timedelta(minutes=1)), "refs": {"lane": "maintainer"},
                    "text": "Sto cambiando `relay_observe.py`. Prossimo: provare."})
    assert snap(root, repo, ledger=entries)["update"] is None
    entries[-1]["text"] = "Sto migliorando il riepilogo Slack. Prossimo: verificare il risultato."
    entries.insert(0, {"kind": "note", "ts": rc.iso(T0 + timedelta(minutes=2)),
                       "refs": {"lane": "other"}, "text": "Nota di un altro canale."})
    s = snap(root, repo, ledger=entries)
    assert (s["update"], s["update_next"]) == ("Sto migliorando il riepilogo Slack.", "verificare il risultato.")
    assert "*Prossimo:* verificare il risultato." in ro.digest_text(s, since=None, merges=[])
    for value in ("abcdef0", "abcdefa", M1, "j-20261104T120000Z-aabbcc", AUTH,
                  "scripts/relay_observe.py", "src/module", r"C:\Users\Sample\secret.txt", "`cmd`",
                  "Sto modificando relay_observe.py", "m-20271104T110422Z-27bb5e", "L20270101000000-abcd"):
        assert ro.machine_marker(value), value
    for value in ("Non so cosa accadde ieri.", "La cosa decadde ieri.",
                  "Sto aggiungendo la colonna Copilot in Coding > Runtime."):
        assert not ro.machine_marker(value), value

    for unsafe in ("Sto leggendo abcdefa.", "Sto leggendo src/module.", "Sto verificando #900.",
                   r"Sto leggendo C:\Users\Sample\secret.txt."):
        entries[-1]["text"] = unsafe + " Prossimo: verificare il riepilogo."
        assert snap(root, repo, ledger=entries)["update"] is None
        assert "Migliorare il riepilogo Slack" in ro.digest_text(snap(root, repo, ledger=entries),
                                                                 since=None, merges=[])


def test_update_without_active_mission_uses_last_delivered_session_start(tmp_path: Path) -> None:
    root, repo = tmp_path / "c", tmp_path / "r"
    mission(root, M1, ["Migliorare il riepilogo Slack"])
    done = {"task": 0, "session": SESSION, "agent": "claude", "at": rc.iso(T0 + timedelta(minutes=30)),
            "outcome": "verified"}
    store_state(root, M1, chain=[SESSION], mission={"next": 1, "done": [done],
                                                  "completed_at": rc.iso(T0 + timedelta(minutes=30))})
    entries = [{"kind": "note", "ts": rc.iso(T0 - timedelta(seconds=1)), "refs": {"lane": "maintainer"},
                "text": "Nota precedente."},
               {"kind": "note", "ts": rc.iso(T0 + timedelta(minutes=5)), "refs": {"lane": "maintainer"},
                "text": "Sto verificando il riepilogo."}]
    assert snap(root, repo, ledger=entries)["update"] == "Sto verificando il riepilogo."
    assert snap(root, repo, ledger=entries[:1])["update"] is None


def test_realistic_visible_digest_and_alerts_have_no_machine_markers(tmp_path: Path) -> None:
    root, repo = tmp_path / "c", tmp_path / "r"
    mission(root, M1, ["Roadmap continuation cycle 289. Act as coordinator."])
    store_state(root, M1, chain=[SESSION], head_session=SESSION)
    turn(repo, SESSION, 1, {"status": "continue", "reason": "run j-20261104T120000Z-aabbcc",
                            "next_step": "open scripts/relay_observe.py"})
    ledger = [{"kind": "claim", "ts": rc.iso(T0), "text": "SHA abcdef0123 in scripts/relay_observe.py"},
              {"kind": "note", "ts": rc.iso(T0 + timedelta(minutes=1)), "refs": {"lane": "maintainer"},
               "text": "Sto migliorando gli aggiornamenti Slack. Prossimo: verificare gli avvisi."}]
    rc.write_json_atomic(root / "roadmap.json", {"enabled": True, "cycle": 289, "paused": None})
    s = snap(root, repo, ledger=ledger)
    s["delivered"] = [{"at": rc.iso(T0 + timedelta(minutes=10)), "label": "Riepilogo delle quote",
                       "outcome": "verified"}]
    s["jobs"] = [{"id": "j-20261104T120000Z-aabbcc", "status": "running", "exit_code": None}]
    merged = [("871", "feat(quotes): add quote panel (Spec 241)"),
              ("872", "fix: improve #872 alerts (Spec 241) (#872)"),
              ("873", "docs: tidy notes #873")]
    visible = _visible(ro.digest_text(s, since=None, merges=merged))
    assert "<Add quote panel>" in visible and "<Improve alerts>" in visible and "<Tidy notes>" in visible
    # Settled roadmap cycles are not listed as deliveries; repeated labels appear once.
    cycles = [{"at": rc.iso(T0 + timedelta(minutes=n)), "label": ro.ROADMAP_LABEL, "outcome": "cycle_settled"}
              for n in (11, 12)]
    repeated = ro.digest_text({**s, "delivered": s["delivered"] * 2 + cycles}, since=None, merges=[])
    assert ro.ROADMAP_LABEL not in repeated and repeated.count("Riepilogo delle quote") == 1
    assert "nessuna novità" in ro.digest_text({**s, "delivered": cycles}, since=None, merges=[])
    assert not ro.machine_marker(visible)
    assert "ciclo 289" not in visible and "j-" not in visible and "m-" not in visible
    states = [
        {**s, "state": "held", "reason": "auth_failure"},
        {**s, "state": "paused", "reason": "coordinator_blocked", "question": "Vuoi continuare con le quote?"},
        {**s, "state": "paused", "reason": "coordinator_blocked", "question": "Controlla `cmd`?"},
        {**s, "state": "paused", "reason": "coordinator_blocked", "question": "Approve spending on #900?"},
        {**s, "state": "stopped", "problems": ["owner_down"]},
        {**s, "state": "unknown", "problems": ["hostops_down", "roadmap_corrupt"]},
    ]
    for state in states:
        assert not ro.machine_marker(_visible(ro.digest_text(state, since=None, merges=merged)))
        for description in ro.incidents(state, T0).values():
            assert not ro.machine_marker(description)
            assert not ro.machine_marker(_visible(ro.alert_text(description, state)))
    assert ro.incidents(states[1], T0)["paused|coordinator_blocked"] == "Vuoi continuare con le quote?"
    assert "rispondi a Claude" in ro.incidents(states[2], T0)["paused|coordinator_blocked"]
    assert "rispondi a Claude" in ro.incidents(states[3], T0)["paused|coordinator_blocked"]
    assert "#900" not in ro.digest_text(states[3], since=None, merges=merged)
    assert "Approve spending" not in ro.alert_text(ro.incidents(states[3], T0)["paused|coordinator_blocked"], states[3])
    with_refs = {**s, "mission": {"label": "Migliorare #900 le quote"},
                 "delivered": [{"at": rc.iso(T0), "label": "Completare #900 il pannello"}],
                 "update": "Sto leggendo src/module.", "update_next": "controllare abcdefa"}
    text = ro.digest_text(with_refs, since=None, merges=[])
    assert "Migliorare le quote" in text and "Completare il pannello" in text
    assert "src/module" not in text and "abcdefa" not in text and "#900" not in text


def test_clean_rejects_secrets_and_bounds_text() -> None:
    assert ro.clean("token ghp_" + "b" * 36) is None
    assert ro.clean("a\x1b[31mred\x00 " + "x" * 500, 50).endswith("…")
    assert ro.clean(f"see {WEBHOOK} now") == "see [link] now"
    assert ro.clean(None) is None and ro.clean("   ") is None


class Sender:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    def __call__(self, kind: str, text: str) -> bool:
        if self.fail:
            return False
        self.sent.append((kind, text))
        return True


def held(root: Path, reason: str = "auth_failure") -> None:
    store_state(root, M1, phase="held", chain=[SESSION], head_session=SESSION,
                hold={"reason": reason, "at": rc.iso(T0), "attempt": 1, "turn": "t"})


def test_alerts_are_prompt_edge_triggered_and_survive_restart(tmp_path: Path) -> None:
    root, repo = tmp_path / "c", tmp_path / "r"
    mission(root, M1, [ROADMAP_TASK])
    store_state(root, M1, chain=[SESSION])
    obs: dict[str, Any] = {}
    send = Sender()
    every = timedelta(hours=2)
    now = T0 + timedelta(hours=1)
    ro.observe_step(obs, snap(root, repo), now, send=send, digest_every=every, merges=lambda _s: [])
    assert [k for k, _ in send.sent] == [f"digest|{rc.iso(now)}"]  # first digest, no incident

    held(root)
    now += timedelta(minutes=1)
    ro.observe_step(obs, snap(root, repo), now, send=send, digest_every=every, merges=lambda _s: [])
    alert = [t for k, t in send.sent if k.startswith("alert|")]
    assert len(alert) == 1 and "tua verifica" in alert[0] and "scrivi a Claude" in alert[0]

    # Repeated identical observations, also after a watcher restart (obs persisted as JSON), stay silent.
    obs = json.loads(json.dumps(obs))
    for _ in range(5):
        now += timedelta(minutes=1)
        ro.observe_step(obs, snap(root, repo), now, send=send, digest_every=every, merges=lambda _s: [])
    assert len([k for k, _ in send.sent if k.startswith("alert|")]) == 1

    # Cleared, then a different incident: announced again.
    store_state(root, M1, chain=[SESSION])
    ro.observe_step(obs, snap(root, repo), now, send=send, digest_every=every, merges=lambda _s: [])
    assert obs["active"] == {}
    ro.observe_step(obs, snap(root, repo, alive=False), now, send=send, digest_every=every, merges=lambda _s: [])
    assert "non è attivo" in send.sent[-1][1]

    # The digest comes due again after the interval.
    now += every
    ro.observe_step(obs, snap(root, repo, alive=False), now, send=send, digest_every=every, merges=lambda _s: [])
    assert send.sent[-1][0] == f"digest|{rc.iso(now)}" and "non è attivo" in send.sent[-1][1]


def test_slack_down_never_blocks_and_is_reported_when_delivery_recovers(tmp_path: Path) -> None:
    root, repo = tmp_path / "c", tmp_path / "r"
    mission(root, M1, [ROADMAP_TASK])
    held(root)
    obs: dict[str, Any] = {}
    down = Sender(fail=True)
    now = T0 + timedelta(hours=1)
    ro.observe_step(obs, snap(root, repo), now, send=down, digest_every=timedelta(hours=2), merges=lambda _s: [])
    assert obs.get("active") == {} and obs.get("last_digest") is None  # retried on the next step, not lost
    up = Sender()
    ro.observe_step(obs, snap(root, repo), now + timedelta(minutes=1), send=up, digest_every=timedelta(hours=2),
                    merges=lambda _s: [], failures=2)
    kinds = [k.split("|", 1)[0] for k, _ in up.sent]
    assert kinds == ["alert", "digest"] and "2 invii Slack falliti" in up.sent[-1][1]


def test_prolonged_wait_becomes_an_incident_and_a_short_one_does_not(tmp_path: Path) -> None:
    root, repo = tmp_path / "c", tmp_path / "r"
    mission(root, M1, [ROADMAP_TASK])
    store_state(root, M1, phase="waiting", chain=[SESSION], waiting_since=rc.iso(T0))
    assert ro.incidents(snap(root, repo), T0 + timedelta(hours=1)) == {}
    assert any(k.startswith("waiting|") for k in ro.incidents(snap(root, repo), T0 + timedelta(hours=7)))


def test_paused_roadmap_alert_carries_the_recorded_question(tmp_path: Path) -> None:
    root, repo = tmp_path / "c", tmp_path / "r"
    mission(root, M1, [ROADMAP_TASK])
    done = {"task": 0, "session": SESSION, "agent": "claude", "at": rc.iso(T0), "outcome": "cycle_settled"}
    store_state(root, M1, chain=[SESSION], mission={"next": 1, "done": [done], "completed_at": rc.iso(T0)})
    rc.write_json_atomic(root / "roadmap.json", {"enabled": True, "cycle": 4, "paused": "coordinator_blocked"})
    ledger = [{"kind": "question", "ts": rc.iso(T0 + timedelta(minutes=5)), "text": "Vuoi approvare la spesa?"}]
    s = snap(root, repo, ledger=ledger)
    (key, description), = ro.incidents(s, T0).items()
    text = ro.alert_text(description, s)
    assert key == "paused|coordinator_blocked" and "Vuoi approvare la spesa?" in text


def test_watch_notifier_send_text_dedupes_and_never_raises(tmp_path: Path) -> None:
    state = rc.default_state()
    sent: list[str] = []
    notifier = rc.Notifier(state, sender=lambda text: sent.append(text) or None, log=lambda _m: None)
    assert notifier.send_text("alert|held|x", "ciao", T0)
    assert not notifier.send_text("alert|held|x", "ciao", T0 + timedelta(minutes=1))
    assert not notifier.send_text("alert|y", "token ghp_" + "c" * 36, T0)
    broken = rc.Notifier(state, sender=lambda text: (_ for _ in ()).throw(OSError("down")), log=lambda _m: None)
    assert not broken.send_text("digest|z", "ciao", T0) and state["notify"]["failures"] == 1
    assert state["notify"]["retry"] is None  # the observer re-sends; no duplicate through the retry slot
    assert sent == ["ciao"]


def test_merges_since_reads_merge_commits() -> None:
    log = ("Merge pull request #871 from a/b\x1ffeat(205): digest\n\x1e\n"
           "Merge pull request #870 from a/c\x1fdocs: x\x1e\nplain commit\x1f\x1e")
    found = ro.merges_since(Path("."), rc.iso(T0), run=lambda *a, **k: (0, log))
    assert found == [("870", "docs: x"), ("871", "feat(205): digest")]
    assert ro.merges_since(Path("."), None, run=lambda *a, **k: (0, log)) == []


# ---------------------------------------------------------------------------
# Terminal monitor


def test_tail_reads_torn_lines_once_complete(tmp_path: Path) -> None:
    path = tmp_path / "output.jsonl"
    path.write_text('{"type": "result", "subtype": "success"}\n{"type": "assi')
    tail = ro.Tail(path)
    assert [e["type"] for e in tail.read()] == ["result"]
    assert tail.read() == []
    with open(path, "a") as handle:
        handle.write('stant", "message": {"content": [{"type": "text", "text": "ok"}]}}\nnot json\n')
    assert [e["type"] for e in tail.read()] == ["assistant"]


def test_render_events_are_truthful_and_redacted() -> None:
    claude = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": f"posting to {WEBHOOK}"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "export T=ghp_" + "d" * 36}}]}}
    lines = ro.render_event(claude)
    assert lines[0].startswith("●") and "[redacted]" in lines[0] and "hooks.slack" not in lines[0]
    assert lines[1].startswith("⏺ Bash(") and "ghp_" not in lines[1]
    result = {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "boom"}]}}
    assert ro.render_event(result) == ["  ✗ boom"]
    codex = {"type": "item.started", "item": {"type": "command_execution", "command": "pytest -q"}}
    assert ro.render_event(codex) == ["⏺ Bash(pytest -q)"]
    assert ro.render_event({"type": "assistant", "parent_tool_use_id": "x", "message": {"content": []}}) == []
    assert ro.render_event({"type": "thinking", "text": "hidden"}) == []


def test_native_argon_monitor_redacts_tools_and_does_not_hide_denials() -> None:
    step = {"step_index": 2, "step_type": "tool", "tool_name": "run_command", "state": "ACTIVE",
            "tool_info": {"parameters": {"CommandLine": "echo ghp_" + "d" * 36}}}
    items = ro.event_items({"event": "step_update", "step_update": step}, paths=True)
    assert items[0]["kind"] == "tool"
    assert "ghp_" not in json.dumps(items) and "[redacted]" in items[0]["text"]
    step["state"] = "DONE"
    step["tool_info"]["output"] = f"Command exited with code 0.\n{WEBHOOK}"
    output = ro.event_items({"event": "step_update", "step_update": step})
    assert output[0]["kind"] == "output" and "hooks.slack" not in json.dumps(output)
    step["tool_info"]["output"] = "Command exited with code 1.\n\nCommand output:\nfailed"
    assert ro.event_items({"event": "step_update", "step_update": step})[0]["failed"] is True
    denied = ro.event_items({"event": "result", "result": {
        "status": "SUCCESS", "response": "", "denied_actions": [{"action": "command"}]}})
    assert denied[-1]["kind"] == "done" and denied[-1]["failed"] is True
    assert ro.event_items({"event": "result", "result": {"status": "SUCCESS", "response": ""}})[-1]["failed"] is True
    assert ro.event_items({"event": "step_update", "step_update": {"step_type": "thinking", "text": "hidden"}}) == []
    assert ro.event_items({"event": "result", "result": None})[0]["kind"] == "error"


def test_follow_handles_session_handoff_without_locks_or_writes(tmp_path: Path) -> None:
    root, repo = tmp_path / "c", tmp_path / "r"
    mission(root, M1, [ROADMAP_TASK])
    store_state(root, M1, phase="running", chain=[SESSION], head_session=SESSION, agent="claude")
    turn(repo, SESSION, 1, None, '{"type": "assistant", "message": {"content": [{"type": "text", "text": "uno"}]}}\n')
    before = sorted((p, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file())
    out: list[str] = []
    ticks = {"n": 0}

    def stop() -> bool:
        ticks["n"] += 1
        if ticks["n"] == 3:  # provider switch: a new session continues the task
            turn(repo, "20261022-090000-fedcba", 1, None,
                 '{"type": "item.completed", "item": {"type": "agent_message", "text": "due"}}\n')
            store_state(root, M1, phase="running", chain=[SESSION, "20261022-090000-fedcba"],
                        head_session="20261022-090000-fedcba", agent="codex")
            before.clear()
            before.extend(sorted((p, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()))
        return ticks["n"] > 5

    ro.follow(root, repo, write=out.append, poll=0, stop=stop)
    text = "\n".join(out)
    assert "● uno" in text and "● due" in text and "codex" in text and "fedcba/turn-001" in text
    assert text.index("uno") < text.index("due")
    assert sorted((p, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()) == before
    assert not list(root.glob("*.lock"))


@pytest.mark.parametrize("hours", [0.5, 5])
def test_watch_rejects_digest_interval_outside_one_to_four_hours(hours: float) -> None:
    assert rc.main(["watch", "--digest-hours", str(hours), "--once", "--no-notify"]) == 2


def test_first_turn_of_an_attempt_is_found_by_its_nonce(tmp_path: Path) -> None:
    root, repo = tmp_path / "c", tmp_path / "r"
    mission(root, M1, [ROADMAP_TASK])
    pending = {"nonce": "rc-20261022T080000Z-0123abcd", "agent": "claude", "parent": None, "started_at": rc.iso(T0),
               "pid": 1, "pid_start": 1, "fingerprint": "f", "attempt": 1, "task": 0}
    store_state(root, M1, phase="running", pending=pending, agent="claude")
    other = turn(repo, "20261022-075900-111111", 1, None, "")
    (other / "prompt.md").write_text("an unrelated REPL turn")
    live = turn(repo, "20261022-080001-222222", 1, None,
                '{"type": "assistant", "message": {"content": [{"type": "text", "text": "avvio"}]}}\n')
    (live / "prompt.md").write_text("[relay-continuity attempt rc-20261022T080000Z-0123abcd]\nRoadmap ...")
    assert ro.active_turn(root, repo)[1] == live
    assert snap(root, repo)["turn"] == {"turn": "turn-001", "running": True}
    out: list[str] = []
    ticks = iter(range(3))
    ro.follow(root, repo, write=out.append, poll=0, stop=lambda: next(ticks, None) is None)
    assert "● avvio" in out


def test_a_recurring_incident_is_alerted_again_through_the_real_notifier(tmp_path: Path) -> None:
    root, repo = tmp_path / "c", tmp_path / "r"
    mission(root, M1, [ROADMAP_TASK])
    store_state(root, M1, chain=[SESSION])
    delivered: list[str] = []
    state = rc.default_state()
    notifier = rc.Notifier(state, sender=lambda text: delivered.append(text) or None, log=lambda _m: None)
    obs: dict[str, Any] = {}

    def step(now: datetime) -> None:
        ro.observe_step(obs, snap(root, repo), now, send=lambda kind, text: notifier.send_text(kind, text, now),
                        digest_every=timedelta(hours=4), merges=lambda _s: [])

    now = T0 + timedelta(hours=1)
    step(now)
    held(root)
    step(now := now + timedelta(minutes=1))
    store_state(root, M1, chain=[SESSION])  # cleared 30 minutes later
    step(now := now + timedelta(minutes=30))
    held(root)  # the same hold comes back well inside the notifier's 6 h dedupe window
    step(now := now + timedelta(minutes=30))
    step(now + timedelta(minutes=1))  # and is still announced only once per occurrence
    assert sum("serve il tuo intervento" in text for text in delivered) == 2


def test_owner_down_is_reported_while_a_mission_waits_or_is_held(tmp_path: Path) -> None:
    root, repo = tmp_path / "c", tmp_path / "r"
    mission(root, M1, [ROADMAP_TASK])
    store_state(root, M1, phase="waiting", chain=[SESSION], waiting_since=rc.iso(T0))
    assert any(k.startswith("owner_down|") for k in ro.incidents(snap(root, repo, alive=False), T0))
    assert not any(k.startswith("owner_down|") for k in ro.incidents(snap(root, repo, alive=True), T0))
    held(root)
    found = ro.incidents(snap(root, repo, alive=False), T0)
    assert any(k.startswith("owner_down|") for k in found) and any(k.startswith("held|") for k in found)


def test_clean_neutralises_slack_markup_links_and_host_paths() -> None:
    value = ro.clean("<!channel> <@U123> see evil.example/x?k=1, www.foo.org, HTTPS://A.B/c and "
                     "<mailto:a@b.c|click> in /home/user/.config/x; hooks.slack.com/services/T/B/C & done")
    assert value is not None
    for leaked in ("<", ">", "evil.example", "www.foo", "A.B/c", "mailto", "/home/user", "hooks.slack.com"):
        assert leaked not in value, leaked
    assert "&lt;!channel&gt;" in value and "&amp; done" in value
    assert ro.clean("spec 205 in relay_observe.py, see STATUS.md and #869") == \
        "spec 205 in relay_observe.py, see STATUS.md and #869"
    assert "<https://github.com/AlbertoRacerro/JarvisOS/issues/869|#869>" in ro.links(ro.clean("PR #869") or "")


@pytest.mark.parametrize("request_age,beat_age,expected", [(121, None, True), (121, 121, True),
                                                         (121, 30, False), (30, None, False)])
def test_hostops_down(tmp_path, monkeypatch, request_age, beat_age, expected):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    paths = rc.host_ops.Paths(tmp_path)
    paths.requests.mkdir(parents=True)
    request = paths.requests / ("a" * 32 + ".json")
    request.write_text("{}")
    ts = T0.timestamp() - request_age
    os.utime(request, (ts, ts))
    if beat_age is not None:
        paths.heartbeat.write_text(json.dumps({"at": rc.iso(T0 - timedelta(seconds=beat_age))}))
    state = ro.snapshot(tmp_path / "control", tmp_path / "repo", T0, alive=lambda _: True, ledger=lambda _: [])
    alerts = ro.incidents(state, T0)
    assert ("problem|hostops_down" in alerts) == expected


@pytest.mark.parametrize(("statuses", "reason"), [
    (["running", "succeeded"], "job_wait"),
    (["succeeded", "succeeded"], "jobs_ready"),
    (["failed", "succeeded"], "jobs_recoverable"),
    (["lost", "timeout"], "jobs_recoverable"),
    (["unknown", "running"], "job_observation_uncertain"),
    (["orphaned", "succeeded"], "job_observation_uncertain"),
])
def test_job_wait_is_not_a_human_incident(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                        statuses: list[str], reason: str) -> None:
    root, repo = tmp_path / "c", tmp_path / "r"
    ids = ["j-20261026T070720Z-0878e7", "j-20261026T070721Z-e6d1ba"]
    rc.write_json_atomic(root / "roadmap.json", {"enabled": True, "cycle": 135, "paused": "coordinator_blocked",
                                               "job_wait": ids})
    records = {j: {"id": j, "status": status, "exit_code": 1 if status == "failed" else None}
               for j, status in zip(ids, statuses, strict=True)}
    monkeypatch.setattr(rc.relay_jobs, "load", lambda job_id, _root: records[job_id])
    monkeypatch.setattr(rc.relay_jobs, "effective_status", lambda j: j["status"])
    # A stale ledger question from the completed turn must not turn an operational wait into a human request.
    s = snap(root, repo, ledger=[{"kind": "question", "ts": rc.iso(T0), "text": "Old question"}])
    assert s["state"] == "waiting" and s["reason"] == reason
    assert s["jobs"] == list(records.values())
    assert not s.get("question") and not ro.incidents(s, T0 + timedelta(days=2))
    text = ro.digest_text(s, since=None, merges=[])
    assert "*Serve il tuo intervento:* no." in text
    assert "in attesa di lavori lunghi in corso" in text and all(j not in text for j in ids)
    assert "il coordinatore ha dichiarato un blocco" not in text


def test_job_wait_with_unknown_record_preserves_uncertainty_and_reports_owner_down(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, repo = tmp_path / "c", tmp_path / "r"
    rc.write_json_atomic(root / "roadmap.json", {"enabled": True, "cycle": 135, "paused": "coordinator_blocked",
                                               "job_wait": ["j-20261026T070720Z-0878e7"]})
    monkeypatch.setattr(rc.relay_jobs, "load", lambda *_: None)
    s = snap(root, repo)
    assert s["reason"] == "job_observation_uncertain" and s["jobs"][0]["status"] == "unknown"
    assert not ro.incidents(s, T0)
    down = snap(root, repo, alive=False)
    assert down["state"] == "stopped" and "owner_down|-" in ro.incidents(down, T0)
    unknown = snap(root, repo, alive=None)
    assert unknown["state"] == "unknown" and "unknown|owner" in ro.incidents(unknown, T0)
