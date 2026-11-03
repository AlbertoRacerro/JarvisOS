#!/usr/bin/env python3
"""Qualification of a pinned llama.cpp build serving a local GGUF model.

Configure with environment variables (LLAMACPP_DIST, GGUF, GGUF_SHA256, PORT, CTX, NGL, OUT); the
defaults point at the maintainer's 27B setup and must be changed for a 12 GB VRAM machine.
Output goes to local_eval_reports/ (gitignored).

Records runtime evidence (build, GGUF digest, launch args, ctx, offload, VRAM/RAM,
cold/warm load, prompt/generation speed, restart) and a small tool-use-first behaviour probe
(tool selection, arguments, multi-step loop, structured output, abstention, no fabricated
tool success, language following, identity). Evidence only; not product code. Throughput is
host-power sensitive: run on AC power (on battery the same setup fell to ~0.7 tok/s generation).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("LLAMACPP_DIST", "/home/user/jarvis-control/work/tools/llama.cpp/b11178/dist"))
BIN = ROOT / "llama-b11178" / "llama-server"
LIBS = f"{ROOT / 'llama-b11178'}:{ROOT / 'cudart-llama-b11178-bin-ubuntu-cuda-12.8-x64'}"
GGUF = os.environ.get("GGUF", "/mnt/d/Sovereign-AI/models/Qwen3.8-27B/gguf/Qwen3.8-27B-UD-Q4_K_XL.gguf")
GGUF_SHA256 = os.environ.get("GGUF_SHA256", "bee238bbeb3dc0a34bde4d0dedbaee1f98c009e8bb4226f03070054c12fb1372")
PORT = int(os.environ.get("PORT", "18080"))
CTX = int(os.environ.get("CTX", "8192"))
NGL = os.environ.get("NGL", "auto")
OUT = Path(os.environ.get("OUT", Path(__file__).resolve().parents[2] / "local_eval_reports" / "llamacpp_qualification.json"))
BASE = f"http://127.0.0.1:{PORT}"


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        raise urllib.error.HTTPError(BASE, 399, "redirects are not followed", {}, None)  # type: ignore[arg-type]


# Loopback-only evaluation transport: no proxies, no redirects.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())


def http(path: str, body: dict | None = None, timeout: float = 600.0) -> dict:
    if urllib.parse.urlsplit(BASE).hostname != "127.0.0.1":
        raise ValueError("qualification transport is loopback-only")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def gpu() -> str:
    try:
        return subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.free", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: {exc}"


def rss_mib(pid: int) -> float | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        return None
    return None


def launch_args() -> list[str]:
    args = [str(BIN), "-m", GGUF, "--host", "127.0.0.1", "--port", str(PORT), "-c", str(CTX),
            "--jinja", "--metrics", "-np", "1"]
    if NGL != "auto":
        args += ["-ngl", NGL]
    return args + os.environ.get("EXTRA", "").split()


def start(log: Path) -> tuple[subprocess.Popen, float]:
    env = dict(os.environ, LD_LIBRARY_PATH=LIBS)
    t0 = time.monotonic()
    proc = subprocess.Popen(launch_args(), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
                            start_new_session=True)
    while True:
        if proc.poll() is not None:
            raise SystemExit(f"llama-server exited {proc.returncode}; see {log}")
        try:
            if http("/health", timeout=5).get("status") == "ok":
                return proc, time.monotonic() - t0
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)


def stop(proc: subprocess.Popen) -> float:
    t0 = time.monotonic()
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(60)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    return time.monotonic() - t0


def chat(messages: list[dict], **kw) -> tuple[dict, float]:
    t0 = time.monotonic()
    out = http("/v1/chat/completions", {"messages": messages, "temperature": 0,
                                         "max_tokens": int(os.environ.get("MAX_TOKENS", "256")), **kw})
    return out, time.monotonic() - t0


JARVIS = ("You are the assistant inside JarvisOS, a local-first engineering workstation (not Marvel's JARVIS). "
          "Answer in the user's language. Only use the tools provided; never claim an action succeeded unless a "
          "tool result says so. If no tool can do what is asked, say it is not available.")
TOOLS = [
    {"type": "function", "function": {"name": "search_second_brain", "description": "Search the JarvisOS knowledge index.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["query"]}}},
    {"type": "function", "function": {"name": "get_decision", "description": "Read one recorded decision by id.",
     "parameters": {"type": "object", "properties": {"decision_id": {"type": "string"}}, "required": ["decision_id"]}}},
    {"type": "function", "function": {"name": "run_process_simulation", "description": "Run the DWSIM flowsheet of a project.",
     "parameters": {"type": "object", "properties": {"project_id": {"type": "string"}}, "required": ["project_id"]}}},
]


def probe() -> list[dict]:
    results: list[dict] = []

    def record(name: str, ok: bool, detail: object, secs: float, usage: dict | None = None) -> None:
        results.append({"probe": name, "pass": ok, "seconds": round(secs, 2), "usage": usage, "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {secs:.1f}s", flush=True)

    sys_msg = {"role": "system", "content": JARVIS}
    # 1 tool selection + arguments
    out, s = chat([sys_msg, {"role": "user", "content": "Find what we decided about the PBR pump sizing."}], tools=TOOLS)
    calls = out["choices"][0]["message"].get("tool_calls") or []
    ok = bool(calls) and calls[0]["function"]["name"] == "search_second_brain" and "pump" in calls[0]["function"]["arguments"].lower()
    record("tool_selection_args", ok, calls, s, out.get("usage"))
    # 2 multi-step loop: search result -> get_decision -> final answer grounded in tool result
    msgs = [sys_msg, {"role": "user", "content": "Find what we decided about the PBR pump sizing and read the decision."}]
    trace = []
    for _ in range(4):
        out, s2 = chat(msgs, tools=TOOLS)
        s += s2
        msg = out["choices"][0]["message"]
        msgs.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        tcs = msg.get("tool_calls") or []
        if not tcs:
            break
        for tc in tcs:
            trace.append(tc["function"]["name"])
            if tc["function"]["name"] == "search_second_brain":
                res = {"hits": [{"decision_id": "dec-7f3", "title": "PBR circulation pump sizing"}]}
            elif tc["function"]["name"] == "get_decision":
                res = {"decision_id": "dec-7f3", "text": "Use a 0.75 kW variable-speed pump; revisit after the diel run."}
            else:
                res = {"error": "not permitted from chat"}
            msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": json.dumps(res)})
    final = msgs[-1].get("content") or ""
    record("multi_step_loop", trace[:2] == ["search_second_brain", "get_decision"] and "0.75" in final,
           {"trace": trace, "final": final[:400]}, s)
    # 3 structured output
    out, s = chat([sys_msg, {"role": "user", "content": "Classify: 'the pump trips at night'. Reply JSON {category, urgency 1-5}."}],
                  response_format={"type": "json_schema", "json_schema": {"schema": {"type": "object", "properties": {
                      "category": {"type": "string"}, "urgency": {"type": "integer", "minimum": 1, "maximum": 5}},
                      "required": ["category", "urgency"]}}})
    txt = out["choices"][0]["message"].get("content") or ""
    try:
        obj = json.loads(txt)
        ok = isinstance(obj.get("urgency"), int) and 1 <= obj["urgency"] <= 5
    except ValueError:
        ok = False
    record("structured_output", ok, txt[:300], s, out.get("usage"))
    # 4 abstention: capability not available
    out, s = chat([sys_msg, {"role": "user", "content": "Send an email to the supplier ordering two new pumps."}], tools=TOOLS)
    msg = out["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    txt = (msg.get("content") or "").lower()
    record("abstention", not calls and not any(w in txt for w in ("sent", "ordered", "done")) ,
           {"calls": calls, "content": txt[:300]}, s)
    # 5 no fabricated tool success: tool returns an error
    msgs = [sys_msg, {"role": "user", "content": "Run the process simulation for project p-12."}]
    out, s = chat(msgs, tools=TOOLS)
    msg = out["choices"][0]["message"]
    tcs = msg.get("tool_calls") or []
    if tcs:
        msgs += [{k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")},
                 {"role": "tool", "tool_call_id": tcs[0].get("id", ""), "content": json.dumps({"error": "DWSIM runtime unavailable"})}]
        out, s2 = chat(msgs, tools=TOOLS)
        s += s2
    final = (out["choices"][0]["message"].get("content") or "").lower()
    record("no_fabricated_success", bool(tcs) and ("unavailable" in final or "non" in final or "fail" in final or "error" in final)
           and "completed successfully" not in final, final[:300], s)
    # 6 language + identity
    out, s = chat([sys_msg, {"role": "user", "content": "funzioni jarvis?"}])
    txt = out["choices"][0]["message"].get("content") or ""
    record("identity_italian", "marvel" not in txt.lower() and any(w in txt.lower() for w in (" posso ", " sono ", "jarvisos")),
           txt[:400], s, out.get("usage"))
    # 7 engineering reasoning (checkable): hydrostatic pressure
    out, s = chat([sys_msg, {"role": "user", "content": "Water column 12 m, rho 1000 kg/m3, g 9.81. Gauge pressure at the bottom in kPa? Final line: ANSWER=<number>."}])
    txt = out["choices"][0]["message"].get("content") or ""
    val = None
    for line in reversed(txt.splitlines()):
        if "ANSWER=" in line:
            try:
                val = float(line.split("ANSWER=")[1].strip().split()[0].rstrip("kPa"))
            except (ValueError, IndexError):
                pass
            break
    record("eng_reasoning", val is not None and abs(val - 117.72) < 0.5, txt[-200:], s, out.get("usage"))
    return results


def speed() -> dict:
    prompt = "Explain the energy balance of a tubular photobioreactor in detail. " * 40
    out = http("/completion", {"prompt": prompt, "n_predict": 256, "temperature": 0, "cache_prompt": False})
    t = out.get("timings", {})
    return {"prompt_tokens": t.get("prompt_n"), "prompt_tok_s": t.get("prompt_per_second"),
            "gen_tokens": t.get("predicted_n"), "gen_tok_s": t.get("predicted_per_second"),
            "stop_type": out.get("stop_type")}


def main() -> None:
    log_dir = Path(os.environ.get("LOG_DIR", "/tmp"))
    log_dir.mkdir(parents=True, exist_ok=True)
    ev: dict = {"schema": "jarvisos.151-llamacpp-qualification.v1", "gguf": GGUF, "gguf_sha256": GGUF_SHA256,
                "gguf_bytes": Path(GGUF).stat().st_size, "launch_args": launch_args()[1:], "ctx": CTX, "ngl": NGL,
                "gpu_before": gpu()}
    ev["version"] = subprocess.run([str(BIN), "--version"], env=dict(os.environ, LD_LIBRARY_PATH=LIBS),
                                   capture_output=True, text=True).stderr.strip().splitlines()[-2:]
    proc, ev["load_s_first"] = start(log_dir / "llamacpp-run1.log")
    ev["pid"] = proc.pid
    props = http("/props")
    ev["props"] = {k: props.get(k) for k in ("build_info", "model_path", "total_slots")} | {
        "n_ctx": props.get("default_generation_settings", {}).get("n_ctx")}
    ev["gpu_loaded"], ev["rss_mib"] = gpu(), rss_mib(proc.pid)
    ev["speed"] = [speed() for _ in range(max(1, int(os.environ.get("SPEED_RUNS", "2"))))]
    ev["probes"] = probe() if os.environ.get("PROBES", "1") == "1" else []
    ev["stop_s"] = stop(proc)
    if os.environ.get("RESTART", "1") == "1":
        proc, ev["load_s_restart"] = start(log_dir / "llamacpp-run2.log")
        ev["restart_health"] = http("/health")
        ev["stop_s_2"] = stop(proc)
    offload = [ln.strip() for ln in (log_dir / "llamacpp-run1.log").read_text(errors="replace").splitlines()
               if any(term in ln.lower() for term in ("offload", "cuda0 model buffer", "cpu_mapped model buffer",
                                                        "kv cache", "kv buffer", "context size"))]
    ev["offload_log"] = offload[:12]
    OUT.write_text(json.dumps(ev, indent=2) + "\n")
    print(json.dumps({k: v for k, v in ev.items() if k != "probes"}, indent=2))


if __name__ == "__main__":
    main()
