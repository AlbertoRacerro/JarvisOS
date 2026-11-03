import type { ActionOutcome } from "../../api/workspaceActions";

type ContractGroup = { title: string; rows: string[] };
export type StudioProposalView = {
  title: string;
  category: string;
  mode: "create" | "update";
  question: string;
  groups: ContractGroup[];
  assumptions: string;
  sourceDiff: string;
};

/** Read only presentation of an already validated action. Rendering never executes source. */
export function studioProposalView(action: ActionOutcome): StudioProposalView | null {
  if (action.surface !== "studio") return null;
  const item = Array.isArray(action.request.actions) ? action.request.actions[0] : null;
  if (!item || typeof item !== "object") return null;
  const proposal = item as Record<string, unknown>;
  if (proposal.op !== "studio_draft") return null;
  const contract = proposal.contract && typeof proposal.contract === "object" ? proposal.contract as Record<string, unknown> : {};
  const groups = Object.entries(contract).filter(([, value]) => Array.isArray(value)).map(([title, value]) => ({
    title: title.replace(/_/g, " "),
    rows: (value as unknown[]).map((row, index) => {
      if (typeof row === "string" || typeof row === "number") return String(row);
      const item = row && typeof row === "object" ? row as Record<string, unknown> : {};
      const label = String(item.label ?? item.name ?? `Item ${index + 1}`);
      const name = item.name && item.label ? ` (${String(item.name)})` : "";
      const unit = item.unit ? ` [${String(item.unit)}]` : "";
      const description = item.description ? ` — ${String(item.description)}` : "";
      const bounds = item.min != null || item.max != null ? ` · bounds ${String(item.min ?? "−∞")}…${String(item.max ?? "+∞")}` : "";
      return `${label}${name}${unit}${description}${bounds}`;
    })
  }));
  const source = typeof proposal.source === "string" ? proposal.source : "";
  const before = action.changes.find((change) => change.label.toLowerCase() === "source")?.before;
  const sourceDiff = `${before == null ? "" : `--- Current Python\n${before.split("\n").map((line) => `- ${line}`).join("\n")}\n`}+++ Proposed Python\n${source.split("\n").map((line) => `+ ${line}`).join("\n")}`;
  return {
    title: String(proposal.name ?? "Studio draft"), category: String(proposal.category ?? "unknown"),
    mode: proposal.mode === "update" ? "update" : "create",
    question: typeof proposal.engineering_question === "string" ? proposal.engineering_question : "",
    groups,
    assumptions: typeof proposal.assumptions === "string" && proposal.assumptions ? proposal.assumptions : "No assumptions supplied. Review the model before testing.",
    sourceDiff
  };
}
