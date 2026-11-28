#!/usr/bin/env python3
"""Explorer runner, trace extractor, and request-set generator for Spec 388.

Stdlib only. Drives strong-model exploration against a loopback proof backend,
extracts deterministic read-only SQLite tool-call traces, parses explorer work reports,
and simulates maintainer clarifications for interactive engineering tasks.
"""

from __future__ import annotations

import datetime
import json
import random
import re
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts import vault_guard
from scripts.benchmarks.agent_eval.runner import (
    TERMINAL_FLOW_STATES,
    _bare_tool_name,
    _http_request,
    _open_proof_db,
    read_job_metrics,
    validate_base_url,
)

# Design families (6) and research families (6), plus operational families (4)
DESIGN_FAMILIES = ("cad", "cfd", "process", "fem", "optics", "biology")
RESEARCH_FAMILIES = (
    "literature",
    "materials",
    "data_vs_model",
    "diagnosis",
    "sensitivity",
    "costs",
)
OTHER_FAMILIES = ("planning_notes", "dev_diagnostics", "machine_gpu", "settings")
ALL_FAMILIES = DESIGN_FAMILIES + RESEARCH_FAMILIES + OTHER_FAMILIES

# Generic engineering templates with zero private organism/project/customer names
_GENERIC_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "cad": [
        {
            "prompt": "Inspect the 3D geometry of the generic mounting bracket model and check hole concentricity.",
            "interactive": True,
            "user_intent": "Verify that all 4 mounting holes are concentric with the outer fillet radius.",
            "preferences": "Prefer M6 standard clearance holes.",
        },
        {
            "prompt": "Create a parametric sweep of fillet radius from 2mm to 8mm for the structural flange.",
            "interactive": False,
        },
        {
            "prompt": "Review the CAD model of the generic spur gear and report pitch diameter and tooth module.",
            "interactive": False,
        },
    ],
    "cfd": [
        {
            "prompt": "Set up a steady-state pressure drop simulation for the generic 90-degree pipe elbow at Re=50,000.",
            "interactive": True,
            "user_intent": "Compute Darcy friction factor and total head loss across the bend.",
            "preferences": "Use k-epsilon turbulence model with standard wall functions.",
        },
        {
            "prompt": "Check the grid convergence index for the 2D backward-facing step mesh refinement study.",
            "interactive": False,
        },
        {
            "prompt": "Calculate velocity profile and boundary layer thickness at the outlet of a generic 50mm duct.",
            "interactive": False,
        },
    ],
    "process": [
        {
            "prompt": "Size a generic shell-and-tube heat exchanger for cooling 5 kg/s water from 85C to 45C using cooling water at 20C.",
            "interactive": True,
            "user_intent": "Determine required surface area and tube count assuming U=1200 W/(m^2*K).",
            "preferences": "Prefer single-pass shell, two-pass tube layout with stainless steel tubes.",
        },
        {
            "prompt": "Determine equilibrium tray count for a binary ethanol-water distillation column at 1 atm.",
            "interactive": False,
        },
        {
            "prompt": "Calculate net positive suction head available (NPSHa) for a generic centrifugal pump transfer loop.",
            "interactive": False,
        },
    ],
    "fem": [
        {
            "prompt": "Perform linear static stress analysis on a simply supported generic rectangular steel beam under central point load.",
            "interactive": True,
            "user_intent": "Find maximum von Mises stress and verify safety factor exceeds 2.0 against yield.",
            "preferences": "Material: generic structural steel, yield strength 250 MPa.",
        },
        {
            "prompt": "Compute the first three natural frequencies of an aluminum cantilever plate in bending.",
            "interactive": False,
        },
        {
            "prompt": "Inspect stress concentration factor Kt near the circular cutout of a generic tensile strip.",
            "interactive": False,
        },
    ],
    "optics": [
        {
            "prompt": "Design a generic plano-convex singlet lens in N-BK7 glass with effective focal length 100mm at 587.6 nm.",
            "interactive": True,
            "user_intent": "Determine front radius of curvature and center thickness for minimal spherical aberration.",
            "preferences": "Clear aperture 25 mm, edge thickness at least 2.5 mm.",
        },
        {
            "prompt": "Calculate optical transmission spectrum through a 3mm PMMA acrylic window between 400 nm and 700 nm.",
            "interactive": False,
        },
        {
            "prompt": "Estimate diffraction-limited spot size and Airy disk radius for a generic f/4 imaging system.",
            "interactive": False,
        },
    ],
    "biology": [
        {
            "prompt": "Model generic Monod microbial growth kinetics in a continuous stirred-tank bioreactor at steady state.",
            "interactive": True,
            "user_intent": "Calculate critical dilution rate and substrate washout concentration.",
            "preferences": "Assume mu_max=0.45 1/h, Ks=0.2 g/L, yield coefficient Y_xs=0.5.",
        },
        {
            "prompt": "Estimate volumetric oxygen mass transfer coefficient kLa in a generic 10L stirred fermenter.",
            "interactive": False,
        },
        {
            "prompt": "Formulate a simple Michaelis-Menten kinetic model for generic enzymatic hydrolysis of sucrose.",
            "interactive": False,
        },
    ],
    "literature": [
        {
            "prompt": "Search published engineering literature for high-temperature creep behaviour of austenitic alloys above 600C.",
            "interactive": True,
            "user_intent": "Find recent peer-reviewed review articles and summarize rupture life data.",
            "preferences": "Focus on open-access publications from 2018 onwards.",
        },
        {
            "prompt": "Survey published patents on rotary valve sealing mechanisms for high-pressure slurries.",
            "interactive": False,
        },
        {
            "prompt": "Retrieve references discussing validation benchmarks for laminar-turbulent boundary layer transition.",
            "interactive": False,
        },
    ],
    "materials": [
        {
            "prompt": "Compare 316L stainless steel vs grade 2 titanium for seawater cooling heat exchanger tubing.",
            "interactive": True,
            "user_intent": "Evaluate pitting resistance equivalent number (PREN), thermal conductivity, and life-cycle cost.",
            "preferences": "Operating temperature up to 60C, chlorination up to 1 ppm.",
        },
        {
            "prompt": "List mechanical properties and fatigue limit of aerospace grade 7075-T6 aluminum alloy.",
            "interactive": False,
        },
        {
            "prompt": "Evaluate chemical compatibility of generic fluoroelastomer O-rings with hydraulic ester fluids.",
            "interactive": False,
        },
    ],
    "data_vs_model": [
        {
            "prompt": "Fit a lumped-capacitance transient cooling model to measured thermocouple time-series data.",
            "interactive": True,
            "user_intent": "Extract convective heat transfer coefficient h and compute R-squared goodness of fit.",
            "preferences": "Report 95% confidence intervals on fitted parameters.",
        },
        {
            "prompt": "Compare simulated pump head-capacity curve against experimental test bench measurements.",
            "interactive": False,
        },
        {
            "prompt": "Residual analysis and out-of-sample error estimate for polynomial friction factor correlation.",
            "interactive": False,
        },
    ],
    "diagnosis": [
        {
            "prompt": "Diagnose root cause of high 2X rotational harmonic vibration in centrifugal blower telemetry.",
            "interactive": True,
            "user_intent": "Determine whether misalignment or shaft bow is the primary vibration driver.",
            "preferences": "Recommend corrective balancing or laser alignment steps.",
        },
        {
            "prompt": "Investigate anomalous temperature excursion in cooling water loop during batch changeover.",
            "interactive": False,
        },
        {
            "prompt": "Diagnose cavitation onset in generic gear pump from acoustic emission and suction pressure traces.",
            "interactive": False,
        },
    ],
    "sensitivity": [
        {
            "prompt": "Run a sensitivity analysis of heat exchanger pressure drop with respect to baffle cut and baffle pitch.",
            "interactive": True,
            "user_intent": "Identify the parameter that contributes most variance to total pumping power.",
            "preferences": "Vary parameters +/- 20% around nominal design baseline.",
        },
        {
            "prompt": "Perform Sobol sensitivity indexing for cantilever beam tip deflection across modulus and load.",
            "interactive": False,
        },
        {
            "prompt": "Assess sensitivity of distillation reboiler duty to feed composition fluctuations.",
            "interactive": False,
        },
    ],
    "costs": [
        {
            "prompt": "Estimate manufacturing and assembly costs for a generic CNC-machined aluminum optical bench bracket.",
            "interactive": True,
            "user_intent": "Break down material stock, machine time, surface anodizing, and inspection costs.",
            "preferences": "Assume batch size of 50 units.",
        },
        {
            "prompt": "Calculate annualized capital and operating costs for a 100 kW industrial chiller installation.",
            "interactive": False,
        },
        {
            "prompt": "Compare 5-year total cost of ownership between electric motor drive vs variable frequency drive.",
            "interactive": False,
        },
    ],
    "planning_notes": [
        {
            "prompt": "Draft an engineering turn plan and intermediate findings document for the heat exchanger design review.",
            "interactive": False,
        },
        {
            "prompt": "Compile engineering laboratory notebook notes summarizing the day's tensile testing campaign.",
            "interactive": False,
        },
    ],
    "dev_diagnostics": [
        {
            "prompt": "Check workbench tool execution logs for failing sub-process exit codes and report error distribution.",
            "interactive": False,
        },
        {
            "prompt": "Inspect recent telemetry events for tool latency outliers exceeding 5000 ms.",
            "interactive": False,
        },
    ],
    "machine_gpu": [
        {
            "prompt": "Inspect local GPU memory allocation, active compute context, and thermal telemetry metrics.",
            "interactive": False,
        },
        {
            "prompt": "Report hardware device inventory and available Vulkan/OpenCL compute queues.",
            "interactive": False,
        },
    ],
    "settings": [
        {
            "prompt": "Review current workbench workspace preferences, default units, and scratch directory path.",
            "interactive": False,
        },
        {
            "prompt": "Validate persistent user preferences schema and verify read-only permissions on config assets.",
            "interactive": False,
        },
    ],
}

_CLARIFICATION_PHRASES = (
    "did you mean",
    "do you want me to",
    "should i",
    "would you like me to",
    "would you like",
    "would you prefer",
    "please clarify",
    "please confirm",
    "could you clarify",
    "could you specify",
    "could you confirm",
    "which one would you prefer",
    "which one do you prefer",
    "can you provide more details",
    "vuoi che",
    "intendi",
    "confermi",
    "preferisci",
    "fammi sapere",
)


def generate_request_set(
    *, count: int = 40, heldout_fraction: float = 0.25, seed: int = 42
) -> dict[str, Any]:
    """Generate a deterministic, generic public request set for explorer evaluation.

    Ensures:
    1. Family weighting >= 60% towards design and research.
    2. Zero private project, organism, or part names.
    3. Deterministic dev/heldout splits with task IDs and interactive fields.
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    if not (0.0 <= heldout_fraction <= 1.0):
        raise ValueError("heldout_fraction must be between 0.0 and 1.0")

    rng = random.Random(seed)

    # Calculate quota for design + research (>= 60%, default ~75%)
    primary_families = list(DESIGN_FAMILIES + RESEARCH_FAMILIES)
    secondary_families = list(OTHER_FAMILIES)

    # Target 75% primary families, min 60%
    primary_target = min(max(int(count * 0.65), int(count * 0.75)), count)
    secondary_target = count - primary_target

    selected_families: list[str] = []
    # Interleave primary families deterministically
    p_idx = 0
    s_idx = 0
    for i in range(count):
        if (
            len(selected_families) < primary_target
            and (i % 4 != 3 or secondary_target == 0)
        ) or s_idx >= secondary_target:
            fam = primary_families[p_idx % len(primary_families)]
            p_idx += 1
            selected_families.append(fam)
        else:
            fam = secondary_families[s_idx % len(secondary_families)]
            s_idx += 1
            selected_families.append(fam)

    # Deterministic heldout split assignment
    heldout_count = round(count * heldout_fraction)
    indices = list(range(count))
    rng.shuffle(indices)
    heldout_set = set(indices[:heldout_count])

    tasks: list[dict[str, Any]] = []
    template_counters: dict[str, int] = {}

    for idx, fam in enumerate(selected_families):
        templates = _GENERIC_TEMPLATES[fam]
        t_counter = template_counters.get(fam, 0)
        template = templates[t_counter % len(templates)]
        template_counters[fam] = t_counter + 1

        split = "heldout" if idx in heldout_set else "dev"
        task_id = f"exp_{idx + 1:03d}_{fam}"

        task_entry: dict[str, Any] = {
            "id": task_id,
            "split": split,
            "family": fam,
            "prompt": template["prompt"],
            "interactive": bool(template.get("interactive", False)),
        }
        if template.get("interactive"):
            task_entry["user_intent"] = template["user_intent"]
            task_entry["preferences"] = template["preferences"]

        tasks.append(task_entry)

    return {
        "set_id": "explorer_public_v1",
        "cloud_ok": True,
        "seed": seed,
        "tasks": tasks,
    }


def _parse_ts(val: Any) -> float | None:
    """Parse numeric or ISO timestamp into milliseconds epoch."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        num = float(val)
        # Unix epoch seconds is roughly between 1e8 (1973) and 1e11 (5138)
        if 1e8 <= num < 1e11:
            return num * 1000.0
        return num
    if isinstance(val, str):
        clean = val.strip()
        try:
            num = float(clean)
            if 1e8 <= num < 1e11:
                return num * 1000.0
            return num
        except ValueError:
            pass
        try:
            dt = datetime.datetime.fromisoformat(clean.replace("Z", "+00:00"))
            return dt.timestamp() * 1000.0
        except ValueError:
            return None
    return None


def _compute_duration_ms(started_at: Any, completed_at: Any) -> float | None:
    """Calculate duration in ms between started_at and completed_at."""
    start_ms = _parse_ts(started_at)
    end_ms = _parse_ts(completed_at)
    if start_ms is not None and end_ms is not None:
        diff = end_ms - start_ms
        return round(diff, 2) if diff >= 0 else 0.0
    return None


def extract_thread_tool_trace(
    data_root: Path | str, thread_id: str
) -> list[dict[str, Any]]:
    """Extract ordered tool-call trace records for thread_id from proof SQLite DB.

    Reads hermes.tool_started, hermes.tool_result, and hermes.agent_event from events table,
    joins on call_id / target_id, strips mcp__jarvis__ prefix, and returns ordered trace.
    """
    conn = _open_proof_db(data_root)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT created_at, event_type, target_id, payload FROM events "
            "WHERE event_type IN ('hermes.agent_event', 'hermes.tool_started', 'hermes.tool_result') "
            "ORDER BY created_at ASC, rowid ASC"
        ).fetchall()
    finally:
        conn.close()

    call_ids: set[str] = set()
    started_by_call: dict[str, dict[str, Any]] = {}
    result_by_call: dict[str, dict[str, Any]] = {}
    order_of_calls: list[str] = []

    for created_at, event_type, target_id, payload_raw in rows:
        payload: dict[str, Any] = {}
        if payload_raw:
            try:
                payload = (
                    json.loads(payload_raw)
                    if isinstance(payload_raw, str)
                    else dict(payload_raw)
                )
            except (ValueError, TypeError):
                payload = {}

        call_id = str(payload.get("call_id") or target_id or "")

        if event_type == "hermes.agent_event":
            session_ref = payload.get("session_ref")
            if (
                isinstance(session_ref, dict)
                and session_ref.get("jarvis_thread_id") == thread_id
                and payload.get("kind") == "tool.call"
            ):
                corr = str(payload.get("correlation_id") or call_id)
                if corr:
                    call_ids.add(corr)
                    if corr not in order_of_calls:
                        order_of_calls.append(corr)

        elif event_type == "hermes.tool_started":
            cid = call_id or str(target_id or "")
            if cid:
                started_by_call[cid] = {
                    "created_at": created_at,
                    "payload": payload,
                }
                if cid not in order_of_calls:
                    order_of_calls.append(cid)

        elif event_type == "hermes.tool_result":
            cid = call_id or str(target_id or "")
            session_ref = payload.get("session_ref")
            if (
                isinstance(session_ref, dict)
                and session_ref.get("jarvis_thread_id") == thread_id
            ):
                if cid:
                    call_ids.add(cid)
            if cid:
                result_by_call[cid] = {
                    "created_at": created_at,
                    "payload": payload,
                }
                if cid not in order_of_calls:
                    order_of_calls.append(cid)

    traces: list[dict[str, Any]] = []
    seq = 1
    for cid in order_of_calls:
        if cid not in call_ids:
            continue
        start_info = started_by_call.get(cid, {})
        res_info = result_by_call.get(cid, {})

        start_payload = start_info.get("payload", {})
        res_payload = res_info.get("payload", {})

        raw_tool_name = (
            res_payload.get("tool_name") or start_payload.get("tool_name") or ""
        )
        tool_name = _bare_tool_name(raw_tool_name)
        capability_id = str(
            res_payload.get("capability_id")
            or start_payload.get("capability_id")
            or ""
        )
        arguments = (
            res_payload.get("arguments") or start_payload.get("arguments") or {}
        )
        if not isinstance(arguments, dict):
            arguments = {}

        status = str(
            res_payload.get("status") or ("running" if start_info else "unknown")
        )
        error_code = res_payload.get("error_code")

        started_at = start_info.get("created_at")
        completed_at = res_info.get("created_at")

        duration_ms = _compute_duration_ms(started_at, completed_at)
        result_digest = res_payload.get("result_digest")
        evidence_refs = res_payload.get("evidence_refs") or []

        traces.append(
            {
                "seq": seq,
                "call_id": cid,
                "tool_name": tool_name,
                "capability_id": capability_id,
                "arguments": arguments,
                "status": status,
                "error_code": error_code,
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_ms": duration_ms,
                "result_digest": result_digest,
                "evidence_refs": evidence_refs,
            }
        )
        seq += 1

    return traces


def wrap_explorer_prompt(prompt: str) -> str:
    """Append concise work-report instructions to explorer prompt."""
    instructions = (
        "\n\n[Explorer Instructions]\n"
        "Before each action, write one short line: 'Intent: doing X to get Y'.\n"
        "At the conclusion of your response, provide a structured work report with these optional sections:\n"
        "STEPS_TAKEN:\n"
        "- concise summary of step\n"
        "WORKAROUNDS:\n"
        "- any workarounds used due to missing or failed tools\n"
        "WISH_LIST:\n"
        "- missing deterministic tools or capabilities that would have simplified this task\n"
        "(Note: this is a work report for engineering telemetry, not internal reasoning. A shorter report or omitting sections if not applicable is completely acceptable.)"
    )
    return f"{prompt.rstrip()}{instructions}"


def parse_work_report(answer: str) -> dict[str, list[str]]:
    """Deterministically parse work report sections from an assistant answer. Never raises."""
    result: dict[str, list[str]] = {
        "steps_taken": [],
        "workarounds": [],
        "wish_list": [],
    }
    if not isinstance(answer, str) or not answer.strip():
        return result

    section_patterns = {
        "steps_taken": re.compile(
            r"(?:^|\n)\s*(?:#+\s*)?(?:\*{1,2})?STEPS[_\s]TAKEN(?:\*{1,2})?:?\s*",
            re.IGNORECASE,
        ),
        "workarounds": re.compile(
            r"(?:^|\n)\s*(?:#+\s*)?(?:\*{1,2})?WORKAROUNDS(?:\*{1,2})?:?\s*",
            re.IGNORECASE,
        ),
        "wish_list": re.compile(
            r"(?:^|\n)\s*(?:#+\s*)?(?:\*{1,2})?WISH[_\s]LIST(?:\*{1,2})?:?\s*",
            re.IGNORECASE,
        ),
    }

    matches: list[tuple[int, int, str]] = []
    for section_key, pat in section_patterns.items():
        for m in pat.finditer(answer):
            matches.append((m.start(), m.end(), section_key))

    if not matches:
        return result

    matches.sort(key=lambda x: x[0])

    for i, (_start, end, section_key) in enumerate(matches):
        next_start = matches[i + 1][0] if i + 1 < len(matches) else len(answer)
        block = answer[end:next_start].strip()
        lines = block.splitlines()
        items: list[str] = []
        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue
            if line_str.startswith("(") and "work report" in line_str.lower():
                continue
            cleaned = re.sub(r"^[-*•]\s+|\d+\.\s+", "", line_str).strip()
            if cleaned and not cleaned.startswith(("#", "```")):
                items.append(cleaned)
        result[section_key] = items

    return result


def needs_clarification(answer: str) -> bool:
    """Return True if assistant answer asks a clarifying question or confirmation."""
    if not isinstance(answer, str) or not answer.strip():
        return False

    text = answer
    lower = text.lower()
    for phrase in _CLARIFICATION_PHRASES:
        if phrase in lower:
            return True

    clean_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith(("```", "|", "#", "-", "*"))
    ]
    if clean_lines and clean_lines[-1].endswith("?"):
        return True
    for line in clean_lines:
        if any(
            w in line.lower()
            for w in ("?", "please choose", "select an option", "which option")
        ):
            if line.endswith("?"):
                return True

    return False


class SimulatedMaintainer:
    """Simulated maintainer for interactive exploration follow-up turns."""

    def __init__(
        self,
        *,
        provider: str = "claude",
        model: str = "haiku",
        timeout: float = 60.0,
        responder: (
            Callable[[dict[str, Any], list[dict[str, str]]], str] | None
        ) = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.timeout = timeout
        self.responder = responder

    def respond(self, task: dict[str, Any], turns: list[dict[str, str]]) -> str:
        """Generate simulated maintainer response to an assistant query."""
        if self.responder is not None:
            return self.responder(task, turns)

        user_intent = str(task.get("user_intent", "")).strip()
        preferences = str(task.get("preferences", "")).strip()

        parts: list[str] = []
        if user_intent:
            parts.append(f"My goal is: {user_intent}.")
        if preferences:
            parts.append(f"Constraints and preferences: {preferences}.")
        if not parts:
            parts.append(
                "Please proceed with the standard engineering configuration."
            )
        return " ".join(parts)


def run_exploration(
    *,
    requests_path: Path | str,
    base_url: str,
    out_dir: Path | str,
    data_root: Path | str,
    workspace_id: str = "default",
    role_id: str | None = None,
    route_class: str | None = None,
    max_clarification_rounds: int = 2,
    simulated_maintainer: SimulatedMaintainer | None = None,
    allow_any_clarification: bool = False,
    resume: bool = False,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Run sequential exploration against proof backend with trace extraction and checkpoints."""
    validate_base_url(base_url)

    vault = vault_guard.vault_root()
    for p in (requests_path, data_root, out_dir):
        if vault_guard.touches_vault(p, vault):
            raise PermissionError(vault_guard.REFUSED_MESSAGE)

    req_file = Path(requests_path)
    if not req_file.is_file():
        raise FileNotFoundError(f"Requests file not found: {requests_path}")

    req_data = json.loads(req_file.read_text(encoding="utf-8"))
    if not isinstance(req_data, dict) or not isinstance(req_data.get("tasks"), list):
        raise ValueError(
            "Requests file must contain a JSON object with a 'tasks' list"
        )

    tasks: list[dict[str, Any]] = req_data["tasks"]
    set_id = str(req_data.get("set_id", "explorer"))

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    log_file = out_path / "exploration_log.json"
    traces_file = out_path / "traces.jsonl"

    completed_ids: set[str] = set()
    requests_log: list[dict[str, Any]] = []

    if resume and log_file.is_file():
        try:
            prior = json.loads(log_file.read_text(encoding="utf-8"))
            if isinstance(prior, dict) and isinstance(prior.get("requests"), list):
                for item in prior["requests"]:
                    if isinstance(item, dict) and item.get("request_id"):
                        completed_ids.add(str(item["request_id"]))
                        requests_log.append(item)
        except Exception:
            pass

    clean_base = base_url.rstrip("/")

    for task in tasks:
        task_id = str(task.get("id", ""))
        if not task_id or (resume and task_id in completed_ids):
            continue

        raw_prompt = str(task.get("prompt", ""))
        wrapped_prompt = wrap_explorer_prompt(raw_prompt)
        start_time = time.time()
        deadline = time.monotonic() + timeout

        thread_payload = {
            "workspace_id": workspace_id,
            "title": f"explorer-{task_id}",
        }
        thread_res = _http_request(
            f"{clean_base}/ai/threads",
            method="POST",
            payload=thread_payload,
            deadline=deadline,
        )
        thread_id = str(thread_res["id"])

        submit_payload: dict[str, Any] = {
            "request_id": f"exp-{uuid.uuid4().hex[:12]}",
            "prompt": wrapped_prompt,
            "task_kind": "general",
        }
        if route_class:
            submit_payload["route_class"] = route_class
        if role_id:
            submit_payload["role_id"] = role_id

        query = urllib.parse.urlencode({"workspace_id": workspace_id})
        submit_url = f"{clean_base}/ai/threads/{thread_id}/interactions?{query}"
        interaction: dict[str, Any] = {}
        interaction_id = ""
        try:
            submit_res = _http_request(
                submit_url, method="POST", payload=submit_payload, deadline=deadline
            )
            interaction = submit_res["interaction"]
            interaction_id = interaction["id"]
            flow_state = str(interaction.get("flow_state", "running"))
        except TimeoutError:
            flow_state = "timed_out"

        terminal_reason: Any = "timeout" if flow_state == "timed_out" else None
        while flow_state not in TERMINAL_FLOW_STATES:
            rem = deadline - time.monotonic()
            if rem <= 0:
                flow_state, terminal_reason = "timed_out", "timeout"
                break
            time.sleep(min(0.2, rem))
            read_url = f"{clean_base}/ai/threads/{thread_id}?{query}"
            try:
                read_res = _http_request(read_url, deadline=deadline)
            except TimeoutError:
                flow_state, terminal_reason = "timed_out", "timeout"
                break
            interaction = next(
                (
                    item
                    for item in read_res.get("interactions", [])
                    if item.get("id") == interaction_id
                ),
                interaction,
            )
            flow_state = str(interaction.get("flow_state", "running"))

        if flow_state != "timed_out":
            terminal_reason = interaction.get("terminal_reason")
        assistant_text = str(interaction.get("assistant_text", ""))

        turns: list[dict[str, str]] = [
            {"role": "user", "content": wrapped_prompt},
            {"role": "assistant", "content": assistant_text},
        ]

        # Multi-turn clarification loop
        rounds = 0
        while (
            flow_state != "timed_out"
            and rounds < max_clarification_rounds
            and simulated_maintainer is not None
            and needs_clarification(assistant_text)
            and (task.get("interactive") or allow_any_clarification)
        ):
            maintainer_reply = simulated_maintainer.respond(task, turns)
            turns.append({"role": "user", "content": maintainer_reply})
            rounds += 1

            sub_payload = {
                "request_id": f"exp-{uuid.uuid4().hex[:12]}",
                "prompt": maintainer_reply,
                "task_kind": "general",
            }
            if route_class:
                sub_payload["route_class"] = route_class
            if role_id:
                sub_payload["role_id"] = role_id

            try:
                sub_res = _http_request(
                    submit_url, method="POST", payload=sub_payload, deadline=deadline
                )
            except TimeoutError:
                flow_state, terminal_reason = "timed_out", "timeout"
                break
            interaction = sub_res["interaction"]
            interaction_id = interaction["id"]
            flow_state = str(interaction.get("flow_state", "running"))

            while flow_state not in TERMINAL_FLOW_STATES:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    flow_state, terminal_reason = "timed_out", "timeout"
                    break
                time.sleep(min(0.2, rem))
                read_url = f"{clean_base}/ai/threads/{thread_id}?{query}"
                try:
                    read_res = _http_request(read_url, deadline=deadline)
                except TimeoutError:
                    flow_state, terminal_reason = "timed_out", "timeout"
                    break
                interaction = next(
                    (
                        item
                        for item in read_res.get("interactions", [])
                        if item.get("id") == interaction_id
                    ),
                    interaction,
                )
                flow_state = str(interaction.get("flow_state", "running"))

            if flow_state != "timed_out":
                terminal_reason = interaction.get("terminal_reason")
            assistant_text = str(interaction.get("assistant_text", ""))
            turns.append({"role": "assistant", "content": assistant_text})

        end_time = time.time()

        tool_trace = extract_thread_tool_trace(data_root, thread_id)
        metrics = read_job_metrics(data_root, start_time, end_time)
        work_report = parse_work_report(assistant_text)

        record: dict[str, Any] = {
            "request_id": task_id,
            "thread_id": thread_id,
            "family": task.get("family"),
            "flow_state": flow_state,
            "terminal_reason": terminal_reason,
            "transcript": turns,
            "tool_trace": tool_trace,
            "work_report": work_report,
            "metrics": metrics,
            "started_at": start_time,
            "completed_at": end_time,
        }

        requests_log.append(record)
        completed_ids.add(task_id)

        # Atomic checkpoint update to traces.jsonl and exploration_log.json
        with tempfile.NamedTemporaryFile(
            "w", dir=out_path, delete=False, encoding="utf-8"
        ) as tmp_traces:
            for item in requests_log:
                tmp_traces.write(json.dumps(item) + "\n")
            tmp_traces_path = Path(tmp_traces.name)
        tmp_traces_path.replace(traces_file)

        full_log = {
            "set_id": set_id,
            "complete": len(completed_ids) >= len(tasks),
            "requests": requests_log,
        }
        with tempfile.NamedTemporaryFile(
            "w", dir=out_path, delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(json.dumps(full_log, indent=2))
            tmp_path = Path(tmp.name)
        tmp_path.replace(log_file)

    return {
        "set_id": set_id,
        "complete": True,
        "requests": requests_log,
    }
