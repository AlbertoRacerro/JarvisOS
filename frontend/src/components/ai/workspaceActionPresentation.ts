import type { ActionOutcome, SurfaceRef } from "../../api/workspaceActions";
import type { StageSelection } from "../../app/selection";

// `revision` is the draft owner revision ("seq:digest") on screen; `epoch` is stamped by workspaceActionSurface.
export type ProcessSurface = { draft_id: string | null; revision: string | null; process_selection: SurfaceRef["process_selection"]; epoch: number };
export function buildSurfaceRef(routeId: string, selection: StageSelection | null, process: ProcessSurface, studioModelId: string | null = null): SurfaceRef {
  if (routeId === "design-studio") return { route_id: routeId, studio_model_id: studioModelId };
  if (routeId === "design-process") {
    return { route_id: routeId, draft_id: process.draft_id, process_selection: process.process_selection ?? [] };
  }
  if (routeId === "design-bluecad") {
    const validPart = selection?.kind === "bluecad-part" ? selection : null;
    const candidate = selection?.kind === "bluecad-part" || selection?.kind === "bluecad-binding-status"
      ? selection.candidateId
      : selection?.kind === "record" && selection.ref.resource === "bluecad-candidate" ? selection.ref.recordId : null;
    return { route_id: routeId, candidate_id: candidate, bluecad_part_ids: validPart ? [validPart.partId] : [] };
  }
  return { route_id: routeId };
}

export function isToolCallShaped(text: string | null | undefined): boolean {
  if (!text) return false;
  const normalized = text.trim();
  const fences = [...normalized.matchAll(/(```|~~~)([a-z0-9_-]*)\s*\n?([\s\S]*?)\n?\s*\1/gi)];
  const candidates = [normalized, ...fences.map(match => match[3])];
  const actionOps = new Set([
    "set_value", "set_unit_model", "set_reaction", "add_unit", "insert_unit_after", "connect", "disconnect", "mirror", "move",
    "rename", "delete", "duplicate_part", "set_part_param", "move_part", "delete_part"
  ]);
  const containsWorkspaceAction = (value: unknown): boolean => {
    if (Array.isArray(value)) return value.some(containsWorkspaceAction);
    if (!value || typeof value !== "object") return false;
    const record = value as Record<string, unknown>;
    if (typeof record.op === "string" && actionOps.has(record.op)) return true;
    return Object.values(record).some(item => item && typeof item === "object" && containsWorkspaceAction(item));
  };
  const embeddedValues = (value: string): unknown[] => {
    const parsed: unknown[] = [];
    for (let start = 0; start < value.length; start += 1) {
      if (value[start] !== "{" && value[start] !== "[") continue;
      const stack: string[] = [];
      let quoted = false;
      let escaped = false;
      for (let end = start; end < value.length; end += 1) {
        const char = value[end];
        if (quoted) {
          if (escaped) escaped = false;
          else if (char === "\\") escaped = true;
          else if (char === '"') quoted = false;
          continue;
        }
        if (char === '"') { quoted = true; continue; }
        if (char === "{" || char === "[") stack.push(char);
        else if (char === "}" || char === "]") {
          const opening = stack.pop();
          if ((opening === "{" && char !== "}") || (opening === "[" && char !== "]")) break;
          if (stack.length === 0) {
            try { parsed.push(JSON.parse(value.slice(start, end + 1)) as unknown); } catch { /* Ignore malformed fragments. */ }
            break;
          }
        }
      }
    }
    return parsed;
  };
  if (fences.some(match => match[2].toLowerCase() === "jarvis-actions")) return true;
  return candidates.some(candidate => {
    const value = candidate.trim();
    if (/<\|(?:tool_call|python_tag|function_call)\|>|<\|im_start\|>\s*(?:tool_call|function_call)\b|<start_function_call>|<\s*function(?:=|\s)|assistant\s+to=/i.test(value)) return true;
    if (/(?:mcp__jarvis__)?jarvis_(?:process|bluecad)_(?:act|read)\s*[({]/i.test(value)) return true;
    if (/\{\s*[\s\S]{0,1000}"name"\s*:\s*"[^"]+"[\s\S]{0,1000}"arguments"\s*:/i.test(value)) return true;
    if (/\{\s*[\s\S]{0,1000}"arguments"\s*:[\s\S]{0,1000}"name"\s*:\s*"[^"]+"/i.test(value)) return true;
    try {
      const parsed: unknown = JSON.parse(value);
      if (containsWorkspaceAction(parsed)) return true;
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        const record = parsed as Record<string, unknown>;
        if (typeof record.name === "string" && /(?:mcp__jarvis__)?jarvis_(?:process|bluecad)_(?:act|read)$/i.test(record.name)) return true;
        if (Array.isArray(record.tool_calls) && record.tool_calls.some(call => call && typeof call === "object" && "name" in call)) return true;
      }
    } catch { /* Prose and non-JSON answers remain visible. */ }
    if (embeddedValues(value).some(containsWorkspaceAction)) return true;
    return false;
  });
}

export function actionStatePresentation(state: ActionOutcome["state"]): { label: string; tone: "success" | "pending" | "plain" | "muted" } {
  switch (state) {
    case "applied": return { label: "Applied", tone: "success" };
    case "proposed": return { label: "Proposed", tone: "pending" };
    case "refused": return { label: "Refused", tone: "plain" };
    case "stale": return { label: "Stale", tone: "plain" };
    case "dismissed": return { label: "Dismissed", tone: "muted" };
    case "undone": return { label: "Undone", tone: "muted" };
  }
}

const RATE_LAW_LABELS: Record<string, string> = { power_law_arrhenius: "Power law (Arrhenius)", monod: "Monod", haldane: "Haldane / Andrews" };

/**
 * Card text for one validated process action. The backend composes `summary`; this is the frontend's own
 * wording for requests whose summary is missing, so `set_unit_model` never falls back to raw JSON.
 */
export function describeProcessAction(action: Record<string, unknown>): string | null {
  const text = (value: unknown) => (typeof value === "string" && value ? value : null);
  if (action.op === "set_reaction") {
    const unit = text(action.unit) ?? "the reactor";
    const reaction = action.reaction as { name?: unknown; rate_law?: { form?: unknown } | null } | null;
    const name = text(reaction?.name);
    if (!reaction) return `Remove ${name ? `reaction ${name}` : "a reaction"} from ${unit}`;
    const form = text(reaction.rate_law?.form) ?? "power_law_arrhenius";
    return `Set ${RATE_LAW_LABELS[form] ?? "rate-law"} reaction${name ? ` ${name}` : ""} on ${unit}`;
  }
  if (action.op === "set_unit_model") {
    const unit = text(action.unit) ?? "the unit";
    const card = text(action.card);
    return card ? `Use model card ${card} for ${unit}` : `Clear the model card for ${unit}`;
  }
  return null;
}

export function actionSummaryText(action: Pick<ActionOutcome, "summary" | "request">): string {
  if (action.summary.trim()) return action.summary;
  const actions = (action.request as { actions?: unknown }).actions;
  const lines = Array.isArray(actions)
    ? actions.map((item) => (item && typeof item === "object" ? describeProcessAction(item as Record<string, unknown>) : null)).filter((line): line is string => Boolean(line))
    : [];
  return lines.join("; ") || "Workspace action";
}

export function actionOriginLabel(origin: ActionOutcome["origin"]): string {
  return origin.kind === "local" ? "Jarvis local" : `Relay${origin.model ? ` · ${origin.model}` : ""}`;
}

const RELAY_FAILURES: Record<string, string> = {
  relay_gateway_disabled: "Relay is turned off on this machine, so nothing was sent",
  relay_agent_login_missing: "the Relay agent is not signed in on this machine",
  relay_agent_binary_missing: "the Relay agent program is not installed on this machine",
  relay_agent_not_allowed: "this Relay agent is not allowed on this machine",
  relay_run_failed: "the Relay run failed",
  relay_run_timeout: "the agent took too long and was stopped",
  relay_run_interrupted: "the run was interrupted (JarvisOS restarted while it was working)",
  relay_run_launch_failed: "the agent could not be started",
  relay_agent_error: "the agent reported an error",
  relay_output_unparseable: "the agent returned an unreadable result",
  relay_run_result_unreadable: "the result could not be read",
  prompt_secret_detected: "the text looks like it contains a secret",
  prompt_sanitization_required: "the text needs a cloud-safe rewrite first",
  prompt_classification_required: "the text was not confirmed as cloud-safe"
};

export function relayFailureMessage(reasonCode: string | null, resultText: string | null): string {
  if (reasonCode === "relay_agent_error" && resultText && /you've hit your session limit/i.test(resultText)) {
    const reset = resultText.match(/\bresets\s+([0-9]{1,2}:[0-9]{2}\s*[ap]m)\b/i)?.[1];
    return `Relay agent unavailable: session limit reached${reset ? ` (resets ${reset})` : ""}`;
  }
  return RELAY_FAILURES[reasonCode ?? ""] ?? "the run did not finish";
}
