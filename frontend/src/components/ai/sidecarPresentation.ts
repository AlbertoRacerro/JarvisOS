export type CurrencyCode = "USD" | "EUR";

export function formatMoney(value: string | number | null | undefined, currency: CurrencyCode): string {
  if (value === null || value === undefined || value === "") return "unknown";
  const amount = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(amount)) return "unknown";

  const formatter = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumSignificantDigits: 4
  });
  if (amount === 0) return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  }).format(0);
  if (Math.abs(amount) < 0.01) {
    const cent = formatter.format(0.01);
    return `${amount < 0 ? "less than -" : "less than "}${cent}`;
  }
  return formatter.format(amount);
}

export function sortCreatedChronologically<T extends { created_at: string; id: string }>(items: readonly T[]): T[] {
  return [...items].sort((left, right) => {
    const byTime = Date.parse(left.created_at) - Date.parse(right.created_at);
    return Number.isFinite(byTime) && byTime !== 0 ? byTime : left.id.localeCompare(right.id);
  });
}

const CLOUD_FAILURES: Record<string, string> = {
  provider_gate_blocked: "Not sent — paid cloud AI is off or over budget. Nothing left your computer.",
  provider_not_configured: "Not sent — no paid cloud provider is configured.",
  provider_unavailable: "Not sent — the paid cloud provider is unavailable.",
  budget_exceeded: "Not sent — this request is over the configured cloud AI budget.",
  insufficient_budget: "Not sent — there is not enough cloud AI budget for this request."
};

export function cloudFailureMessage(reasonCode: string | null | undefined): string {
  return CLOUD_FAILURES[reasonCode ?? ""] ?? "The cloud request was not sent because it could not pass the provider and budget checks.";
}
