"""Interrupted external operation probes for relay_continuity (spec 265)."""
from __future__ import annotations

import importlib.util
import json
import re
import shlex
import sys
from collections.abc import Callable
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
    CI_FAILURE_CONCLUSIONS,
    CI_SUCCESS_CONCLUSIONS,
    CI_WAIT_LINE_RE,
    FORBIDDEN_WAIT_CHARS,
    JOB_WAIT_LINE_RE,
    devctx,
    host_ops,
    load_sibling,
    relay_jobs,
)

load_sibling("relay_continuity_turns", "relay_continuity_turns.py")
from relay_continuity_turns import (  # noqa: E402
    ToolCall,
    classify_command,
    last_turn_dir,
    read_events,
    split_first_word,
    split_segments,
)

# ---------------------------------------------------------------------------
# Interrupted external operations (spec 197 as amended): settled only by deterministic read-only probes

VERDICTS = ("completed", "running", "not_started", "unresolved", "insufficient_permissions")
SETTLED = ("completed", "not_started")
VERDICT_TEXT = {"completed": "COMPLETED: it took effect; do not repeat it",
                "not_started": "NOT STARTED: it did not take effect; perform it only if still required after recover"}
PARKED_TEXT = ("UNRESOLVED (parked): it may have taken effect; never repeat it, "
               "verify its effect from Git, GitHub, host receipts or the ledger, "
               "and park only the work that depends on it")
PERMISSION_RE = re.compile(r"(?i)permission denied|could not read username|authentication failed|gh auth login|"
                           r"not logged in|requires authentication|resource not accessible|HTTP 40[13]\b")
PUSH_FLAGS = {"-u", "--set-upstream", "-q", "--quiet", "-v", "--verbose", "--no-verify", "--porcelain", "--atomic",
              "--progress", "--no-progress", "-f", "--force", "--force-if-includes"}
GH_VALUE_OPTS = {  # per subcommand: `gh pr merge -m` is --merge, `gh pr create -m` takes a milestone
    "merge": {"-b", "--body", "-F", "--body-file", "-t", "--subject", "-A", "--author-email", "--match-head-commit",
              "-R", "--repo"},
    "create": {"-b", "--body", "-F", "--body-file", "-t", "--title", "-R", "--repo", "-H", "--head", "-B", "--base",
               "-T", "--template", "-a", "--assignee", "-l", "--label", "-m", "--milestone", "-p", "--project",
               "-r", "--reviewer"},
}
Runner = Callable[..., tuple[int | None, str]]


def final_text(turn_dir: Path) -> str:
    """The agent's last top-level reply: Codex's `--output-last-message` file, else Claude's `result` or
    Codex's last `agent_message` event."""
    try:
        return (turn_dir / "last-message.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    events, _ = read_events(turn_dir / "output.jsonl")
    text = ""
    for e in events:
        if e.get("type") == "result" and not e.get("parent_tool_use_id"):
            text = str(e.get("result") or "")
        elif e.get("type") == "item.completed" and (e.get("item") or {}).get("type") == "agent_message":
            text = str(e["item"].get("text") or "")
    return text


def parse_wait_directives(text: str) -> tuple[list[str], list[tuple[int, str]]]:
    """Parse strict JOB-WAIT and CI-WAIT directives from text, deduplicated.
    Reject lines with backticks, shell operators, or non-matching formats."""
    jobs: list[str] = []
    ci_waits: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("JOB-WAIT:"):
            if any(ch in line for ch in FORBIDDEN_WAIT_CHARS):
                continue
            m = JOB_WAIT_LINE_RE.match(line)
            if m:
                jobs.append(m.group(1))
        elif line.startswith("CI-WAIT:"):
            if any(ch in line for ch in FORBIDDEN_WAIT_CHARS):
                continue
            m = CI_WAIT_LINE_RE.match(line)
            if m:
                ci_waits.append((int(m.group(1)), m.group(2)))
    return list(dict.fromkeys(jobs)), list(dict.fromkeys(ci_waits))


def turn_context_tokens(turn_dir: Path | None) -> int | None:
    """Read the context size of the last model call from turn's output.jsonl.

    For Claude turns, reads the usage of the last assistant event:
    input_tokens + cache_creation_input_tokens + cache_read_input_tokens.
    For agents without per-call usage (Codex today), returns None.
    """
    if turn_dir is None:
        return None
    try:
        data = json.loads((turn_dir / "state.json").read_text(encoding="utf-8"))
        if data.get("agent_key") == "codex":
            return None
    except (OSError, json.JSONDecodeError):
        pass
    events, _ = read_events(turn_dir / "output.jsonl")
    if any(e.get("type") == "thread.started" for e in events):
        return None
    last_context: int | None = None
    for e in events:
        if e.get("type") == "assistant" and not e.get("parent_tool_use_id"):
            usage = e.get("usage")
            if not isinstance(usage, dict):
                msg = e.get("message")
                if isinstance(msg, dict):
                    usage = msg.get("usage")
            if isinstance(usage, dict):
                inp = usage.get("input_tokens") or 0
                create = usage.get("cache_creation_input_tokens") or 0
                read = usage.get("cache_read_input_tokens") or 0
                last_context = int(inp + create + read)
    if last_context is not None:
        return last_context
    for e in events:
        if e.get("type") == "assistant":
            usage = e.get("usage")
            if not isinstance(usage, dict):
                msg = e.get("message")
                if isinstance(msg, dict):
                    usage = msg.get("usage")
            if isinstance(usage, dict):
                inp = usage.get("input_tokens") or 0
                create = usage.get("cache_creation_input_tokens") or 0
                read = usage.get("cache_read_input_tokens") or 0
                last_context = int(inp + create + read)
    return last_context


def default_pr_ci_status(repo: Path, pr_number: int, expected_head: str, run: Runner = devctx.run) -> dict[str, Any]:
    """Query GitHub CLI for PR state, required status checks, and check run outcomes."""
    code, out = run(["gh", "pr", "view", str(pr_number), "--json", "state,headRefOid,baseRefName,statusCheckRollup"],
                    cwd=repo, timeout=30)
    if code != 0:
        return {"unreadable": True}
    try:
        data = json.loads(out)
        if not isinstance(data, dict):
            return {"unreadable": True}
        raw_head = data.get("headRefOid")
        if not isinstance(raw_head, str):
            return {"unreadable": True}
        observed_head = raw_head.strip()
        if not observed_head:
            return {"unreadable": True}
        state = str(data.get("state") or "OPEN").lower()
        base_ref = str(data.get("baseRefName") or "master").strip() or "master"
        checks = []
        for c in data.get("statusCheckRollup") or []:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or c.get("context") or "")
            status = str(c.get("status") or c.get("state") or "").lower()
            conclusion = str(c.get("conclusion") or c.get("state") or "").lower()
            checks.append({"name": name, "status": status, "conclusion": conclusion})
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return {"unreadable": True}

    required: set[str] = set()
    saw_required_checks = False

    r_code, r_out = run(["gh", "api", f"repos/:owner/:repo/rules/branches/{base_ref}"], cwd=repo, timeout=30)
    if r_code == 0:
        try:
            rules = json.loads(r_out)
            if isinstance(rules, list):
                for rule in rules:
                    if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
                        params = rule.get("parameters")
                        if isinstance(params, dict):
                            raw_checks = params.get("required_status_checks")
                            if isinstance(raw_checks, list):
                                for rc in raw_checks:
                                    if isinstance(rc, dict) and isinstance(rc.get("context"), str):
                                        required.add(rc["context"])
                                    elif isinstance(rc, str):
                                        required.add(rc)
                                saw_required_checks = True
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    if not saw_required_checks:
        p_code, p_out = run(["gh", "api", f"repos/:owner/:repo/branches/{base_ref}/protection/required_status_checks"],
                            cwd=repo, timeout=30)
        if p_code == 0:
            try:
                prot = json.loads(p_out)
                if isinstance(prot, dict):
                    raw_contexts = prot.get("contexts")
                    if isinstance(raw_contexts, list):
                        for ctx in raw_contexts:
                            if isinstance(ctx, str):
                                required.add(ctx)
                    raw_checks = prot.get("checks")
                    if isinstance(raw_checks, list):
                        for chk in raw_checks:
                            if isinstance(chk, dict) and isinstance(chk.get("context"), str):
                                required.add(chk["context"])
                    if required or prot.get("enforcement_level") not in (None, "off"):
                        saw_required_checks = True
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

    if not required:
        return {"unreadable": True}

    return {
        "observed_head": observed_head,
        "state": state,
        "base_ref": base_ref,
        "required_checks": sorted(required),
        "checks": checks,
    }


def evaluate_pr_ci(ci_info: dict[str, Any], expected_head: str, elapsed_wait: float) -> tuple[str, str]:
    """Evaluate PR status and required checks against acceptance criteria.
    Returns (status_type, detail) where status_type is 'immediate_wake', 'completed', 'pending', or 'unreadable'."""
    if not isinstance(ci_info, dict) or ci_info.get("unreadable"):
        return "unreadable", "remote_unreadable"

    observed_head = ci_info.get("observed_head")
    if not isinstance(observed_head, str) or not observed_head.strip():
        return "unreadable", "remote_unreadable"
    observed_head = observed_head.strip()
    if observed_head != expected_head:
        return "immediate_wake", "head_changed"

    pr_state = str(ci_info.get("state") or "").lower()
    if pr_state in ("closed", "merged"):
        return "immediate_wake", "pr_closed"

    required_checks = ci_info.get("required_checks")
    if not isinstance(required_checks, (list, tuple, set)) or not required_checks:
        return "unreadable", "remote_unreadable"

    checks = ci_info.get("checks")
    if not isinstance(checks, list):
        return "unreadable", "remote_unreadable"

    if not checks:
        if elapsed_wait >= 120.0:
            return "immediate_wake", "no_checks_reported"
        return "pending", "no_checks_reported"

    check_map: dict[str, dict[str, Any]] = {}
    for c in checks:
        if isinstance(c, dict) and "name" in c:
            check_map[str(c["name"])] = c

    for req_name in required_checks:
        c = check_map.get(req_name)
        if c is not None:
            conclusion = str(c.get("conclusion") or "").lower()
            if conclusion in CI_FAILURE_CONCLUSIONS:
                return "immediate_wake", "check_failed"

    all_completed_success = True
    for req_name in required_checks:
        c = check_map.get(req_name)
        if c is None:
            all_completed_success = False
            break
        status = str(c.get("status") or "").lower()
        conclusion = str(c.get("conclusion") or "").lower()
        if status not in ("completed", "complete", "done", "success"):
            all_completed_success = False
            break
        if conclusion not in CI_SUCCESS_CONCLUSIONS:
            all_completed_success = False
            break

    if all_completed_success:
        return "completed", "success"

    reported_required = [req for req in required_checks if req in check_map]
    if not reported_required and elapsed_wait >= 120.0:
        return "immediate_wake", "no_checks_reported"

    return "pending", "in_progress"


def job_record_status(job: dict[str, Any]) -> str:
    """Determine effective status of a job record: terminal status is final; real processes
    are checked against the process table; uninstrumented mock/record statuses are respected."""
    status = str(job.get("status") or "")
    if status in relay_jobs.TERMINAL:
        return status
    if "wrapper" in job or "wrapper_pid" in job:
        return relay_jobs.effective_status(job)
    return status or "unknown"


def _unwrap(command: str) -> str:
    for prefix in ("bash -lc ", "bash -c ", "/bin/bash -lc ", "/bin/bash -c ", "sh -c ", "/bin/sh -c "):
        if command.strip().startswith(prefix):
            program, rest = split_first_word(command.strip()[len(prefix):])
            return program if program is not None and not rest.strip() else ""
    return command


def _simple_commands(command: str) -> list[tuple[Path | None, list[str]]] | None:
    """(cd target, words) of each simple command, or None when the command is too dynamic to probe."""
    command = re.sub(r"\s\d*>&\d\b|\s\d*>{1,2}\s*/dev/null\b", " ", _unwrap(command))  # stream redirections only
    if not command or any(ch in command for ch in "$`<>") or "\n" in command.strip():
        return None
    out: list[tuple[Path | None, list[str]]] = []
    cwd: Path | None = None
    for seg in split_segments(command):
        if seg.strip().endswith("&") and not seg.strip().endswith("&&"):
            return None  # a detached job: no probe can show it finished
        try:
            words = shlex.split(seg, comments=True)
        except ValueError:
            return None
        if words[:1] == ["cd"] and len(words) == 2:
            cwd = Path(words[1]) if cwd is None or Path(words[1]).is_absolute() else cwd / words[1]
            if not cwd.is_absolute():
                return None  # relative to an unknown working directory
        elif words:
            out.append((cwd, words))
    return out


def _denied(text: str) -> bool:
    return bool(PERMISSION_RE.search(text or ""))


def _probe_push(cwd: Path | None, args: list[str], run: Runner, repo: Path) -> tuple[str, str] | None:
    """One branch update only. HEAD, the upstream and the current branch are resolved only in a directory the
    command itself named (`cwd`); a named local branch is the same in every worktree."""
    force, positional = False, []
    for a in args:
        if a.startswith("--force-with-lease"):
            force = True
        elif a.startswith("-"):
            if a not in PUSH_FLAGS:
                return None  # --all, --tags, --delete, -o VALUE, --repo=...: not one branch update
            force = force or a in ("-f", "--force")
        else:
            positional.append(a)
    if len(positional) > 2:
        return None
    if len(positional) == 2:
        refspec = positional[1]
        force = force or refspec.startswith("+")
        src, _, dst = refspec.lstrip("+").partition(":")
        remote, dst = positional[0], dst or src
        if not src:
            return None  # `:branch` deletes it
    else:
        if cwd is None:
            return None
        code, upstream = run(["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        if code != 0 or "/" not in upstream.strip():
            return None
        up_remote, _, up_branch = upstream.strip().partition("/")
        remote, src, dst = (positional[0] if positional else up_remote), "HEAD", up_branch
    if src == "HEAD" or dst == "HEAD":
        if cwd is None:
            return None
        code, branch = run(["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"])
        if code != 0 or branch.strip() in ("", "HEAD"):
            return None
        dst = branch.strip() if dst == "HEAD" else dst
    src, dst = src.removeprefix("refs/heads/"), dst.removeprefix("refs/heads/")
    if dst.startswith("refs/") or src.startswith("refs/"):
        return None  # tags, notes and other refs are not probed
    cwd = cwd or repo
    if src != "HEAD" and run(["git", "-C", str(cwd), "show-ref", "--verify", "--quiet", f"refs/heads/{src}"])[0] != 0:
        return None  # not a local branch (a tag, a SHA): not a branch update
    code, local = run(["git", "-C", str(cwd), "rev-parse", "--verify", f"{src}^{{commit}}"])
    if code != 0:
        return None
    local = local.strip()
    code, listed = run(["git", "-C", str(cwd), "ls-remote", remote, f"refs/heads/{dst}"], timeout=60)
    if code != 0:
        return ("insufficient_permissions", f"`git ls-remote {remote}` was refused") if _denied(listed) else None
    remote_sha = listed.split()[0] if listed.split() else ""
    if remote_sha == local:
        return "completed", f"remote branch {dst} is at the pushed commit {local[:12]}"
    if force:
        return None  # a forced update may have replaced someone else's work: not settled by ancestry
    if not remote_sha:
        return "not_started", f"remote branch {dst} does not exist: the push did not take effect"
    code, _ = run(["git", "-C", str(cwd), "merge-base", "--is-ancestor", remote_sha, local])
    if code == 0:
        return "not_started", f"remote branch {dst} ({remote_sha[:12]}) is behind {local[:12]}: the push did not take effect"
    return None


def _gh_parse(args: list[str], value_opts: set[str]) -> tuple[list[str], dict[str, str], set[str]]:
    positional, values, flags, i = [], {}, set(), 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and "=" in a:
            key, value = a.split("=", 1)
            values[key] = value
        elif a in value_opts and i + 1 < len(args):
            values[a] = args[i + 1]
            i += 1
        elif a.startswith("-"):
            flags.add(a)
        else:
            positional.append(a)
        i += 1
    return positional, values, flags


def _probe_gh(cwd: Path | None, args: list[str], run: Runner) -> tuple[str, str] | None:
    """`cwd` is set only when the command itself named an absolute directory; otherwise the agent's working
    directory is unknown (it persists across calls), so nothing branch-relative is resolved."""
    sub = args[1] if len(args) > 1 and args[0] == "pr" else ""
    positional, values, flags = _gh_parse(args, GH_VALUE_OPTS.get(sub, set()))
    repo = values.get("-R") or values.get("--repo")
    repo_args = ["--repo", repo] if repo else []
    if positional[:2] == ["pr", "merge"]:
        if flags & {"--auto", "--disable-auto"} or len(positional) != 3 \
                or not re.fullmatch(r"\d+|https://github\.com/[^/\s]+/[^/\s]+/pull/\d+", positional[2]):
            return None  # auto-merge is armed, not performed; without an explicit PR the target is unknown
        target = positional[2:3]
        code, out = run(["gh", "pr", "view", *target, *repo_args, "--json", "number,state"], cwd=cwd, timeout=60)
        if code != 0:
            return ("insufficient_permissions", "`gh pr view` was refused") if _denied(out) else None
        try:
            pr = json.loads(out)
        except ValueError:
            return None
        if pr.get("state") == "MERGED":
            return "completed", f"PR #{pr.get('number')} is merged"
        if pr.get("state") == "OPEN":
            return "not_started", f"PR #{pr.get('number')} is still open: the merge did not take effect"
        return None
    if positional[:2] == ["pr", "create"] and len(positional) == 2:
        head = values.get("-H") or values.get("--head")
        if head is None:
            if cwd is None:
                return None
            code, branch = run(["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"])
            if code != 0 or branch.strip() in ("", "HEAD"):
                return None
            head = branch.strip()
        code, out = run(["gh", "pr", "list", "--head", head, "--state", "all", *repo_args, "--json", "number,state"],
                        cwd=cwd, timeout=60)
        if code != 0:
            return ("insufficient_permissions", "`gh pr list` was refused") if _denied(out) else None
        try:
            prs = [p for p in json.loads(out) if isinstance(p, dict)]
        except (ValueError, TypeError):
            return None
        live = [p for p in prs if p.get("state") == "OPEN"]  # a merged one may be an older PR from a reused name
        if live:
            return "completed", f"open PR #{live[0].get('number')} exists for branch {head}"
        if not prs:
            return "not_started", f"no PR exists for branch {head}: the creation did not take effect"
        return None
    return None


def _probe_file(probe: dict[str, Any], tool: str) -> tuple[str, str] | None:
    path = Path(probe.get("file_path") or "")
    if not path.is_absolute():
        return None
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else None
    except (OSError, UnicodeDecodeError):
        return None
    if tool == "Write" and "content" in probe:
        if text is None:
            return "not_started", f"{path.name} does not exist: the write did not take effect"
        if text == probe["content"]:
            return "completed", f"{path.name} holds exactly the written content"
        return None
    old, new = probe.get("old_string"), probe.get("new_string")
    if tool != "Edit" or text is None or not old or new is None or old in new or new in old:
        return None  # containment makes presence ambiguous
    if new in text and old not in text:
        return "completed", f"{path.name} contains the edit's replacement text and not the original"
    if old in text and new not in text:
        return "not_started", f"{path.name} still contains the original text: the edit did not take effect"
    return None


def probe_call(call: ToolCall, repo: Path, run: Runner = devctx.run) -> tuple[str, str] | None:
    """Deterministic read-only probe of one in-flight operation: (verdict, evidence), or None when only a
    broader investigation can tell. A command probes only when its one effectful step is the operation."""
    if call.tool in ("Edit", "Write"):
        return _probe_file(call.probe, call.tool)
    commands = _simple_commands(str(call.probe.get("command") or ""))
    if not commands:
        return None
    effectful = [(cwd, words) for cwd, words in commands if classify_command(shlex.join(words))[0] != "read_only"]
    if len(effectful) > 1:
        return _probe_commit_push(effectful, run, repo)
    if len(effectful) != 1:
        return None
    cd, words = effectful[0]
    # The agent's working directory persists across calls: only an absolute directory named here is known.
    cwd = cd if cd is not None and cd.is_absolute() else None
    if Path(words[0]).name == "git":
        args = words[1:]
        while args[:1] == ["-C"] and len(args) > 1:
            cwd = Path(args[1]) if Path(args[1]).is_absolute() else (cwd / args[1] if cwd else None)
            args = args[2:]
        return _probe_push(cwd, args[1:], run, repo) if args[:1] == ["push"] else None
    if Path(words[0]).name == "gh":
        return _probe_gh(cwd, words[1:], run)
    return None


LOCAL_GIT_STEPS = {"add", "commit", "rm", "mv", "tag"}


def _git_step(cd: Path | None, words: list[str]) -> tuple[Path | None, list[str]] | None:
    """(directory, git arguments after any -C) of a git command, or None when it is not git."""
    if Path(words[0]).name != "git":
        return None
    cwd, args = (cd if cd is not None and cd.is_absolute() else None), words[1:]
    while args[:1] == ["-C"] and len(args) > 1:
        cwd = Path(args[1]) if Path(args[1]).is_absolute() else (cwd / args[1] if cwd else None)
        args = args[2:]
    return cwd, args


def _probe_commit_push(effectful: list[tuple[Path | None, list[str]]], run: Runner,
                       repo: Path) -> tuple[str, str] | None:
    """Local git steps (add, commit, rm, mv, tag) followed by exactly one push, all in one named directory: the
    push probe settles it. A push found on the remote counts as completed only when the worktree has no staged or
    modified tracked file left, so an interruption before the commit is not taken for a finished push."""
    steps = [_git_step(cd, words) for cd, words in effectful]
    if any(step is None for step in steps):
        return None
    *local, (cwd, push) = steps  # type: ignore[misc]
    if push[:1] != ["push"] or any(args[:1] == [] or args[0] not in LOCAL_GIT_STEPS for _, args in local):
        return None
    if cwd is None or any(step_cwd != cwd for step_cwd, _ in local):
        return None
    probed = _probe_push(cwd, push[1:], run, repo)
    if probed is None or probed[0] != "completed":
        return probed
    code, dirty = run(["git", "--no-optional-locks", "-C", str(cwd), "status", "--porcelain", "--untracked-files=no"])
    if code != 0 or dirty.strip():
        return None
    return probed




def check_task(repo: Path, check: dict[str, Any], run: Runner = devctx.run) -> str | None:
    """Deterministic, read-only completion check of a mission task. None: satisfied."""
    if check["kind"] == "host_receipt":
        return (None if host_ops.find_ok_receipt(host_ops.Paths(Path.home()), check["op"], check.get("sha"))
                else "no verified ok host receipt")
    if check["kind"] == "path_exists":
        target = (repo / check["path"]).resolve()
        if repo.resolve() not in target.parents and target != repo.resolve():
            return "path escapes the repository"
        return None if target.exists() else f"{check['path']} does not exist"
    if check["kind"] == "pr_merged":
        code, out = run(["gh", "pr", "view", str(check["pr"]), "--json", "state", "-q", ".state"], cwd=repo, timeout=60)
        if code != 0:
            return f"gh pr view {check['pr']} failed"
        return None if out.strip() == "MERGED" else f"PR #{check['pr']} is {out.strip() or 'unknown'}"
    if check["kind"] == "coordinator_cycle":
        code, out = run(["git", "ls-remote", "--heads", "origin"], cwd=repo, timeout=60)
        return None if code == 0 and out.strip() else "remote heads unreadable"
    if check["kind"] == "spec_merged":
        # The registry on GitHub master, not a local checkout a provider may have edited.
        code, out = run(["gh", "api", "-H", "Accept: application/vnd.github.raw",
                         "repos/{owner}/{repo}/contents/docs/specs/STATUS.md?ref=master"], cwd=repo, timeout=60)
        if code != 0 or not out.strip():
            return "STATUS.md on GitHub master is unreadable"
        rows = [cells for line in out.splitlines() if line.startswith("|")
                and len(cells := [c.strip() for c in line.strip().strip("|").split("|")]) >= 3 and cells[0] == check["spec"]]
        if len(rows) != 1:
            return f"spec {check['spec']} has {len(rows)} STATUS.md rows"
        if rows[0][1] not in ("merged", "in_review"):
            return f"spec {check['spec']} is {rows[0][1] or 'unknown'} in STATUS.md"
        prs = sorted({int(n) for n in re.findall(r"\[#(\d+)\]", rows[0][2])})
        if not prs:
            return f"spec {check['spec']} links no PR in STATUS.md"
        for pr in prs:
            if problem := check_task(repo, {"kind": "pr_merged", "pr": pr}, run):
                return problem
        return None
    return f"unknown check {check.get('kind')}"




RELAY_STATUS_LINE = re.compile(r"^RELAY_STATUS:\s*(\{.*\})\s*$", re.MULTILINE)
RELAY_STATUSES = ("continue", "propose_done", "blocked")


def reply_relay_status(text: str) -> str | None:
    """The status of the last well-formed `RELAY_STATUS: {...}` line of the agent's own final reply, or None."""
    for raw in reversed(RELAY_STATUS_LINE.findall(text)):
        try:
            status = json.loads(raw).get("status")
        except (json.JSONDecodeError, AttributeError):
            continue
        if status in RELAY_STATUSES:
            return status
    return None


def final_relay_status(repo: Path, session: str | None) -> str | None:
    """RELAY_STATUS the coordinator reported at the end of a session (continue | propose_done | blocked).
    The coordinator's own final line wins: Agent Relay 0.7.7 replaces the recorded status with `continue` whenever
    certain credential or connectivity words appear anywhere in a turn's stream, tool output included, so a
    finished task was relaunched (cycle 87, ledger L20261025085406-61ab). Relay's recorded status is only the
    fallback when the reply carries no such line."""
    turn = last_turn_dir(repo / ".agent-relay" / "sessions" / session) if session else None
    if turn is None:
        return None
    if (status := reply_relay_status(final_text(turn))) is not None:
        return status
    try:
        return json.loads((turn / "state.json").read_text()).get("status")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


