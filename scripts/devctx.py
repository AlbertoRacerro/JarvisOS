#!/usr/bin/env python3
"""Control-room recovery packet and typed control ledger (spec 154).

`recover` rebuilds development context from fresh Git/GitHub/STATUS/worktree/lane/runtime
truth and annotates recorded ledger entries with mechanical freshness verdicts. The ledger
holds only what cannot be derived (directives, decisions, findings, proof results, failed
attempts, blockers, claims). The packet is orientation, never authority or instructions.

Standard library only. Read-only towards the repository except an optional `git fetch`.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = "devctx.v1"
KINDS = ("directive", "decision", "finding", "proof", "attempt", "blocker", "question", "claim", "note")
MAX_TEXT = 2_000
MAX_DIRECTIVE_BYTES = 256_000
DEFAULT_BUDGET_CHARS = 12_000
CLAIM_TTL_HOURS = 12.0
AGENT_NAMES = ("claude", "codex", "agy", "gemini", "opencode")
MODEL_SERVERS = ("llama-server", "ollama")
SECRET_PATTERNS = [re.compile(p) for p in (
    r"gh[pousr]_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"\bsk-[A-Za-z0-9_-]{20,}",
    r"AKIA[0-9A-Z]{16}", r"-----BEGIN [A-Z ]*PRIVATE KEY", r"xox[abprs]-[A-Za-z0-9-]{10,}",
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.",
    r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd|bearer)\b\s*[:=]\s*['\"]?[^\s'\"]{8,}",
)]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def age(ts: datetime, now: datetime) -> str:
    seconds = max(0, int((now - ts).total_seconds()))
    for unit, size in (("d", 86_400), ("h", 3_600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def run(args: list[str], cwd: Path | None = None, timeout: float = 20.0) -> tuple[int | None, str]:
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False)
        return proc.returncode, proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)


def redact(text: str) -> str:
    return "[redacted: matches a secret pattern]" if find_secret(text) else text


def find_secret(text: str) -> str | None:
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return pattern.pattern
    return None


# ---------------------------------------------------------------------------
# Environment (injectable for tests)


@dataclass
class Env:
    repo: Path
    control: Path
    relay_sessions: Path | None
    gh: Callable[[list[str]], Any] | None = None
    processes: Callable[[], list[dict[str, Any]]] | None = None
    gpu: Callable[[], str | None] | None = None
    now: datetime = field(default_factory=now_utc)

    def git(self, *args: str, cwd: Path | None = None, timeout: float = 20.0) -> str | None:
        code, out = run(["git", *args], cwd=cwd or self.repo, timeout=timeout)
        return out.strip() if code == 0 else None

    @property
    def ledger(self) -> Path:
        return self.control / "ledger.jsonl"

    @property
    def directives(self) -> Path:
        return self.control / "directives"


def real_gh(args: list[str]) -> Any:
    code, out = run(["gh", *args], timeout=12.0)
    if code != 0:
        raise RuntimeError(out.strip()[:300] or "gh failed")
    return json.loads(out) if out.strip() else None


def real_processes(log_root: Path | None = None) -> list[dict[str, Any]]:
    """Agent/model-server processes, plus any process holding a lane log under `log_root` open.

    Excludes this process and its ancestors."""
    skip, pid = set(), os.getpid()
    while pid > 1:
        skip.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    rows = []
    boot = time.time() - float(Path("/proc/uptime").read_text().split()[0]) if Path("/proc/uptime").exists() else 0
    hertz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    for entry in Path("/proc").iterdir() if Path("/proc").exists() else []:
        if not entry.name.isdigit() or int(entry.name) in skip:
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            name = Path(argv[0].decode(errors="replace")).name if argv and argv[0] else ""
            logs = []
            if log_root is not None:
                # Any fd, not just stdout: pytest and similar tools re-point fd 1 at a capture file.
                for fd in (entry / "fd").iterdir():
                    target = os.readlink(fd)
                    if target.startswith(f"{log_root}/") and target.endswith(".log"):
                        logs.append(target)
            if name not in AGENT_NAMES + MODEL_SERVERS and not logs:
                continue
            stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            rows.append({
                "pid": int(entry.name), "ppid": int(stat[1]), "name": name,
                "start": int(stat[19]), "started_at": iso(datetime.fromtimestamp(boot + int(stat[19]) / hertz, timezone.utc)),
                "cwd": os.readlink(entry / "cwd"),
                "logs": sorted(set(logs)),
                "cmdline": " ".join(a.decode(errors="replace") for a in argv if a)[:4_000],
            })
        except (OSError, ValueError, IndexError):
            continue
    return rows


def real_gpu() -> str | None:
    code, out = run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
                     "--format=csv,noheader,nounits"], timeout=5.0)
    if code != 0 or not out.strip():
        return None
    used, total, util = (item.strip() for item in out.splitlines()[0].split(","))
    return f"{used}/{total} MiB used, {util}% util"


def pid_start(pid: int) -> int | None:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def default_env(repo: Path | None = None) -> Env:
    root = repo or Path(run(["git", "rev-parse", "--show-toplevel"])[1].strip() or ".")
    common = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=root)[1].strip()
    main_root = Path(common).parent if common else root
    control = Path(os.environ.get("JARVIS_CONTROL_DIR", Path.home() / "jarvis-control/work")).expanduser()
    relay = main_root / ".agent-relay" / "sessions"
    return Env(repo=root, control=control, relay_sessions=relay if relay.is_dir() else None,
               gh=real_gh, processes=lambda: real_processes(control / "out"), gpu=real_gpu)


# ---------------------------------------------------------------------------
# Ledger


def read_ledger(env: Env) -> list[dict[str, Any]]:
    if not env.ledger.exists():
        return []
    entries = []
    for line in env.ledger.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("schema") == SCHEMA:
            entries.append(item)
    return entries


def append_entry(env: Env, entry: dict[str, Any]) -> dict[str, Any]:
    env.control.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
    with open(env.ledger, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle, fcntl.LOCK_UN)
    return entry


def detect_author(procs: list[dict[str, Any]] | None = None) -> str:
    if os.environ.get("DEVCTX_AUTHOR"):
        return os.environ["DEVCTX_AUTHOR"][:120]
    pid = os.getppid()
    for _ in range(12):
        try:
            argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode(errors="replace")
            name = Path(argv0).name
            if name in AGENT_NAMES:
                return f"{name} pid {pid}"
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if pid <= 1:
            break
    return "unknown"


def dirty_digest(env: Env, tree: Path, paths: list[str]) -> str | None:
    """Digest of uncommitted tracked and untracked content under `paths`; None when clean."""
    diff = env.git("diff", "HEAD", "--binary", "--", *paths, cwd=tree) or ""
    untracked = (env.git("ls-files", "--others", "--exclude-standard", "--", *paths, cwd=tree) or "").splitlines()
    if not diff and not untracked:
        return None
    digest = hashlib.sha256(diff.encode())
    for name in sorted(untracked):
        digest.update(name.encode() + b"\0" + hashlib.sha256((tree / name).read_bytes()).digest())
    return digest.hexdigest()


def make_entry(env: Env, kind: str, text: str, *, status: str | None = None, refs: dict[str, Any] | None = None,
               supersedes: list[str] | None = None, conflicts: list[str] | None = None,
               claim: dict[str, Any] | None = None, directive: dict[str, Any] | None = None,
               author: str | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}")
    text = text.strip()
    if not text or len(text) > MAX_TEXT:
        raise ValueError(f"text must be 1..{MAX_TEXT} characters")
    if hit := find_secret(text):
        raise ValueError(f"text matches a credential/secret pattern ({hit}); refusing to record it")
    if status not in (None, "accepted", "proposed"):
        raise ValueError("status must be accepted or proposed")
    known = {item["id"] for item in read_ledger(env)}
    for ref in (supersedes or []) + (conflicts or []):
        if ref not in known:
            raise ValueError(f"unknown ledger id {ref}")
    clean_refs = {k: v for k, v in (refs or {}).items() if v not in (None, [], "")}
    if sha := clean_refs.get("sha"):
        full = env.git("rev-parse", "--verify", f"{sha}^{{commit}}")
        if not full:
            raise ValueError(f"sha {sha} is not a commit in this repository")
        clean_refs["sha"] = full
        if kind == "proof" and not any(clean_refs.get(k) for k in ("worktree", "pr", "branch")):
            clean_refs["worktree"] = str(env.repo)
        if tree := clean_refs.get("worktree"):
            if not Path(tree).is_dir():
                raise ValueError(f"worktree {tree} does not exist")
            clean_refs["worktree"] = str(Path(tree).resolve())
            if dirty := dirty_digest(env, Path(tree), clean_refs.get("paths") or []):
                clean_refs["dirty_digest"] = dirty
    return {
        "schema": SCHEMA, "id": f"L{env.now.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(2)}",
        "ts": iso(env.now), "kind": kind, "status": status, "text": text,
        "author": author or detect_author(), "refs": clean_refs,
        "supersedes": supersedes or [], "conflicts": conflicts or [],
        **({"claim": claim} if claim else {}), **({"directive": directive} if directive else {}),
    }


def record_directive(env: Env, content: str, *, source: str | None, supersedes: list[str] | None,
                     author: str | None = None) -> dict[str, Any]:
    raw = content.encode("utf-8")
    if not raw.strip() or len(raw) > MAX_DIRECTIVE_BYTES:
        raise ValueError(f"directive must be 1..{MAX_DIRECTIVE_BYTES} bytes")
    if hit := find_secret(content):
        raise ValueError(f"directive matches a credential/secret pattern ({hit}); refusing to record it")
    digest = hashlib.sha256(raw).hexdigest()
    title = " / ".join([line.strip() for line in content.splitlines() if line.strip()][:2])[:200]
    env.directives.mkdir(parents=True, exist_ok=True)
    path = env.directives / f"{env.now.strftime('%Y%m%dT%H%M%S')}-{digest[:12]}.md"
    if not any(p.name.endswith(f"-{digest[:12]}.md") for p in env.directives.iterdir()):
        path.write_bytes(raw)
        path.chmod(0o600)
    else:
        path = next(p for p in env.directives.iterdir() if p.name.endswith(f"-{digest[:12]}.md"))
    entry = make_entry(env, "directive", title or "(untitled directive)", status="accepted",
                       supersedes=supersedes, author=author,
                       directive={"path": str(path), "sha256": digest, "bytes": len(raw), "source": source})
    return append_entry(env, entry)


# ---------------------------------------------------------------------------
# Derived state collectors


def fetch_origin(env: Env, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {"attempted": False}
    code, out = run(["git", "fetch", "--quiet", "--prune", "origin"], cwd=env.repo, timeout=20.0)
    return {"attempted": True, "ok": code == 0, "error": None if code == 0 else out.strip()[:300]}


def master_state(env: Env) -> dict[str, Any]:
    line = env.git("log", "-1", "--format=%H%x00%cI%x00%s", "origin/master")
    if not line:
        return {"sha": None}
    sha, when, subject = line.split("\x00", 2)
    fetch_head = env.git("rev-parse", "--path-format=absolute", "--git-common-dir")
    fetched_at = None
    if fetch_head and (Path(fetch_head) / "FETCH_HEAD").exists():
        fetched_at = iso(datetime.fromtimestamp((Path(fetch_head) / "FETCH_HEAD").stat().st_mtime, timezone.utc))
    return {"sha": sha, "committed_at": when, "subject": subject[:120], "fetched_at": fetched_at}


def github_state(env: Env) -> dict[str, Any]:
    if env.gh is None:
        return {"ok": False, "error": "gh disabled", "open": [], "merged": []}
    try:
        opened = env.gh(["pr", "list", "--state", "open", "--limit", "30", "--json",
                         "number,title,headRefName,headRefOid,isDraft,mergeable,statusCheckRollup,updatedAt"]) or []
        merged = env.gh(["pr", "list", "--state", "merged", "--limit", "500", "--json",
                         "number,title,mergedAt,headRefName,headRefOid"]) or []
    except (RuntimeError, ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)[:300], "open": [], "merged": []}
    for pr in opened:
        counts: dict[str, int] = {}
        for check in pr.pop("statusCheckRollup", None) or []:
            state = check.get("conclusion") or check.get("state") or check.get("status") or "UNKNOWN"
            counts[state] = counts.get(state, 0) + 1
        pr["checks"] = counts
    return {"ok": True, "error": None, "open": opened, "merged": merged}


def pr_lookup(env: Env, gh_state: dict[str, Any], number: int,
              cache: dict[int, dict[str, Any] | None]) -> dict[str, Any] | None:
    if number in cache:
        return cache[number]
    found = next((dict(pr, state="OPEN") for pr in gh_state["open"] if pr["number"] == number), None)
    # Merged PRs are already listed; one `gh pr view` per old ledger reference dominated recover time.
    found = found or next((dict(pr, state="MERGED") for pr in gh_state["merged"] if pr["number"] == number), None)
    if found is None and gh_state["ok"] and env.gh is not None:
        try:
            found = env.gh(["pr", "view", str(number), "--json", "number,state,headRefName,headRefOid,mergeCommit,mergedAt"])
        except (RuntimeError, ValueError, OSError):
            found = None
    cache[number] = found
    return found


STATUS_ROW = re.compile(r"^\| (\w+) \| (\w+) \| ([^|]*) \| ([^|]*) \| ([^|]*) \|")


def status_state(env: Env) -> dict[str, Any]:
    text = env.git("show", "origin/master:docs/specs/STATUS.md")
    if text is None:
        return {"ok": False, "rows": []}
    rows = []
    for line in text.splitlines():
        match = STATUS_ROW.match(line)
        if match and match.group(1) != "Spec":
            spec, status, pr, name, deps = (g.strip() for g in match.groups())
            rows.append({"spec": spec, "status": status, "prs": re.findall(r"#(\d+)", pr), "name": name,
                         "deps": [d.strip() for d in deps.split(",") if d.strip() and d.strip() != "—"]})
    merged = {row["spec"] for row in rows if row["status"] == "merged"}
    for row in rows:
        row["deps_merged"] = all(dep in merged for dep in row["deps"])
    return {"ok": True, "rows": rows}


def worktree_state(env: Env, gh_state: dict[str, Any]) -> list[dict[str, Any]]:
    porcelain = env.git("worktree", "list", "--porcelain") or ""
    trees: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in porcelain.splitlines() + [""]:
        if not line:
            if current:
                trees.append(current)
            current = {}
        elif line.startswith("worktree "):
            current["path"] = line[9:]
        elif line.startswith("HEAD "):
            current["head"] = line[5:]
        elif line.startswith("branch "):
            current["branch"] = line[7:].removeprefix("refs/heads/")
        elif line in ("detached", "prunable") or line.startswith("prunable"):
            current[line.split()[0]] = True
    open_by_branch = {pr["headRefName"]: pr["number"] for pr in gh_state["open"]}
    merged_heads: dict[str, list[tuple[int, str]]] = {}
    for pr in gh_state["merged"]:
        merged_heads.setdefault(pr["headRefName"], []).append((pr["number"], pr["headRefOid"]))
    for tree in trees:
        path = Path(tree["path"])
        if not path.exists():
            tree["missing"] = True
            continue
        status = env.git("status", "--porcelain", cwd=path)
        tree["dirty"] = len(status.splitlines()) if status else 0
        head: str | None = tree.get("head")
        branch: str | None = tree.get("branch")
        tree["merged"] = head is not None and env.git("merge-base", "--is-ancestor", head, "origin/master") is not None
        for number, merged_head in merged_heads.get(branch or "", []):
            # Squash/rebase merges leave the PR head outside master's ancestry.
            if not tree["merged"] and head and (head == merged_head or env.git(
                    "merge-base", "--is-ancestor", head, merged_head) is not None):
                tree["merged"], tree["merged_pr"] = True, number
        upstream = f"origin/{branch}" if branch and env.git("rev-parse", "--verify", "-q", f"origin/{branch}") else None
        tree["unpushed"] = int(env.git("rev-list", "--count", f"{upstream}..{head}") or 0) if upstream else None
        tree["pr"] = open_by_branch.get(branch or "")
        last = env.git("log", "-1", "--format=%cI", head or "HEAD", cwd=path)
        tree["last_commit_at"] = last
    return trees


def lane_state(env: Env, procs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lanes: dict[str, dict[str, Any]] = {}
    out = env.control / "out"
    dirs = [out, *sorted(p for p in out.iterdir() if p.is_dir())] if out.is_dir() else []
    for folder in dirs:
        for path in folder.iterdir():
            name = path.name
            match = (re.fullmatch(r"(.+)\.report\.md", name) or re.fullmatch(r"(.+)\.done", name)
                     or re.fullmatch(r"(.+)\.a\d+\.log", name))
            if not match or not path.is_file():
                continue
            lane = lanes.setdefault(f"{folder.name}/{match.group(1)}", {
                "lane": match.group(1), "dir": str(folder), "report_bytes": 0, "done": False, "last_write": 0.0})
            lane["last_write"] = max(lane["last_write"], path.stat().st_mtime)
            if name.endswith(".report.md"):
                lane["report_bytes"] = path.stat().st_size
            elif name.endswith(".done"):
                lane["done"] = True
                codes = re.findall(r"EXIT (-?\d+)", path.read_text(encoding="utf-8", errors="replace"))
                lane["exit_code"] = int(codes[-1]) if codes else None
    for lane in lanes.values():
        token = re.compile(rf"(?<![\w.-]){re.escape(lane['lane'])}\.report\.md")
        log = re.compile(rf"/{re.escape(lane['lane'])}\.a\d+\.log$")
        live = [p for p in procs if (p["name"] in AGENT_NAMES and token.search(p["cmdline"]))
                or any(log.search(item) for item in p.get("logs", []))]
        lane["pids"] = [p["pid"] for p in live]
        worktree = next((m.group(1) for p in live if (m := re.search(r"\s-C\s+(\S+)", p["cmdline"]))), None)
        lane["worktree"] = worktree or (live[0]["cwd"] if live else None)
        if live:
            lane["state"] = "running"
        elif lane["done"]:
            if lane["report_bytes"]:
                lane["state"] = "done"
            elif lane.get("exit_code") == 0:
                lane["state"] = "done-log-only"  # e.g. a test-suite lane whose evidence is its log
            else:
                lane["state"] = "failed-empty-report" if lane.get("exit_code") is None else f"failed-exit-{lane['exit_code']}"
        else:
            lane["state"] = "incomplete-no-process" if not lane["report_bytes"] else "report-without-done-marker"
        lane["last_write_at"] = iso(datetime.fromtimestamp(lane.pop("last_write"), timezone.utc))
    return sorted(lanes.values(), key=lambda item: item["last_write_at"], reverse=True)


def relay_prompts(env: Env, since: datetime | None, limit: int = 3) -> list[dict[str, Any]]:
    if env.relay_sessions is None:
        return []
    found: list[dict[str, Any]] = []
    for prompt in env.relay_sessions.glob("*/turns/*/prompt.md"):
        stat = prompt.stat()
        when = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        if stat.st_size < 800 or (since and when <= since):
            continue
        text = prompt.read_text(encoding="utf-8", errors="replace")
        task = text.split("## Task", 1)[1] if "You are participating in a Relay-managed" in text and "## Task" in text else text
        title = next((line.strip() for line in task.splitlines() if line.strip()), "")[:140]
        found.append({"path": str(prompt), "at": iso(when), "bytes": stat.st_size, "title": redact(title)})
    return sorted(found, key=lambda item: item["at"], reverse=True)[:limit]


# ---------------------------------------------------------------------------
# Freshness


def resolve_target(env: Env, refs: dict[str, Any], gh_state: dict[str, Any], cache: dict[int, Any]) -> tuple[str | None, str]:
    if pr_ref := refs.get("pr"):
        pr = pr_lookup(env, gh_state, int(pr_ref), cache)
        if pr is None:
            return None, f"PR #{pr_ref} state unavailable"
        if pr.get("state") == "MERGED":
            return "origin/master", f"PR #{pr_ref} merged; compared with origin/master"
        if pr.get("state") == "OPEN":
            return pr.get("headRefOid"), f"PR #{pr_ref} open head {str(pr.get('headRefOid'))[:8]}"
        return None, f"PR #{pr_ref} {str(pr.get('state')).lower()}"
    if tree := refs.get("worktree"):
        head = env.git("rev-parse", "HEAD", cwd=Path(tree)) if Path(tree).exists() else None
        return (head, f"worktree {Path(tree).name} HEAD {head[:8]}") if head else (None, f"worktree {tree} missing")
    if branch := refs.get("branch"):
        for ref in (f"origin/{branch}", branch):
            if head := env.git("rev-parse", "--verify", "-q", ref):
                return head, f"{ref} {head[:8]}"
        return None, f"branch {branch} not found"
    return "origin/master", "compared with origin/master"


def freshness(env: Env, entry: dict[str, Any], gh_state: dict[str, Any], cache: dict[int, Any],
              procs: list[dict[str, Any]], refs_fresh: bool = True) -> dict[str, Any]:
    result = _freshness(env, entry, gh_state, cache, procs)
    if not refs_fresh and result["verdict"] in ("current", "stale") and "origin/" in result["why"]:
        # A comparison against last-fetched remote refs is not evidence about the current remote.
        return {**result, "verdict": "unverified",
                "why": f"origin refs not refreshed (fetch skipped/failed); last-fetched comparison said "
                       f"{result['verdict']}: {result['why']}"}
    return result


def _freshness(env: Env, entry: dict[str, Any], gh_state: dict[str, Any], cache: dict[int, Any],
               procs: list[dict[str, Any]]) -> dict[str, Any]:
    refs = entry.get("refs", {})
    if entry["kind"] == "claim":
        claim = entry.get("claim", {})
        expired = env.now > parse_ts(claim.get("expires", entry["ts"]))
        pid = claim.get("pid")
        alive = pid is None or (claim.get("pid_start") is not None and pid_start(pid) == claim.get("pid_start"))
        if expired or not alive:
            return {"verdict": "expired", "why": "TTL passed" if expired else f"holder pid {pid} is gone"}
        return {"verdict": "current", "why": f"held until {claim.get('expires')}"}
    sha = refs.get("sha")
    if not sha:
        if pr_ref := refs.get("pr"):
            pr = pr_lookup(env, gh_state, int(pr_ref), cache)
            state = (pr or {}).get("state", "unknown")
            return {"verdict": "unverified", "why": f"no SHA binding; PR #{pr_ref} is now {state}"}
        return {"verdict": "unverified", "why": "no SHA binding"}
    target, how = resolve_target(env, refs, gh_state, cache)
    if not target:
        return {"verdict": "unverified", "why": how}
    if env.git("cat-file", "-e", f"{target}^{{commit}}") is None:
        return {"verdict": "unverified", "why": f"{how}; target commit not present locally"}
    paths = refs.get("paths") or []
    changed = env.git("diff", "--name-only", sha, target, "--", *paths)
    if changed is None:
        return {"verdict": "unverified", "why": f"{how}; diff failed"}
    files = [line for line in changed.splitlines() if line]
    scope = "bound paths" if paths else "whole tree"
    if tree := refs.get("worktree"):
        now_dirty = dirty_digest(env, Path(tree), paths) if Path(tree).is_dir() else None
        if now_dirty != refs.get("dirty_digest"):
            status = env.git("status", "--porcelain", "--", *paths, cwd=Path(tree)) or ""
            files += [f"(uncommitted) {line[3:]}" for line in status.splitlines()] or [
                "(uncommitted state recorded with the evidence is gone)"]
    if not files:
        return {"verdict": "current", "why": f"{scope} unchanged since {sha[:8]}; {how}"}
    return {"verdict": "stale", "why": f"{len(files)} changed in {scope} since {sha[:8]}; {how}", "changed": files[:8]}


def directive_provenance(entry: dict[str, Any], relay_texts: list[tuple[str, str]]) -> str:
    """A directive is maintainer-verified only when Relay's verbatim prompt store contains it."""
    path = Path(entry["directive"]["path"])
    if not path.exists():
        return "directive file missing"
    body = path.read_text(encoding="utf-8", errors="replace").strip()
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry["directive"]["sha256"]:
        return "directive file modified after recording"
    for name, text in relay_texts:
        if body and body in text:
            return f"relay-verified ({name})"
    return f"unverified source (recorded by {entry['author']}; source {entry['directive'].get('source') or 'unstated'})"


# ---------------------------------------------------------------------------
# Packet


def fmt_refs(refs: dict[str, Any]) -> str:
    return " ".join(f"{k}={v[:8] if k == 'sha' else ','.join(v) if k == 'paths' else v}"
                    for k, v in refs.items() if k != "dirty_digest")


def build_packet(env: Env, *, fetch: bool = True) -> dict[str, Any]:
    started = time.monotonic()
    fetched = fetch_origin(env, fetch)
    procs = env.processes() if env.processes else []
    gh_state = github_state(env)
    status = status_state(env)
    ledger = read_ledger(env)
    superseded = {ref for item in ledger for ref in item.get("supersedes", [])}
    cache: dict[int, Any] = {}
    active = [item for item in ledger if item["id"] not in superseded]
    relay_texts = None
    refs_fresh = bool(fetched.get("ok"))
    for item in active:
        item["freshness"] = freshness(env, item, gh_state, cache, procs, refs_fresh)
        if item.get("directive"):
            if relay_texts is None:
                relay_texts = [(str(p), p.read_text(encoding="utf-8", errors="replace"))
                               for p in env.relay_sessions.glob("*/turns/*/prompt.md")] if env.relay_sessions else []
            item["provenance"] = directive_provenance(item, relay_texts)
    active_ids = {item["id"] for item in active}
    conflicts = [(item["id"], ref) for item in active for ref in item.get("conflicts", []) if ref in active_ids]
    last_ledger_ts = parse_ts(ledger[-1]["ts"]) if ledger else None
    directives = [item for item in active if item["kind"] == "directive"]
    last_directive_ts = parse_ts(directives[-1]["ts"]) if directives else None
    recorded_digests = {item["directive"]["sha256"] for item in ledger if item.get("directive")}
    unrecorded = [p for p in relay_prompts(env, last_directive_ts)
                  if hashlib.sha256(Path(p["path"]).read_bytes()).hexdigest() not in recorded_digests]
    progress = env.control / "PROGRESS.md"
    progress_note = None
    if progress.exists():
        mtime = datetime.fromtimestamp(progress.stat().st_mtime, timezone.utc)
        if last_ledger_ts is None or mtime > last_ledger_ts + timedelta(seconds=5):
            heads = [line for line in progress.read_text(encoding="utf-8", errors="replace").splitlines() if line.startswith("## ")]
            progress_note = {"path": str(progress), "modified_at": iso(mtime), "last_section": heads[-1][3:120] if heads else None}
    handoff_kinds = ("note", "decision", "proof", "claim", "blocker", "question")
    handoff = next((e for e in reversed(active) if e["kind"] in handoff_kinds
                    and not str(e.get("author", "")).startswith("relay-")), None)  # relay-* lines are automation
    trees = worktree_state(env, gh_state)
    lanes = lane_state(env, procs)
    open_prs = gh_state["open"]
    in_flight = [row for row in status["rows"] if row["status"] in ("in_progress", "in_review", "blocked")]
    ready = [row for row in status["rows"] if row["status"] == "ready"]
    candidates = []
    for pr in open_prs:
        checks = pr.get("checks", {})
        bad = sum(v for k, v in checks.items() if k in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT"))
        pending = sum(v for k, v in checks.items() if k in ("PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED"))
        verb = "repair failing checks on" if bad else "wait for checks on" if pending else \
            "complete evidence / undraft" if pr.get("isDraft") else "merge decision for"
        candidates.append(f"{verb} PR #{pr['number']} ({pr['title'][:70]})")
    open_numbers = {str(pr["number"]) for pr in open_prs}
    for row in in_flight:
        if row["status"] == "in_review" and not set(row["prs"]) & open_numbers:
            candidates.append(f"spec {row['spec']} is in_review but none of its PRs is open: reconcile STATUS")
    for row in ready:
        if row["deps_merged"] and not set(row["prs"]) & open_numbers:
            linked = [pr["number"] for pr in open_prs if re.search(rf"/{re.escape(row['spec'])}-", pr["headRefName"])]
            candidates.append(f"spec {row['spec']} {row['name']} is ready with dependencies merged"
                              + (f"; open PR(s) {linked} on its branch likely implement it" if linked else ""))
    for lane in lanes:
        if lane["state"] == "running":
            candidates.append(f"lane {lane['lane']} is running (pids {lane['pids']}): consume its report when done")
    for item in active:
        if item["kind"] in ("blocker", "question"):
            candidates.append(f"open {item['kind']} {item['id']}: {item['text'][:90]}")
    warnings = []
    if fetched.get("attempted") and not fetched.get("ok"):
        warnings.append(f"git fetch FAILED ({fetched['error']}); origin/master is last-fetched state, NOT verified current")
    if not fetched.get("attempted"):
        warnings.append("git fetch skipped; origin/master is last-fetched state, NOT verified current")
    if not gh_state["ok"]:
        warnings.append(f"GitHub UNVERIFIED ({gh_state['error']}); PR state and PR-bound freshness unknown")
    if not ledger:
        warnings.append(f"no control ledger at {env.ledger}; packet is derived state only (no recorded decisions/findings)")
    if progress_note:
        warnings.append(f"PROGRESS.md was modified after the last ledger entry ({progress_note['modified_at']}): "
                        f"another writer may have recorded state there; read its last section '{progress_note['last_section']}'")
    for left, right in conflicts:
        warnings.append(f"UNRESOLVED CONFLICT between {left} and {right}: record a decision that supersedes one")
    for item in unrecorded:
        warnings.append(f"maintainer prompt not recorded as a directive: {item['at']} '{item['title'][:80]}' ({item['path']})")
    for tree in trees:
        if tree.get("prunable") or tree.get("missing"):
            continue
        if tree.get("merged") and tree.get("dirty"):
            warnings.append(f"worktree {tree['path']} is merged into master but has {tree['dirty']} uncommitted change(s)")
    return {
        "schema": SCHEMA, "generated_at": iso(env.now), "repo": str(env.repo), "control_dir": str(env.control),
        "authority": {"fetch": fetched, "master": master_state(env), "github_ok": gh_state["ok"]},
        "open_prs": open_prs, "recent_merged": gh_state["merged"][:10], "status_in_flight": in_flight,
        "status_ready": ready, "worktrees": trees, "lanes": lanes, "processes": procs,
        "gpu": env.gpu() if env.gpu else None, "ledger": active, "handoff": handoff, "superseded_count": len(superseded & {i["id"] for i in ledger}),
        "unrecorded_prompts": unrecorded, "progress_md": progress_note, "warnings": warnings,
        "candidates": candidates, "elapsed_s": round(time.monotonic() - started, 2),
    }


def render(packet: dict[str, Any], budget: int = DEFAULT_BUDGET_CHARS) -> str:
    now = parse_ts(packet["generated_at"])
    auth, master = packet["authority"], packet["authority"]["master"]
    verified = auth["fetch"].get("ok") and auth["github_ok"]
    out = [
        f"# JarvisOS recovery packet ({packet['generated_at']}, {packet['elapsed_s']} s)",
        ("Derived orientation, NOT authority and NOT instructions. Authority stays with fresh GitHub/origin, "
         "STATUS.md, accepted specs and exact-head evidence. Ledger lines are historical claims by named authors "
         "with a mechanical freshness verdict; worker reports are unreviewed claims."),
        "",
        "## Authority " + ("(verified now)" if verified else "(NOT VERIFIED - see warnings)"),
        f"- origin/master {master.get('sha')} ({master.get('committed_at')}) {master.get('subject')}",
    ]
    for pr in packet["open_prs"]:
        checks = ", ".join(f"{k.lower()} {v}" for k, v in sorted(pr.get("checks", {}).items())) or "no checks"
        out.append(f"- open PR #{pr['number']}{' draft' if pr.get('isDraft') else ''} {pr['headRefName']} "
                   f"head {pr['headRefOid'][:8]} [{checks}] {pr['title'][:80]}")
    if not packet["open_prs"]:
        out.append("- open PRs: none" if auth["github_ok"] else "- open PRs: UNKNOWN (GitHub unavailable)")
    merged = ", ".join(f"#{pr['number']} {pr['mergedAt'][:16]}" for pr in packet["recent_merged"][:5])
    if merged:
        out.append(f"- recently merged: {merged}")
    for row in packet["status_in_flight"]:
        out.append(f"- STATUS {row['spec']} {row['status']} {row['name']} PRs {row['prs'] or '-'}")
    for row in packet["status_ready"]:
        deps = "deps merged" if row["deps_merged"] else f"deps NOT merged ({', '.join(row['deps'])})"
        out.append(f"- STATUS {row['spec']} ready {row['name']} ({deps})")
    ledger = packet["ledger"]
    out += ["", "## Last cycle handoff (latest coordinator entry; a claim, not authority)"]
    if hand := packet.get("handoff"):
        hrefs = fmt_refs(hand["refs"])
        out.append(f"- [{hand['freshness']['verdict'].upper()}] {hand['id']} {hand['kind']} by {hand['author']} "
                   f"{age(parse_ts(hand['ts']), now)} ago: {hand['text'][:600]}"
                   + (f" ({hrefs})" if hrefs else "") + f" -- {hand['freshness']['why']}")
    else:
        out.append("- none recorded")
    directives = [e for e in ledger if e["kind"] == "directive"]
    out += ["", "## Maintainer directives (latest first)"]
    for entry in reversed(directives[-4:]):
        meta = entry["directive"]
        out.append(f"- [{entry.get('provenance', 'unchecked')}] {entry['id']} {entry['ts']} "
                   f"({age(parse_ts(entry['ts']), now)} ago) \"{entry['text'][:160]}\" -> {meta['path']} ({meta['bytes']} B)")
    if not directives:
        out.append("- none recorded")
    if len(directives) > 4:
        out.append(f"- (+{len(directives) - 4} older: `devctx.py search` / `show`)")
    groups = [("Claims (single-writer)", ("claim",)), ("Proofs / tests", ("proof",)),
              ("Decisions", ("decision",)), ("Blockers / questions", ("blocker", "question")),
              ("Findings, failed attempts, notes", ("finding", "attempt", "note"))]
    caps = {"claim": 8, "proof": 10, "decision": 10, "blocker": 8, "finding": 12}
    for title, kinds in groups:
        items = [e for e in ledger if e["kind"] in kinds]
        if not items:
            continue
        out += ["", f"## {title}"]
        cap = caps.get(kinds[0], 10)
        for entry in reversed(items[-cap:]):
            fresh = entry["freshness"]
            status = f" {entry['status']}" if entry.get("status") else ""
            refs = fmt_refs(entry["refs"])
            out.append(f"- [{fresh['verdict'].upper()}] {entry['id']} {entry['kind']}{status} by {entry['author']} "
                       f"{age(parse_ts(entry['ts']), now)} ago: {entry['text'][:220]}"
                       + (f" ({refs})" if refs else "") + f" -- {fresh['why']}")
        if len(items) > cap:
            out.append(f"- (+{len(items) - cap} older {title.lower()})")
    if packet["superseded_count"]:
        out.append(f"\n({packet['superseded_count']} superseded ledger entries hidden)")
    trees = [t for t in packet["worktrees"] if not t.get("missing") and not t.get("prunable")
             and (t.get("dirty") or t.get("pr") or t.get("unpushed") or not t.get("merged"))]
    out += ["", "## Worktrees with unmerged, unpushed, dirty or PR-linked state"]
    for tree in trees[:14]:
        bits = [tree.get("branch") or "detached", (tree.get("head") or "")[:8]]
        if tree.get("merged"):
            bits.append("merged")
        if tree.get("dirty"):
            bits.append(f"{tree['dirty']} uncommitted")
        if tree.get("unpushed"):
            bits.append(f"{tree['unpushed']} unpushed")
        elif tree.get("unpushed") is None and not tree.get("merged"):
            bits.append("no remote branch")
        if tree.get("pr"):
            bits.append(f"PR #{tree['pr']}")
        if tree.get("last_commit_at"):
            bits.append(f"last commit {age(parse_ts(tree['last_commit_at']), now)} ago")
        out.append(f"- {tree['path']}: {', '.join(bits)}")
    if len(trees) > 14:
        out.append(f"- (+{len(trees) - 14} more)")
    out.append(f"- ({len(packet['worktrees']) - len(trees)} other worktrees are clean and merged)")
    lanes = packet["lanes"]
    recent = [l for l in lanes if l["state"] != "done" or (now - parse_ts(l["last_write_at"])) < timedelta(hours=24)]
    out += ["", "## Worker lanes (running, unfinished, or finished in 24 h; reports are unreviewed claims)"]
    for lane in recent[:14]:
        where = f" in {lane['worktree']}" if lane.get("worktree") else ""
        out.append(f"- {lane['lane']} {lane['state']}{where}, last write {age(parse_ts(lane['last_write_at']), now)} ago, "
                   f"report {lane['report_bytes']} B ({lane['dir']})")
    out.append(f"- ({len(lanes) - min(len(recent), 14)} older lanes: `devctx.py lanes`)")
    out += ["", "## Runtime"]
    for proc in packet["processes"]:
        out.append(f"- {proc['name']} pid {proc['pid']} since {proc['started_at'][:16]} cwd {proc['cwd']}")
    out.append(f"- GPU: {packet['gpu'] or 'unavailable'}")
    out += ["", "## Warnings"] + ([f"- {w}" for w in packet["warnings"]] or ["- none"])
    out += ["", "## Next-step candidates (derived from state; verify before acting)"]
    out += [f"- {c}" for c in packet["candidates"]] or ["- none derived; check the latest directive"]
    out += ["", ("Drill down: `python3 scripts/devctx.py show <id>|lanes|search <text>`. "
                 "Record: `devctx.py note <kind> ...`, `devctx.py directive --file <prompt>`.")]
    text = "\n".join(out)
    if len(text) > budget:
        text = text[:budget - 120].rsplit("\n", 1)[0] + f"\n... (truncated at {budget} chars; use --max-chars or --json)"
    return text


# ---------------------------------------------------------------------------
# CLI


def command_note(env: Env, args: argparse.Namespace) -> None:
    refs = {"pr": args.pr, "sha": args.sha, "branch": args.branch, "worktree": args.worktree,
            "lane": args.lane, "spec": args.spec, "paths": args.path, "evidence": args.evidence}
    claim = None
    if args.kind == "claim":
        pid = args.pid
        if pid and pid_start(pid) is None:
            raise ValueError(f"claim holder pid {pid} is not running")
        claim = {"expires": iso(env.now + timedelta(hours=args.ttl_hours)), "pid": pid,
                 "pid_start": pid_start(pid) if pid else None}
    entry = make_entry(env, args.kind, args.text, status=args.status, refs=refs, supersedes=args.supersedes,
                       conflicts=args.conflicts, claim=claim)
    append_entry(env, entry)
    print(entry["id"])


def command_show(env: Env, args: argparse.Namespace) -> None:
    entry = next((item for item in read_ledger(env) if item["id"] == args.id), None)
    if entry is None:
        raise SystemExit(f"no ledger entry {args.id}")
    packet_gh = github_state(env)
    procs = env.processes() if env.processes else []
    entry["freshness"] = freshness(env, entry, packet_gh, {}, procs, fetch_origin(env, True).get("ok", False))
    print(json.dumps(entry, indent=2, ensure_ascii=False))
    if entry.get("directive"):
        path = Path(entry["directive"]["path"])
        body = path.read_text(encoding="utf-8", errors="replace") if path.exists() else "(directive file missing)"
        print(f"\n--- {path} ---\n{body[:args.max_chars]}")


def command_search(env: Env, args: argparse.Namespace) -> None:
    needle = args.text.lower()
    hits = []
    for item in read_ledger(env):
        if needle in json.dumps(item, ensure_ascii=False).lower():
            hits.append(f"ledger {item['id']} {item['kind']} {item['ts']}: {item['text'][:160]}")
    files = sorted(env.directives.glob("*.md")) if env.directives.exists() else []
    out = env.control / "out"
    files += sorted(out.glob("*.report.md")) + sorted(out.glob("*/*.report.md")) if out.exists() else []
    files += [env.control / "PROGRESS.md"] if (env.control / "PROGRESS.md").exists() else []
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if needle in line.lower():
                hits.append(f"{path}:{number}: {redact(line.strip()[:160])}")
    print("\n".join(hits[:args.limit]) or "no matches")
    if len(hits) > args.limit:
        print(f"... {len(hits) - args.limit} more")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, help="repository root (default: current git toplevel)")
    sub = parser.add_subparsers(dest="action", required=True)
    rec = sub.add_parser("recover", help="print the bounded recovery packet")
    rec.add_argument("--json", action="store_true")
    rec.add_argument("--no-fetch", action="store_true", help="do not git fetch (packet marks authority unverified)")
    rec.add_argument("--max-chars", type=int, default=DEFAULT_BUDGET_CHARS)
    note = sub.add_parser("note", help="append a typed ledger entry")
    note.add_argument("kind", choices=[k for k in KINDS if k != "directive"])
    note.add_argument("text")
    note.add_argument("--status", choices=("accepted", "proposed"))
    note.add_argument("--pr", type=int)
    note.add_argument("--sha")
    note.add_argument("--branch")
    note.add_argument("--worktree")
    note.add_argument("--lane")
    note.add_argument("--spec")
    note.add_argument("--path", action="append", default=[], help="bind freshness to these repo paths (repeatable)")
    note.add_argument("--evidence", help="path of the evidence artifact")
    note.add_argument("--supersedes", action="append", default=[])
    note.add_argument("--conflicts", action="append", default=[])
    note.add_argument("--pid", type=int, help="claim holder pid (claim expires when it exits)")
    note.add_argument("--ttl-hours", type=float, default=CLAIM_TTL_HOURS)
    dire = sub.add_parser("directive", help="record a maintainer directive verbatim")
    dire.add_argument("text", nargs="?")
    dire.add_argument("--file", type=Path)
    dire.add_argument("--source", help="where the directive came from (e.g. Relay session id)")
    dire.add_argument("--supersedes", action="append", default=[])
    show = sub.add_parser("show", help="show one ledger entry with its current freshness")
    show.add_argument("id")
    show.add_argument("--max-chars", type=int, default=20_000)
    lanes = sub.add_parser("lanes", help="list all worker lanes")
    lanes.add_argument("--json", action="store_true")
    search = sub.add_parser("search", help="search ledger, directives, lane reports and legacy PROGRESS")
    search.add_argument("text")
    search.add_argument("--limit", type=int, default=30)
    args = parser.parse_args(argv)
    env = default_env(args.repo)
    try:
        if args.action == "recover":
            packet = build_packet(env, fetch=not args.no_fetch)
            print(json.dumps(packet, indent=1, ensure_ascii=False) if args.json else render(packet, args.max_chars))
        elif args.action == "note":
            command_note(env, args)
        elif args.action == "directive":
            if bool(args.file) == bool(args.text):
                raise ValueError("give either TEXT or --file")
            content = args.file.read_text(encoding="utf-8") if args.file else args.text
            print(record_directive(env, content, source=args.source, supersedes=args.supersedes)["id"])
        elif args.action == "show":
            command_show(env, args)
        elif args.action == "lanes":
            rows = lane_state(env, env.processes() if env.processes else [])
            print(json.dumps(rows, indent=1) if args.json else "\n".join(
                f"{r['lane']:<16} {r['state']:<26} {r['last_write_at']} report {r['report_bytes']:>6} B {r['dir']}" for r in rows))
        elif args.action == "search":
            command_search(env, args)
    except ValueError as exc:
        print(f"devctx: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
