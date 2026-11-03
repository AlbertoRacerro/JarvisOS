type UnknownRecord = Record<string, unknown>;

function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as UnknownRecord : {};
}

function records(value: unknown): UnknownRecord[] {
  return Array.isArray(value) ? value.filter((item): item is UnknownRecord => item !== null && typeof item === "object" && !Array.isArray(item)) : [];
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function humanizeReasonCode(value: string | null | undefined, fallback: string): string {
  if (!value) return fallback;
  return value.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function runtimeRelationship(alignment: string): string | null {
  if (alignment === "aligned") return "Local runtime matches remote";
  if (alignment === "local_behind") return "Local runtime behind remote";
  if (alignment === "divergent") return "Local and remote diverge";
  return null;
}

export function runtimeDeltaSummary(value: UnknownRecord, alignment = "unknown", reason: string | null = null) {
  const relationship = runtimeRelationship(alignment);
  const canonicalUnknown = relationship === null;
  const files = canonicalUnknown ? [] : records(value.files).map((file) => ({
    name: String(file.filename ?? file.path ?? "Unnamed file"),
    status: typeof file.status === "string" ? file.status : null
  }));
  return {
    relation: canonicalUnknown ? "unknown" : alignment,
    relationship: relationship ?? "Runtime relationship unavailable",
    aheadBy: canonicalUnknown ? null : finiteNumber(value.ahead_by),
    behindBy: canonicalUnknown ? null : finiteNumber(value.behind_by),
    files,
    status: canonicalUnknown ? "unavailable" : typeof value.status === "string" ? value.status : "unavailable",
    partial: canonicalUnknown ? false : value.partial === true,
    explanation: canonicalUnknown && reason ? humanizeReasonCode(reason, "Runtime relationship unavailable") : null
  };
}

export function pullRequestEvidenceSummary(value: UnknownRecord) {
  const pr = record(value.pr);
  const checks = records(record(value.checks).check_runs);
  const reviews = records(record(value.reviews).reviews);
  const checkCount = (predicate: (item: UnknownRecord) => boolean) => checks.filter(predicate).length;
  const reviewCount = (predicate: (item: UnknownRecord) => boolean) => reviews.filter(predicate).length;
  return {
    title: typeof pr.title === "string" && pr.title.trim() ? pr.title : `Pull request ${String(pr.number ?? "")}`.trim(),
    state: pr.merged === true ? "merged" : typeof pr.state === "string" ? pr.state : "unknown",
    checks: {
      total: checks.length,
      passing: checkCount((item) => item.conclusion === "success" && item.stale !== true),
      failing: checkCount((item) => typeof item.conclusion === "string" && !["success", "neutral", "skipped"].includes(item.conclusion) && item.stale !== true),
      pending: checkCount((item) => item.status !== "completed" && item.stale !== true),
      stale: checkCount((item) => item.stale === true)
    },
    reviews: {
      total: reviews.length,
      approved: reviewCount((item) => String(item.state).toUpperCase() === "APPROVED" && item.stale !== true),
      changesRequested: reviewCount((item) => String(item.state).toUpperCase() === "CHANGES_REQUESTED" && item.stale !== true),
      stale: reviewCount((item) => item.stale === true)
    }
  };
}

export function providerLocation(requiresNetwork: boolean, executionClass: string): string {
  if (requiresNetwork || executionClass === "external_provider") return "Uses a network service";
  if (executionClass === "synthetic") return "Runs without an external service";
  return "Runs locally";
}

export function savedPaidAiSummary(paidAiEnabled: boolean, monthlyBudgetUsd: number): string {
  return paidAiEnabled
    ? `Paid AI is enabled within the saved $${monthlyBudgetUsd} monthly limit.`
    : "Paid AI is disabled in saved settings; no paid external request is permitted.";
}

function persistedCredentialMeaning(persistedState: string): string {
  if (persistedState === "usable") return "Stored credential ready";
  if (persistedState === "corrupted") return "Stored credential damaged";
  if (persistedState === "unavailable") return "Secure credential store unavailable";
  if (persistedState === "not_supported") return "Stored credentials not supported";
  if (persistedState === "absent") return "No stored credential";
  return "Stored credential state unavailable";
}

export function credentialMeaning(effectiveSource: string, persistedState: string): string {
  const persisted = persistedCredentialMeaning(persistedState);
  if (effectiveSource === "not_required") return `No credential required · ${persisted}.`;
  if (effectiveSource === "environment") return `Environment credential active · ${persisted}.`;
  if (effectiveSource === "secure_persisted") return `Securely stored credential active · ${persisted}.`;
  if (effectiveSource === "invalid") return `Environment credential invalid · ${persisted}.`;
  if (effectiveSource === "absent") return `No effective credential · ${persisted}.`;
  return `Credential availability unknown · ${persisted}.`;
}
