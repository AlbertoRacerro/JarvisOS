"""Measure how well the local model points at the right target, through the backend's own prompt assembly.

Read-only against the model server: it only POSTs chat completions to the already running llama-server (the one the
app uses) and writes a JSON file. No app data is touched.

Run from the backend directory with the backend venv, e.g.
  cd backend && .venv/bin/python ../docs/implementation/evidence/bubu3-pointing/pointing_eval.py \
      --label after --ids new --out ../docs/implementation/evidence/bubu3-pointing/results_after.json
`--ids old|new` selects how Components-tree rows are named in the hand-built snapshots (master vs this change); the
prompt text comes from whatever backend code is checked out (render_visible_page_block + assemble_prompt).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenarios
from app.modules.ai.context_builder import assemble_prompt
from app.modules.ai.thread_service import _jarvis_system_envelope
from app.modules.ai.thread_ui_context import (
    VISIBLE_PAGE_SOURCE,
    UiContext,
    render_visible_page_block,
)

DIRECTIVE = re.compile(r"\[\[\s*point\s*:\s*([\w.:-]+)\s*(?:\|([^\]]*))?\]\]", re.IGNORECASE)


def build_prompt(ui_context: dict, question: str) -> str:
    envelope = _jarvis_system_envelope(workspace_id="zephyr", workspace_name="Zephyr", app_state=None, route_availability=[])
    blocks = [{"source": "jarvis:system-envelope", "content": envelope},
              {"source": VISIBLE_PAGE_SOURCE, "content": render_visible_page_block(UiContext.model_validate(ui_context))}]
    return assemble_prompt(blocks, question)


def ask(base_url: str, model: str, prompt: str, max_tokens: int) -> dict:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0,
                       "max_tokens": max_tokens, "stream": False,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    headers = {"Content-Type": "application/json"}
    key = os.getenv("JARVISOS_LLAMACPP_API_KEY")  # the operator's token for the running server, supplied by the caller
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(f"{base_url}/chat/completions", data=body, headers=headers)
    started = time.time()
    with urllib.request.urlopen(request, timeout=300) as response:
        payload = json.load(response)
    message = payload["choices"][0]["message"]
    return {"text": message.get("content") or "", "seconds": round(time.time() - started, 1),
            "prompt_tokens": (payload.get("usage") or {}).get("prompt_tokens")}


def score(text: str, correct: list[str], plausible: list[str]) -> tuple[str, str | None]:
    match = DIRECTIVE.search(text)
    if not match:
        return "none", None
    first = match.group(1)
    return ("correct" if first in correct else "plausible" if first in plausible else "wrong"), first


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label")
    parser.add_argument("--ids", choices=["old", "new"], required=True)
    parser.add_argument("--out")
    parser.add_argument("--emit-prompts", help="write prompt JSON without contacting the model")
    parser.add_argument("--base-url", default="http://127.0.0.1:18081/v1")
    parser.add_argument("--model", default="gemma-4-12b-it-qat-q4_0")
    parser.add_argument("--max-tokens", type=int, default=220)
    parser.add_argument("--dry-run", action="store_true", help="print prompts, do not call the model")
    args = parser.parse_args()
    if not args.emit_prompts and (not args.label or not args.out):
        parser.error("--label and --out are required for a model run")
    rows = []
    prompts = []
    for scenario in scenarios.build(args.ids):
        prompt = build_prompt(scenario["ui_context"], scenario["question"])
        if args.emit_prompts:
            prompts.append({"name": scenario["name"], "prompt": prompt,
                            "correct": scenario["correct"], "plausible": scenario["plausible"]})
            continue
        if args.dry_run:
            print(f"##### {scenario['name']} ({len(prompt)} chars)\n{prompt}\n")
            continue
        reply = ask(args.base_url, args.model, prompt, args.max_tokens)
        verdict, first = score(reply["text"], scenario["correct"], scenario["plausible"])
        rows.append({"name": scenario["name"], "question": scenario["question"], "verdict": verdict, "first_id": first,
                     "correct": scenario["correct"], "plausible": scenario["plausible"], "prompt_chars": len(prompt), **reply})
        print(f"{scenario['name']:34} {verdict:9} first={first}  ({reply['seconds']}s)", flush=True)
    if args.emit_prompts:
        Path(args.emit_prompts).write_text(json.dumps(prompts, ensure_ascii=False) + "\n", encoding="utf-8")
    elif not args.dry_run:
        tally = {k: sum(1 for r in rows if r["verdict"] == k) for k in ("correct", "plausible", "wrong", "none")}
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        Path(args.out).write_text(json.dumps({"label": args.label, "head": head, "ids": args.ids,
                                              "model": args.model, "base_url": args.base_url,
                                              "max_tokens": args.max_tokens, "temperature": 0,
                                              "tally": tally, "rows": rows},
                                             indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("tally", tally)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
