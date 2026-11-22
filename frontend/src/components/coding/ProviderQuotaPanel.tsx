import { useEffect, useState } from "react";
import {
  readProviderQuotas,
  type FreeModelQuota,
  type PrepaidBalance,
  type ProviderQuota,
  type ProviderQuotas,
  type ProviderQuotaWindow,
} from "../../api/coding";

// Account-status readings only (spec 217, 346). Automatic polling uses the server TTL;
// the explicit refresh asks the backend for new usage without starting a model turn.
const POLL_MS = 60_000;
const RADIUS = 22;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

const COLUMNS = [
  { key: "claude", label: "Claude" },
  { key: "codex", label: "OpenAI / Codex" },
  { key: "antigravity", label: "Google" },
  { key: "copilot", label: "GitHub" },
] as const;

const ROWS = [
  { id: "5 ore", kind: "five_hour", label: "5 ore" },
  { id: "Settimanale", kind: "weekly", label: "Settimanale" },
  { id: "Modello di frontiera", kind: "frontier", label: "Modello di frontiera" },
  { id: "Extra usage", kind: "extra", label: "Extra usage" },
] as const;

const FRONTIER_MODELS = ["Opus", "Argon"];

const REASONS: Record<string, string> = {
  session_expired: "Login scaduto",
  credentials_missing: "Credenziali assenti",
  not_authenticated: "Accesso non effettuato",
  rate_limited: "Limite richieste superato",
  provider_unreachable: "Servizio non raggiungibile",
  invalid_response: "Risposta non leggibile",
  agy_quota_unreadable: "Quota agy non leggibile",
  relay_limits_unreadable: "Limiti agent-relay non leggibili",
  relay_not_found: "agent-relay non trovato",
  relay_timeout: "Timeout agent-relay",
  relay_no_json: "Nessun output da agent-relay",
  agy_not_found: "agy non trovato",
  agy_timeout: "Timeout agy",
  agy_no_json: "Nessun output da agy",
  no_reading: "Nessuna lettura",
  no_usage_windows: "Nessuna finestra di utilizzo",
  codex_limits_unreadable: "Utilizzo Codex non disponibile",
  codex_not_found: "Codex non trovato",
  codex_timeout: "Timeout utilizzo Codex",
  codex_no_json: "Utilizzo Codex non disponibile; verificare accesso",
  copilot_quota_unreadable: "Quota Copilot non disponibile",
  copilot_not_found: "gh non trovato",
  copilot_timeout: "Timeout Copilot",
  copilot_no_json: "Nessun output da Copilot",
  cli_disabled: "CLI disabilitata",
  bucket_missing: "Dati quota assenti",
  no_entitlement: "Nessun abbonamento",
  invalid_reading: "Lettura non valida",
  provider_error: "Errore del provider",
};

function until(iso: string | null, now: Date): string | null {
  if (!iso) return null;
  const minutes = Math.round((new Date(iso).getTime() - now.getTime()) / 60_000);
  if (!Number.isFinite(minutes) || minutes <= 0) return null;
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours}h ${minutes % 60}m` : `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

function age(iso: string | null | undefined, now: Date): string | null {
  if (!iso) return null;
  const minutes = Math.round((now.getTime() - new Date(iso).getTime()) / 60_000);
  if (!Number.isFinite(minutes)) return null;
  if (minutes < 1) return "letto ora";
  if (minutes < 60) return `letto ${minutes} min fa`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `letto ${hours} h fa`;
  const days = Math.floor(hours / 24);
  return days === 1 ? "letto 1 giorno fa" : `letto ${days} giorni fa`;
}

export function QuotaRing({
  window,
  now,
  model,
}: Readonly<{ window: ProviderQuotaWindow; now: Date; model?: string | null }>) {
  const left = Math.max(0, Math.min(100, window.remaining_percent));
  const state = window.exhausted ? "exhausted" : window.stale ? "stale" : left <= 15 ? "low" : "ok";
  const reset = until(window.resets_at, now);
  return (
    <div className="coding-quota-cell" data-state={state}>
      <svg
        viewBox="0 0 56 56"
        role="img"
        aria-label={`${Math.round(left)}% rimasto${window.exhausted ? ", esaurito" : ""}${window.stale ? ", non aggiornato" : ""}`}
      >
        <circle className="coding-quota-track" cx="28" cy="28" r={RADIUS} />
        {left > 0 ? (
          <circle
            className="coding-quota-value"
            cx="28"
            cy="28"
            r={RADIUS}
            strokeDasharray={`${(left / 100) * CIRCUMFERENCE} ${CIRCUMFERENCE}`}
            transform="rotate(-90 28 28)"
          />
        ) : null}
        <text x="28" y="28" dominantBaseline="central" textAnchor="middle">
          {`${Math.round(left)}%`}
        </text>
      </svg>
      <span className="coding-quota-caption">
        {model ? <span className="coding-quota-model">{model}</span> : null}
        <strong>{window.exhausted ? "Esaurito" : "rimasto"}</strong>
        {window.stale ? <em>non aggiornato</em> : null}
        {reset ? <span title={`Si azzera tra ${reset}`}>↻ {reset}</span> : null}
      </span>
    </div>
  );
}

function Missing({ text, model }: Readonly<{ text: string; model?: string }>) {
  return (
    <div className="coding-quota-cell" data-state="missing">
      <span className="coding-quota-na">
        <span className="coding-quota-dot" />
      </span>
      <span className="coding-quota-caption">
        {model ? <span className="coding-quota-model">{model}</span> : null}
        <span>{text}</span>
      </span>
    </div>
  );
}

function EmptyCell({ text = "Non applicabile", model }: Readonly<{ text?: string; model?: string }>) {
  return (
    <div className="coding-quota-cell" data-state="not-applicable">
      <span className="coding-quota-na">
        <span className="coding-quota-dot" />
      </span>
      <span className="coding-quota-caption">
        {model ? <span className="coding-quota-model">{model}</span> : null}
        <span>{text}</span>
      </span>
    </div>
  );
}

function cell(
  provider: ProviderQuota,
  window: ProviderQuotaWindow | undefined,
  now: Date,
  model?: string,
  emptyText: string = "Non riportato"
) {
  if (provider.status !== "ok") {
    return <Missing text={REASONS[provider.reason ?? ""] ?? "Non disponibile"} model={model} />;
  }
  return window ? (
    <QuotaRing window={window} now={now} model={model} />
  ) : (
    <EmptyCell text={emptyText} model={model} />
  );
}

function modelName(w: ProviderQuotaWindow): string {
  const text = `${w.label ?? ""} ${w.id}`.toLowerCase();
  const known = FRONTIER_MODELS.find((name) => text.includes(name.toLowerCase()));
  return known ?? (w.label ?? w.id).split(/[ ·_-]/)[0];
}

export function ProviderQuotaView({
  data,
  error,
  now = new Date(),
  refreshing = false,
  onRefresh,
}: Readonly<{
  data: ProviderQuotas | null;
  error: string | null;
  now?: Date;
  refreshing?: boolean;
  onRefresh?: () => void;
}>) {
  const providers = data?.providers ?? [];
  const freeModels: readonly FreeModelQuota[] = data?.free_models ?? [];
  const prepaidBalances: readonly PrepaidBalance[] = data?.prepaid_balances ?? [];

  const agy = providers.find((p) => p.provider === "antigravity");
  const agyClaude = Boolean(agy && providers.some((p) => p.provider === "claude"));

  const agyRing = (id: "3p-5h" | "3p-weekly") => {
    if (!agy || agy.status !== "ok") return null;
    const w = agy.windows.find((item) => item.id === id);
    if (!w) return null;
    const spanLabel = id === "3p-5h" ? "5 ore" : "Settimanale";
    return (
      <div
        role="group"
        aria-label={`Claude via AGY · ${spanLabel}`}
        title={`Antigravity shared Claude & GPT pool${agy.as_of ? ` · observed at ${agy.as_of}` : ""}`}
      >
        {cell(agy, w, now, "Claude via AGY")}
      </div>
    );
  };

  const renderCell = (colKey: string, rowId: string, p: ProviderQuota | undefined) => {
    if (colKey === "claude") {
      if (rowId === "5 ore") {
        const cw = p?.windows.find((w) => w.kind === "five_hour");
        const agy5h = agyRing("3p-5h");
        if (p && p.status !== "ok") {
          return (
            <>
              <Missing text={REASONS[p.reason ?? ""] ?? "Non disponibile"} model={agyClaude ? "Claude Code" : undefined} />
              {agy5h}
            </>
          );
        }
        if (cw && agy5h) {
          return (
            <>
              <QuotaRing window={cw} now={now} model="Claude Code" />
              {agy5h}
            </>
          );
        }
        if (cw) return <QuotaRing window={cw} now={now} model={agyClaude ? "Claude Code" : undefined} />;
        if (agy5h) return agy5h;
        return <EmptyCell text="Non riportato" />;
      }
      if (rowId === "Settimanale") {
        if (!p) return <EmptyCell text="Non riportato" />;
        return cell(p, p.windows.find((w) => w.kind === "weekly"), now, undefined, "Non riportato");
      }
      if (rowId === "Modello di frontiera") {
        const fw = p?.windows.find((w) => w.kind === "frontier");
        const agyWeekly = agyRing("3p-weekly");
        if (p && p.status !== "ok") {
          return (
            <>
              <Missing text={REASONS[p.reason ?? ""] ?? "Non disponibile"} />
              {agyWeekly}
            </>
          );
        }
        if (fw && agyWeekly) {
          return (
            <>
              <QuotaRing window={fw} now={now} model={modelName(fw)} />
              {agyWeekly}
            </>
          );
        }
        if (fw) return <QuotaRing window={fw} now={now} model={modelName(fw)} />;
        if (agyWeekly) return agyWeekly;
        return <EmptyCell text="Non applicabile" />;
      }
      if (rowId === "Extra usage") {
        if (!p) return <EmptyCell text="Non applicabile" />;
        const pw = p.windows.find((w) => w.kind === "pool" && w.id !== "3p-5h" && w.id !== "3p-weekly");
        return pw ? <QuotaRing window={pw} now={now} /> : <EmptyCell text="Non applicabile" />;
      }
    }

    if (colKey === "codex") {
      if (!p) return <EmptyCell text={rowId === "Modello di frontiera" || rowId === "Extra usage" ? "Non applicabile" : "Non riportato"} />;
      if (rowId === "5 ore") {
        return cell(p, p.windows.find((w) => w.kind === "five_hour"), now, undefined, "Non riportato");
      }
      if (rowId === "Settimanale") {
        return cell(p, p.windows.find((w) => w.kind === "weekly"), now, undefined, "Non riportato");
      }
      if (rowId === "Modello di frontiera") {
        const fw = p.windows.find((w) => w.kind === "frontier");
        return fw ? <QuotaRing window={fw} now={now} model={modelName(fw)} /> : <EmptyCell text="Non applicabile" />;
      }
      if (rowId === "Extra usage") {
        const pw = p.windows.find((w) => w.kind === "pool");
        return pw ? <QuotaRing window={pw} now={now} /> : <EmptyCell text="Non applicabile" />;
      }
    }

    if (colKey === "antigravity") {
      if (!p) return <EmptyCell text={rowId === "Extra usage" ? "Non applicabile" : "Non riportato"} />;
      if (rowId === "5 ore") {
        return cell(p, p.windows.find((w) => w.kind === "five_hour"), now, undefined, "Non riportato");
      }
      if (rowId === "Settimanale") {
        return cell(p, p.windows.find((w) => w.kind === "weekly"), now, undefined, "Non riportato");
      }
      if (rowId === "Modello di frontiera") {
        const fw = p.windows.find((w) => w.kind === "frontier");
        return fw ? <QuotaRing window={fw} now={now} model={modelName(fw)} /> : <EmptyCell text="Non applicabile" />;
      }
      if (rowId === "Extra usage") {
        const pw = p.windows.find((w) => w.kind === "pool" && !w.id.startsWith("3p"));
        return pw ? <QuotaRing window={pw} now={now} /> : <EmptyCell text="Non applicabile" />;
      }
    }

    if (colKey === "copilot") {
      if (!p) return <EmptyCell text="Non applicabile" />;
      if (p.status !== "ok") {
        return <Missing text={REASONS[p.reason ?? ""] ?? "Non disponibile"} />;
      }
      if (rowId === "Extra usage") {
        const pw = p.windows.find((w) => w.kind === "pool" || w.id === "premium_interactions");
        return pw ? <QuotaRing window={pw} now={now} model="Copilot" /> : <EmptyCell text="Non applicabile" />;
      }
      return <EmptyCell text={rowId === "5 ore" || rowId === "Settimanale" ? "Non riportato" : "Non applicabile"} />;
    }

    return <EmptyCell text="Non applicabile" />;
  };

  return (
    <section className="coding-control-section coding-quota" aria-label="Utilizzo provider" aria-busy={refreshing}>
      <header className="coding-quota-header">
        <div className="coding-quota-title-row">
          <h3>Utilizzo provider</h3>
          <span className="coding-quota-asof">
            {data ? age(data.read_at, now) : error ? "non disponibile" : "lettura…"}
          </span>
        </div>
        {onRefresh ? (
          <button type="button" onClick={onRefresh} disabled={refreshing} className="coding-quota-refresh-btn">
            {refreshing ? "Aggiornamento…" : "↻ Aggiorna"}
          </button>
        ) : null}
      </header>

      {error ? (
        <p className="coding-control-empty" role="status">
          Utilizzo provider non disponibile: {error}
        </p>
      ) : null}

      {data ? (
        <div className="coding-quota-body">
          {/* Section 1 — Subscription grid */}
          <div className="coding-quota-section coding-quota-sub-grid">
            <div
              className="coding-quota-grid"
              style={{ gridTemplateColumns: `max-content repeat(${COLUMNS.length}, minmax(0, 1fr))` }}
            >
              <span />
              {COLUMNS.map((col) => {
                const p = providers.find((item) => item.provider === col.key);
                return (
                  <strong key={col.key} className="coding-quota-provider">
                    {col.label}
                    {p?.last_error ? (
                      <small>
                        Ultima lettura {age(p.as_of, now)?.replace(/^(?:letto )/, "") ?? "tempo fa"} — aggiornamento fallito
                      </small>
                    ) : p?.as_of ? (
                      <small title={`Rilevato il ${p.as_of}`}>{age(p.as_of, now)}</small>
                    ) : null}
                  </strong>
                );
              })}

              {ROWS.map((row) => (
                <div key={row.id} className="coding-quota-row" role="group" aria-label={row.label}>
                  <span className="coding-quota-rowlabel">{row.label}</span>
                  {COLUMNS.map((col) => {
                    const p = providers.find((item) => item.provider === col.key);
                    return (
                      <div key={col.key} data-provider={col.key} className="coding-quota-pair">
                        {renderCell(col.key, row.id, p)}
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          </div>

          {/* Section 2 — Modelli gratis ora */}
          <div className="coding-quota-section coding-free-models-section">
            <div className="coding-quota-section-header">
              <h4>Modelli gratis ora</h4>
            </div>
            {freeModels.length === 0 ? (
              <p className="coding-control-empty" role="status">
                Nessun modello gratis attivo
              </p>
            ) : (
              <div className="coding-free-models-grid">
                {freeModels.map((fm) => (
                  <div key={fm.id} className="coding-free-card" data-status={fm.status}>
                    <div className="coding-free-header">
                      <span className="coding-free-source">{fm.source}</span>
                      {fm.intelligence_index != null ? (
                        <span className="coding-free-score" title="Artificial Analysis Intelligence Index">
                          AA {fm.intelligence_index}
                        </span>
                      ) : null}
                    </div>
                    <div className="coding-free-body">
                      <strong className="coding-free-name">{fm.label}</strong>
                      {fm.model_id ? <span className="coding-free-model">{fm.model_id}</span> : null}
                    </div>
                    <div className="coding-free-indicator">
                      {fm.exhausted || fm.status === "exhausted" ? (
                        <span className="coding-free-badge exhausted">Esaurito</span>
                      ) : fm.status === "throttled" ? (
                        <span className="coding-free-badge throttled">Rallentato</span>
                      ) : fm.status === "unqualified" ? (
                        <span className="coding-free-badge unqualified">Non qualificato</span>
                      ) : fm.limit_kind === "ring" && fm.remaining_percent != null ? (
                        <div className="coding-free-ring-wrap">
                          <svg viewBox="0 0 36 36" className="coding-free-ring-svg" role="img" aria-label={`${Math.round(fm.remaining_percent)}% rimasto`}>
                            <circle className="coding-quota-track" cx="18" cy="18" r="14" />
                            <circle
                              className="coding-quota-value"
                              cx="18"
                              cy="18"
                              r="14"
                              strokeDasharray={`${(Math.max(0, Math.min(100, fm.remaining_percent)) / 100) * 2 * Math.PI * 14} ${2 * Math.PI * 14}`}
                              transform="rotate(-90 18 18)"
                            />
                            <text x="18" y="18" dominantBaseline="central" textAnchor="middle">
                              {`${Math.round(fm.remaining_percent)}%`}
                            </text>
                          </svg>
                          <span className="coding-free-ring-label">rimasto</span>
                        </div>
                      ) : (
                        <div className="coding-free-live-wrap" title="Disponibile (limite dinamico)">
                          <span className="coding-free-live-dot" />
                          <span className="coding-free-live-text">Attivo</span>
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Section 3 — Saldi prepagati (sola lettura) */}
          <div className="coding-quota-section coding-prepaid-section">
            <div className="coding-quota-section-header">
              <h4>Saldi prepagati (sola lettura)</h4>
              <span className="coding-prepaid-caption">Sola lettura — modelli a pagamento disattivati</span>
            </div>
            {prepaidBalances.length === 0 ? (
              <p className="coding-control-empty" role="status">
                Nessun saldo prepagato configurato
              </p>
            ) : (
              <div className="coding-prepaid-list">
                {prepaidBalances.map((pb) => (
                  <div key={pb.provider} className="coding-prepaid-card" data-status={pb.status}>
                    <div className="coding-prepaid-main">
                      <strong className="coding-prepaid-label">{pb.label}</strong>
                      <span className="coding-prepaid-mode">sola lettura</span>
                    </div>
                    <div className="coding-prepaid-amount">
                      {pb.status !== "ok" ? (
                        <span className="coding-prepaid-unavailable">{pb.reason ?? "Non disponibile"}</span>
                      ) : pb.balance_usd != null ? (
                        <span className="coding-prepaid-balance">${pb.balance_usd.toFixed(2)}</span>
                      ) : pb.spent_eur != null || pb.cap_eur != null ? (
                        <span className="coding-prepaid-spend">
                          €{(pb.spent_eur ?? 0).toFixed(2)} / €{(pb.cap_eur ?? 0).toFixed(2)}
                        </span>
                      ) : (
                        <span className="coding-prepaid-ok">Inattivo</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      ) : null}
    </section>
  );
}

export default function ProviderQuotaPanel() {
  const [data, setData] = useState<ProviderQuotas | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(true);
  const [refreshRequest, setRefreshRequest] = useState(0);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async (refresh = false) => {
      setRefreshing(true);
      try {
        const next = await readProviderQuotas(undefined, refresh);
        if (active) {
          setData(next);
          setError(null);
        }
      } catch {
        if (active) {
          setError("server non raggiungibile");
          setData(null);
        }
      }
      if (active) setRefreshing(false);
      if (active) timer = setTimeout(() => void tick(), POLL_MS);
    };

    void tick(refreshRequest > 0);
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [refreshRequest]);

  return (
    <ProviderQuotaView
      data={data}
      error={error}
      refreshing={refreshing}
      onRefresh={() => {
        setRefreshing(true);
        setRefreshRequest((value) => value + 1);
      }}
    />
  );
}
