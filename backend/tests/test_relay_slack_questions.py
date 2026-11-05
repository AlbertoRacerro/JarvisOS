"""Spec 244: host Slack questions and answers, using only fake transport and temporary state."""
from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.modules.coding import relay_monitor

ro = relay_monitor.observe()
rc = ro.rc
NOW = datetime(2026, 11, 5, 16, 0, tzinfo=UTC)
CONFIG = {"bot_token": "xoxb-FAKE-SECRET-1234567890123456", "channel_id": "C123", "maintainer_user_id": "U123"}


@pytest.fixture
def space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, repo = tmp_path / "continuity", tmp_path / "repo"
    root.mkdir()
    repo.mkdir()
    monkeypatch.setenv("JARVIS_CONTROL_DIR", str(tmp_path / "control"))
    monkeypatch.setattr(relay_monitor, "control_root", lambda: root)
    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: repo)
    return root, repo


def question(repo: Path, text: str) -> dict:
    env = rc.devctx.default_env(repo)
    entry = rc.devctx.make_entry(env, "question", text)
    rc.devctx.append_entry(env, entry)
    return entry


def open_snap(repo: Path) -> dict:
    return {"state": "idle", "reason": None, "problems": [], "mission": None,
            "at": rc.iso(NOW), "open_questions": ro.parse_open_questions(ro._ledger(repo))}


class FakeSlack:
    def __init__(self):
        self.messages: list[dict] = []
        self.posts: list[dict] = []
        self.next_post_ts: str | None = None

    def api(self, method: str, _config: dict, params: dict) -> dict:
        if method == "chat.postMessage":
            self.posts.append(params.copy())
            ts = self.next_post_ts or f"1000.{len(self.posts):06d}"
            self.next_post_ts = None
            return {"ok": True, "ts": ts}
        if method == "conversations.history":
            return {"ok": True, "messages": [m for m in self.messages if not m.get("thread_ts")]}
        return {"ok": True, "messages": [m for m in self.messages if m.get("thread_ts") == params["ts"]]}

    def post(self, config: dict, text: str, *, thread_ts: str | None = None, api=None) -> str | None:
        return rc.slack_post(config, text, thread_ts=thread_ts, api=self.api)


def deliver(repo: Path, fake: FakeSlack, obs: dict) -> None:
    ro.observe_step(obs, open_snap(repo), NOW, send=lambda _k, _t: True,
                    digest_every=timedelta(hours=2), merges=lambda _s: [],
                    question_send=lambda _qid, text: fake.post(CONFIG, text))


def test_delivery_once_and_generic_suppressed(space):
    _root, repo = space
    q = question(repo, "Come procedere?\n1. (consigliata) Prima — conseguenza: veloce\n2. Dopo — conseguenza: lento")
    fake, obs = FakeSlack(), {}
    deliver(repo, fake, obs)
    assert len(fake.posts) == 1
    assert "Come procedere?" in fake.posts[0]["text"]
    assert "1. Prima (consigliata) — conseguenza: veloce" in fake.posts[0]["text"]
    assert "2. Dopo — conseguenza: lento" in fake.posts[0]["text"]
    assert obs["sent_questions"][q["id"]] == "1000.000001"
    deliver(repo, fake, json.loads(json.dumps(obs)))
    assert len(fake.posts) == 1
    paused = {**open_snap(repo), "state": "paused", "reason": "coordinator_blocked"}
    assert not any("rispondi a Claude in chat" in v for v in ro.incidents(paused, NOW).values())


def test_webhook_fallback_records_delivery_without_inbound(space):
    _root, repo = space
    q = question(repo, "Scelta?\n1. Sì — conseguenza: avanti")
    sent: list[str] = []
    obs: dict = {}

    def send(kind, text):
        if kind.startswith("question|"):
            sent.append(text)
        return True

    for _ in range(2):
        ro.observe_step(obs, open_snap(repo), NOW, send=send, digest_every=timedelta(hours=2), merges=lambda _s: [])
        obs = json.loads(json.dumps(obs))
    assert len(sent) == 1 and obs["sent_questions"][q["id"]] == "webhook"


def test_inbound_answer_replay_and_filters(space):
    root, repo = space
    q = question(repo, "Scelta?\n1. Sì — conseguenza: fatto")
    fake, obs = FakeSlack(), {}
    deliver(repo, fake, obs)
    bad = [
        {"user": "UOTHER"}, {"user": "U123", "bot_id": "B1"}, {"user": "U123", "subtype": "message_changed"},
        {"user": "U123", "channel": "COTHER"}, {"user": "U123", "text": "a" * 1001},
        {"user": "U123", "text": "xoxb-FAKE-SECRET-1234567890123456"},
    ]
    for i, row in enumerate(bad, 2):
        fake.messages.append({"ts": f"1000.{i:06d}", "channel": "C123", "text": "no", **row})
    rc.poll_slack_questions(obs, CONFIG, root, repo, ro, NOW, api=fake.api, post=fake.post)
    assert len(fake.posts) == 1 and len(ro.parse_open_questions(ro._ledger(repo))) == 1
    fake.messages.append({"ts": "1000.000008", "channel": "C123", "user": "U123", "text": "fammi x e y"})
    rc.poll_slack_questions(obs, CONFIG, root, repo, ro, NOW + timedelta(seconds=31), api=fake.api, post=fake.post)
    decisions = [e for e in ro._ledger(repo) if e["kind"] == "decision"]
    assert len(decisions) == 1
    assert decisions[0]["author"] == "maintainer (slack)"
    assert decisions[0]["supersedes"] == [q["id"]]
    assert decisions[0]["text"] == f"Maintainer answer to question {q['id']} (via Slack): fammi x e y"
    assert len(fake.posts) == 2 and "Ricevuto" in fake.posts[1]["text"]
    rc.poll_slack_questions(obs, CONFIG, root, repo, ro, NOW + timedelta(seconds=62), api=fake.api, post=fake.post)
    assert len([e for e in ro._ledger(repo) if e["kind"] == "decision"]) == 1
    assert len(fake.posts) == 2
    assert CONFIG["bot_token"] not in json.dumps(ro._ledger(repo))
    assert CONFIG["bot_token"] not in json.dumps(obs)
    assert CONFIG["bot_token"] not in json.dumps(fake.posts)


@pytest.mark.parametrize("delivery", ["slack", "webhook"])
def test_old_top_level_answer_does_not_answer_new_question(space, delivery):
    root, repo = space
    first = question(repo, "Prima?\n1. Sì — conseguenza: a")
    fake, obs = FakeSlack(), {}
    deliver(repo, fake, obs)
    fake.messages.append({"ts": "1000.000010", "channel": "C123", "user": "U123", "text": "risposta alla prima"})
    assert ro.record_answer(root, repo, first["id"], "risposta in Jarvis", source="Jarvis UI",
                            author="maintainer")["state"] == "ok"
    second = question(repo, "Seconda?\n1. Sì — conseguenza: b")
    fake.next_post_ts = "1000.000020"
    deliver(repo, fake, obs)
    if delivery == "webhook":
        obs["sent_questions"][second["id"]] = "webhook"
    posts_before = len(fake.posts)
    rc.poll_slack_questions(obs, CONFIG, root, repo, ro, NOW, api=fake.api, post=fake.post)
    assert [q["id"] for q in ro.parse_open_questions(ro._ledger(repo))] == [second["id"]]
    assert len([e for e in ro._ledger(repo) if e["kind"] == "decision"]) == 1
    assert len(fake.posts) == posts_before
    assert obs["slack_cursors"]["history"] == "1000.000010"


def test_failed_confirmation_retries_without_recording_answer_again(space):
    root, repo = space
    q = question(repo, "Scelta?\n1. Sì — conseguenza: avanti")
    fake, obs = FakeSlack(), {}
    deliver(repo, fake, obs)
    fake.messages.append({"ts": "1000.000010", "channel": "C123", "user": "U123", "text": "sì"})
    attempts: list[str] = []

    def fail_once(config, text, *, thread_ts=None, api=None):
        attempts.append(text)
        if len(attempts) == 1:
            return None
        return fake.post(config, text, thread_ts=thread_ts, api=api)

    rc.poll_slack_questions(obs, CONFIG, root, repo, ro, NOW, api=fake.api, post=fail_once)
    assert len([e for e in ro._ledger(repo) if e["kind"] == "decision"]) == 1
    assert obs["slack_cursors"]["history"] == "1000.000010"
    assert obs["slack_pending_confirmations"][0]["qid"] == q["id"]
    obs = json.loads(json.dumps(obs))  # the watcher persists obs between polls
    rc.poll_slack_questions(obs, CONFIG, root, repo, ro, NOW + timedelta(seconds=31), api=fake.api, post=fail_once)
    assert len(attempts) == 2
    assert len([e for e in ro._ledger(repo) if e["kind"] == "decision"]) == 1
    assert "Ricevuto" in fake.posts[-1]["text"]
    assert fake.posts[-1]["thread_ts"] == "1000.000010"
    assert obs["slack_pending_confirmations"] == []


def test_failed_confirmation_stops_after_five_attempts(space):
    root, repo = space
    question(repo, "Scelta?\n1. Sì — conseguenza: avanti")
    fake, obs = FakeSlack(), {}
    deliver(repo, fake, obs)
    fake.messages.append({"ts": "1000.000010", "channel": "C123", "user": "U123", "text": "sì"})
    attempts = []

    def fail_post(_config, text, *, thread_ts=None, api=None):
        attempts.append((text, thread_ts))
        return None

    for offset in range(0, 7):
        rc.poll_slack_questions(obs, CONFIG, root, repo, ro, NOW + timedelta(seconds=31 * offset),
                                api=fake.api, post=fail_post)
        obs = json.loads(json.dumps(obs))
    assert len(attempts) == 5
    assert obs["slack_pending_confirmations"] == []
    assert len([e for e in ro._ledger(repo) if e["kind"] == "decision"]) == 1


def test_thread_mapping_and_ambiguous_top_level(space):
    root, repo = space
    first = question(repo, "Prima?\n1. Sì — conseguenza: a")
    second = question(repo, "Seconda?\n1. Sì — conseguenza: b")
    fake, obs = FakeSlack(), {}
    deliver(repo, fake, obs)
    fake.messages.append({"ts": "1000.000003", "channel": "C123", "user": "U123", "text": "ok"})
    rc.poll_slack_questions(obs, CONFIG, root, repo, ro, NOW, api=fake.api, post=fake.post)
    assert len([e for e in ro._ledger(repo) if e["kind"] == "decision"]) == 0
    assert "thread" in fake.posts[-1]["text"]
    fake.messages.append({"ts": "1000.000005", "channel": "C123", "user": "U123", "text": "seconda scelta",
                          "thread_ts": obs["sent_questions"][second["id"]]})
    rc.poll_slack_questions(obs, CONFIG, root, repo, ro, NOW + timedelta(seconds=31), api=fake.api, post=fake.post)
    decisions = [e for e in ro._ledger(repo) if e["kind"] == "decision"]
    assert len(decisions) == 1 and decisions[0]["supersedes"] == [second["id"]]
    assert first["id"] in [q["id"] for q in ro.parse_open_questions(ro._ledger(repo))]


def test_slack_answer_wakes_no_progress_and_closed_question_gets_one_reply(space):
    root, repo = space
    q = question(repo, "Procedere?\n1. Sì — conseguenza: via")
    fake, obs = FakeSlack(), {}
    deliver(repo, fake, obs)
    rc.write_json_atomic(rc.roadmap_path(root), {"enabled": True, "paused": "no_progress",
                                                 "paused_at": rc.iso(NOW - timedelta(minutes=1)),
                                                 "wake_at": rc.iso(NOW + timedelta(days=5)), "no_progress": 3})
    fake.messages.append({"ts": "1000.000002", "channel": "C123", "user": "U123", "text": "sì",
                          "thread_ts": obs["sent_questions"][q["id"]]})
    rc.poll_slack_questions(obs, CONFIG, root, repo, ro, NOW, api=fake.api, post=fake.post)
    assert rc.load_roadmap(root)["paused"] is None
    assert len([e for e in ro._ledger(repo) if e["kind"] == "decision"]) == 1
    fake.messages.append({"ts": "1000.000004", "channel": "C123", "user": "U123", "text": "ancora"})
    rc.poll_slack_questions(obs, CONFIG, root, repo, ro, NOW + timedelta(seconds=31), api=fake.api, post=fake.post)
    assert "Non ci sono domande aperte" in fake.posts[-1]["text"]
    count = len(fake.posts)
    rc.poll_slack_questions(obs, CONFIG, root, repo, ro, NOW + timedelta(seconds=62), api=fake.api, post=fake.post)
    assert len(fake.posts) == count


def test_top_level_with_no_question_gets_one_reply(space):
    root, repo = space
    fake, obs = FakeSlack(), {}
    ts = f"{int(NOW.timestamp()) + 1}.000001"
    fake.messages.append({"ts": ts, "channel": "C123", "user": "U123", "text": "Ci sono domande?"})
    rc.poll_slack_questions(obs, CONFIG, root, repo, ro, NOW, api=fake.api, post=fake.post)
    assert len(fake.posts) == 1 and "Non ci sono domande aperte" in fake.posts[0]["text"]
    rc.poll_slack_questions(obs, CONFIG, root, repo, ro, NOW + timedelta(seconds=31), api=fake.api, post=fake.post)
    assert len(fake.posts) == 1


def test_no_progress_wakes_for_ui_slack_directive_and_receipt(space, monkeypatch):
    root, repo = space
    host_paths = rc.host_ops.Paths
    monkeypatch.setattr(rc.host_ops, "Paths", lambda _home: host_paths(root))
    q = question(repo, "Procedi?\n1. Sì — conseguenza: vai")
    paused = rc.iso(NOW - timedelta(days=1))
    road = {"enabled": True, "paused": "no_progress", "paused_at": paused,
            "wake_at": rc.iso(NOW + timedelta(days=5)), "no_progress": 3}
    rc.write_json_atomic(rc.roadmap_path(root), road)
    assert not rc.maintainer_or_hostops_after(repo, paused)
    assert relay_monitor.answer_question(q["id"], custom_text="sì")["state"] == "ok"
    assert rc.load_roadmap(root)["paused"] is None
    rc.write_json_atomic(rc.roadmap_path(root), road)
    assert rc.maintainer_or_hostops_after(repo, paused)
    env = rc.devctx.default_env(repo)
    directive = rc.devctx.make_entry(env, "directive", "Procedere", author="maintainer")
    rc.devctx.append_entry(env, directive)
    assert rc.maintainer_or_hostops_after(repo, paused)
    monkeypatch.setattr(rc.devctx, "read_ledger", lambda _env: [])
    class Paths:
        receipts = root / "receipts"
    Paths.receipts.mkdir()
    (Paths.receipts / ("a" * 32 + ".json")).write_text("{}")
    monkeypatch.setattr(rc.host_ops, "Paths", lambda _home: Paths)
    monkeypatch.setattr(rc.host_ops, "load_receipt", lambda _paths, _id: (
        {"outcome": "ok", "finished_at": rc.iso(NOW + timedelta(seconds=1))}, ""))
    assert rc.maintainer_or_hostops_after(repo, paused)


def test_roadmap_step_wakes_for_new_directive_or_receipt(space, monkeypatch):
    root, repo = space
    paused = rc.iso(NOW - timedelta(days=1))
    road = {"enabled": True, "paused": "no_progress", "paused_at": paused,
            "wake_at": rc.iso(NOW + timedelta(days=5)), "no_progress": 3,
            "authority": "L20261105160000-abcd", "fingerprint": "same", "job_states": {}, "pr_ids": []}
    rc.write_json_atomic(rc.roadmap_path(root), road)
    monkeypatch.setattr(rc, "roadmap_job_states", lambda _root: {})
    monkeypatch.setattr(rc, "roadmap_job_ids", lambda _root: [])
    monkeypatch.setattr(rc, "roadmap_pr_ids", lambda _repo: [])
    monkeypatch.setattr(rc, "roadmap_ci_fingerprint", lambda _repo: "same")
    paths = rc.host_ops.Paths
    monkeypatch.setattr(rc.host_ops, "Paths", lambda _home: paths(root))
    def submit(_authority, _tasks, mid):
        return {"id": mid}, None

    def step():
        return rc.roadmap_step(root, repo, fingerprint=lambda: "same", submit=submit, now=lambda: NOW)
    assert step() == "roadmap:paused:no_progress"
    env = rc.devctx.default_env(repo)
    rc.devctx.append_entry(env, rc.devctx.make_entry(env, "directive", "Agente", author="agent"))
    assert step() == "roadmap:paused:no_progress"
    rc.devctx.append_entry(env, rc.devctx.make_entry(env, "directive", "Procedere", author="maintainer"))
    assert step().startswith("roadmap:submitted:")
    rc.write_json_atomic(rc.roadmap_path(root), road)
    monkeypatch.setattr(rc.devctx, "read_ledger", lambda _env: [])
    receipts = paths(root).receipts
    receipts.mkdir(parents=True)
    (receipts / ("a" * 32 + ".json")).write_text("{}")
    monkeypatch.setattr(rc.host_ops, "load_receipt", lambda _paths, _id: (
        {"outcome": "failed", "finished_at": rc.iso(NOW)}, ""))
    assert step().startswith("roadmap:submitted:")


def test_config_and_client_reject_unsafe_transport(tmp_path: Path):
    config_file = tmp_path / "slack_bot.json"
    config_file.write_text(json.dumps(CONFIG))
    config_file.chmod(0o644)
    assert rc.load_slack_bot(config_file) is None
    config_file.chmod(0o600)
    assert rc.load_slack_bot(config_file) == CONFIG
    link = tmp_path / "slack_bot_link.json"
    link.symlink_to(config_file)
    assert rc.load_slack_bot(link) is None
    class Opener:
        def __init__(self):
            self.called = False
        def open(self, _request, timeout):
            self.called = True
            raise urllib.error.HTTPError("https://slack.com/api/chat.postMessage", 302, "redirect", {}, None)
    opener = Opener()
    assert rc.slack_api("chat.postMessage", CONFIG, {}, opener=opener, base_url="https://evil.example/api/") is None
    assert not opener.called
    assert rc.slack_api("chat.postMessage", CONFIG, {}, opener=opener) is None
    assert opener.called
    assert CONFIG["bot_token"] not in json.dumps(rc.default_state())
    assert not any(isinstance(handler, rc.urllib.request.ProxyHandler) and handler.proxies
                   for handler in rc._SLACK_OPENER.handlers)
    assert any(isinstance(handler, rc._RejectRedirects) for handler in rc._SLACK_OPENER.handlers)
