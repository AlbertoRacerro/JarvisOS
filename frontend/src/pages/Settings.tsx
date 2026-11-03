import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import type { AISettings, AIStatus, SystemInfoResponse } from "../api/client";
import {
  SettingsApiError,
  loadAISettings,
  loadAIStatus,
  loadProviderSettings,
  loadSecretStatus,
  loadSystemInfo,
  removeScalewayCredential,
  replaceScalewayCredential,
  saveAISetting,
  type ProviderSettings,
  type SettingsSecretStatus
} from "../api/settings";
import InlineNotice from "../components/ui/InlineNotice";
import Surface from "../components/ui/Surface";
import { credentialMeaning, providerLocation, savedPaidAiSummary } from "../operatorSemantics";
import {
  ACCENT_OPTIONS,
  ACCENT_PRESETS,
  APPEARANCE_OPTIONS,
  applyAccentPreference,
  applyAppearancePreference,
  readAccentPreference,
  readAppearancePreference,
  subscribeToVisualPreferenceUpdates,
  writeAccentPreference,
  writeAppearancePreference,
  type AccentPreference,
  type AccentPreset,
  type AppearancePreference
} from "../theme";

type NumericKey =
  | "monthly_api_budget_usd"
  | "scaleway_monthly_token_cap"
  | "scaleway_hard_stop_token_cap"
  | "max_direct_continuations";
type BooleanKey = "paid_ai_enabled" | "scaleway_enabled";
type EditableKey = NumericKey | BooleanKey;
type Draft = Record<NumericKey, string> & Record<BooleanKey, boolean>;

type CanonicalSnapshot = {
  settings: AISettings;
  status: AIStatus;
  providers: ProviderSettings;
  secret: SettingsSecretStatus;
  system: SystemInfoResponse;
};

const numericKeys: NumericKey[] = [
  "monthly_api_budget_usd",
  "scaleway_monthly_token_cap",
  "scaleway_hard_stop_token_cap",
  "max_direct_continuations"
];

const accentLabels: Readonly<Record<AccentPreset, string>> = {
  microalgae: "Microalgae",
  "leaf-chlorophyll": "Leaf Chlorophyll",
  lagoon: "Lagoon",
  glacier: "Glacier",
  iris: "Iris",
  ember: "Ember"
};

function draftFrom(settings: AISettings): Draft {
  return {
    monthly_api_budget_usd: String(settings.monthly_api_budget_usd),
    scaleway_monthly_token_cap: String(settings.scaleway_monthly_token_cap),
    scaleway_hard_stop_token_cap: String(settings.scaleway_hard_stop_token_cap),
    max_direct_continuations: String(settings.max_direct_continuations),
    paid_ai_enabled: settings.paid_ai_enabled,
    scaleway_enabled: settings.scaleway_enabled
  };
}

function displayError(caught: unknown, fallback: string): string {
  if (caught instanceof SettingsApiError) {
    return caught.code ? `${caught.message} (${caught.code})` : caught.message;
  }
  return caught instanceof Error ? caught.message : fallback;
}

function credentialSummary(provider: ProviderSettings["providers"][number]): string {
  return credentialMeaning(provider.credential.effective_source, provider.credential.persisted_state);
}

function providerName(providerId: string): string {
  if (providerId === "fake") return "Built-in test provider";
  if (providerId === "ollama") return "Ollama (local)";
  if (providerId === "scaleway") return "Scaleway";
  return providerId.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function readableCode(value: string | null | undefined, fallback: string): string {
  if (!value) return fallback;
  return value.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function capabilitySummary(provider: ProviderSettings["providers"][number]): string {
  const location = providerLocation(provider.requires_network, provider.execution_class);
  return `${location} · ${provider.enabled ? "Enabled in configuration · runtime availability not verified" : "Disabled in configuration"}`;
}

function Settings() {
  const mounted = useRef(true);
  const generation = useRef(0);
  const deleteTriggerRef = useRef<HTMLButtonElement | null>(null);
  const deleteConfirmRef = useRef<HTMLButtonElement | null>(null);
  const credentialInputRef = useRef<HTMLInputElement | null>(null);

  const [settings, setSettings] = useState<AISettings | null>(null);
  const [status, setStatus] = useState<AIStatus | null>(null);
  const [providers, setProviders] = useState<ProviderSettings | null>(null);
  const [secret, setSecret] = useState<SettingsSecretStatus | null>(null);
  const [system, setSystem] = useState<SystemInfoResponse | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loading, setLoading] = useState(true);
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [credentialBusy, setCredentialBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [appearance, setAppearance] = useState<AppearancePreference>(() => readAppearancePreference());
  const [accent, setAccent] = useState<AccentPreference>(() => readAccentPreference());

  const invalidateCanonicalSnapshot = useCallback(() => {
    setSettings(null);
    setStatus(null);
    setProviders(null);
    setSecret(null);
    setSystem(null);
    setDraft(null);
  }, []);

  const loadCanonical = useCallback(async (
    preserveDraft = false,
    savedKey?: EditableKey,
    projectFailure = true
  ): Promise<boolean> => {
    const owner = ++generation.current;
    setLoading(true);
    if (projectFailure) setError(null);

    try {
      const [nextSettings, nextStatus, nextProviders, nextSecret, nextSystem] = await Promise.all([
        loadAISettings(),
        loadAIStatus(),
        loadProviderSettings(),
        loadSecretStatus(),
        loadSystemInfo()
      ]);
      if (!mounted.current || owner !== generation.current) return false;

      const snapshot: CanonicalSnapshot = {
        settings: nextSettings,
        status: nextStatus,
        providers: nextProviders,
        secret: nextSecret,
        system: nextSystem
      };
      setSettings(snapshot.settings);
      setStatus(snapshot.status);
      setProviders(snapshot.providers);
      setSecret(snapshot.secret);
      setSystem(snapshot.system);
      setDraft((current) => {
        const canonicalDraft = draftFrom(snapshot.settings);
        if (!preserveDraft || !current) return canonicalDraft;
        if (!savedKey) return current;
        return { ...current, [savedKey]: canonicalDraft[savedKey] };
      });
      setUncertain(false);
      return true;
    } catch (caught) {
      if (mounted.current && owner === generation.current) {
        invalidateCanonicalSnapshot();
        if (projectFailure) setError(displayError(caught, "Settings could not be loaded."));
      }
      return false;
    } finally {
      if (mounted.current && owner === generation.current) setLoading(false);
    }
  }, [invalidateCanonicalSnapshot]);

  useEffect(() => {
    mounted.current = true;
    void loadCanonical();
    return () => {
      mounted.current = false;
      generation.current += 1;
    };
  }, [loadCanonical]);

  useEffect(() => {
    if (confirmDelete) deleteConfirmRef.current?.focus();
  }, [confirmDelete]);

  useEffect(() => subscribeToVisualPreferenceUpdates(() => {
    const nextAppearance = readAppearancePreference();
    const nextAccent = readAccentPreference();
    setAppearance(nextAppearance);
    setAccent(nextAccent);
  }), []);

  const refreshAfterMutation = async (savedKey?: EditableKey): Promise<boolean> => {
    const refreshed = await loadCanonical(true, savedKey, false);
    if (!refreshed && mounted.current) {
      setUncertain(true);
      setError("State uncertain — canonical reread failed. Reload before another mutation.");
    }
    return refreshed;
  };

  const save = async (key: EditableKey) => {
    if (!draft || settingsBusy || uncertain) return;

    let value: number | boolean;
    if (numericKeys.includes(key as NumericKey)) {
      const raw = draft[key as NumericKey].trim();
      const parsed = Number(raw);
      const valid = raw !== "" && Number.isFinite(parsed) && parsed >= 0
        && (key !== "max_direct_continuations" || (Number.isInteger(parsed) && parsed <= 16));
      if (!valid) {
        setError(key === "max_direct_continuations"
          ? "Direct continuations must be an integer from 0 to 16."
          : "Enter a finite value greater than or equal to zero.");
        return;
      }
      value = parsed;
    } else {
      value = draft[key as BooleanKey];
    }

    setSettingsBusy(true);
    setError(null);
    setMessage(null);
    try {
      await saveAISetting({ [key]: value });
      if (await refreshAfterMutation(key)) setMessage("Saved. Canonical settings reloaded.");
    } catch (caught) {
      const failure = displayError(caught, "Settings write failed.");
      const refreshed = await refreshAfterMutation();
      if (refreshed && mounted.current) setError(`${failure} Canonical state was reloaded; no retry was attempted.`);
    } finally {
      if (mounted.current) setSettingsBusy(false);
    }
  };

  const replaceCredential = async (event: FormEvent) => {
    event.preventDefault();
    const scalewayCapability = providers?.providers.find((provider) => provider.provider_id === "scaleway")?.credential_capabilities;
    if (credentialBusy || uncertain || !apiKey.trim() || !scalewayCapability?.replace_persisted) return;

    const submitted = apiKey;
    setCredentialBusy(true);
    setError(null);
    setMessage(null);
    try {
      await replaceScalewayCredential(submitted);
      setApiKey("");
      if (await refreshAfterMutation()) setMessage("Credential mutation completed. Canonical secure status reloaded.");
    } catch (caught) {
      setApiKey("");
      const failure = displayError(caught, "Credential write failed.");
      const refreshed = await refreshAfterMutation();
      if (refreshed && mounted.current) setError(`${failure} Canonical credential state was reloaded; no retry was attempted.`);
    } finally {
      submitted.replace(/./g, "");
      if (mounted.current) setCredentialBusy(false);
    }
  };

  const cancelDelete = () => {
    setConfirmDelete(false);
    requestAnimationFrame(() => deleteTriggerRef.current?.focus());
  };

  const removeCredential = async () => {
    const scalewayCapability = providers?.providers.find((provider) => provider.provider_id === "scaleway")?.credential_capabilities;
    if (credentialBusy || uncertain || !scalewayCapability?.delete_persisted) return;

    setCredentialBusy(true);
    setError(null);
    setMessage(null);
    try {
      await removeScalewayCredential();
      setConfirmDelete(false);
      if (await refreshAfterMutation()) {
        setMessage("Credential deleted. Canonical secure status reloaded.");
        requestAnimationFrame(() => credentialInputRef.current?.focus());
      }
    } catch (caught) {
      const failure = displayError(caught, "Credential delete failed.");
      const refreshed = await refreshAfterMutation();
      if (refreshed && mounted.current) setError(`${failure} Canonical credential state was reloaded; no retry was attempted.`);
    } finally {
      if (mounted.current) setCredentialBusy(false);
    }
  };

  const manuallyReload = async () => {
    setMessage(null);
    if (await loadCanonical(true)) setMessage("Canonical state reloaded.");
  };

  const setVisualAppearance = (preference: AppearancePreference) => {
    writeAppearancePreference(preference);
    applyAppearancePreference(preference);
    setAppearance(preference);
  };

  const setVisualAccent = (preset: AccentPreset) => {
    const stored = writeAccentPreference({ preset });
    applyAccentPreference(stored);
    setAccent(stored);
  };

  const allMutationsBusy = settingsBusy || credentialBusy || uncertain;
  const scaleway = providers?.providers.find((provider) => provider.provider_id === "scaleway") ?? null;

  return (
    <section className="settings-page" aria-labelledby="settings-title">
      <header className="page-header">
        <p className="eyebrow">Operator controls</p>
        <h1 id="settings-title">Settings</h1>
        <p>Visual preferences stay local. Budget, provider permission and secure credential controls remain canonical server-owned settings.</p>
      </header>

      {loading && <InlineNotice tone="info">Loading canonical settings.</InlineNotice>}
      {uncertain && (
        <InlineNotice tone="danger">
          State uncertain. Reload canonical state before another mutation. <button onClick={() => void manuallyReload()}>Reload</button>
        </InlineNotice>
      )}
      {error && <InlineNotice tone="danger">{error}{!uncertain && !settings && <button disabled={loading} onClick={() => void manuallyReload()}>Reload settings</button>}</InlineNotice>}
      {message && <InlineNotice tone="success">{message}</InlineNotice>}

      <div className="settings-grid">
        <Surface className="settings-card settings-card--visual">
          <div className="settings-visual__heading">
            <div><p className="eyebrow">Local visual preference</p><h2>Appearance & accent</h2></div>
            <span className="settings-accent-preview" aria-hidden="true" />
          </div>
          <fieldset className="settings-visual__group">
            <legend>Appearance</legend>
            <div className="settings-choice-row">
              {APPEARANCE_OPTIONS.map((option) => (
                <label key={option} className="settings-choice">
                  <input type="radio" name="appearance" value={option} checked={appearance === option} onChange={() => setVisualAppearance(option)} />
                  <span>{option[0].toUpperCase() + option.slice(1)}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <fieldset className="settings-visual__group">
            <legend>Accent</legend>
            <div className="settings-accent-grid">
              {ACCENT_OPTIONS.map((option) => (
                <label key={option} className="settings-accent-choice">
                  <input type="radio" name="accent" value={option} checked={accent.preset === option} onChange={() => setVisualAccent(option)} />
                  <span className="settings-accent-swatch" style={{ "--settings-swatch-light": ACCENT_PRESETS[option].light, "--settings-swatch-dark": ACCENT_PRESETS[option].dark } as React.CSSProperties} aria-hidden="true" />
                  <span>{accentLabels[option]}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <p className="settings-muted">Each accent is tuned separately for Light and Dark. Accent affects navigation, focus, selection and primary actions only. Engineering status and scientific colors remain independent.</p>
        </Surface>

        <Surface className="settings-card">
          <h2>AI permission & budget</h2>
          <p className="settings-card__summary">External calls: <strong>{providers ? (providers.external_calls_allowed ? "Allowed" : "Blocked") : (loading ? "Checking" : "Unavailable")}</strong>{providers?.blocking_reason ? ` — ${readableCode(providers.blocking_reason, "Blocked by policy")}` : ""}</p>
          <p className="settings-muted">{settings ? savedPaidAiSummary(settings.paid_ai_enabled, settings.monthly_api_budget_usd) : "Checking budget policy."}</p>
          {settings && draft && (draft.paid_ai_enabled !== settings.paid_ai_enabled || draft.monthly_api_budget_usd !== String(settings.monthly_api_budget_usd)) ? <p className="settings-muted">Pending unsaved draft; current permission is unchanged until Save.</p> : null}
          {providers ? <details><summary>Technical details</summary><p>Policy mode · {providers.policy_mode}</p><p>Blocking reason code · {providers.blocking_reason ?? "none"}</p></details> : null}
          {draft && (
            <div className="settings-fields">
              <label><span>Monthly API budget <small>USD</small></span><span className="settings-field"><input inputMode="decimal" value={draft.monthly_api_budget_usd} disabled={allMutationsBusy} onChange={(event) => setDraft({ ...draft, monthly_api_budget_usd: event.target.value })} /><button disabled={allMutationsBusy} onClick={() => void save("monthly_api_budget_usd")}>Save</button></span></label>
              <label className="settings-toggle"><span>Paid AI enabled</span><input type="checkbox" checked={draft.paid_ai_enabled} disabled={allMutationsBusy} onChange={(event) => setDraft({ ...draft, paid_ai_enabled: event.target.checked })} /><button disabled={allMutationsBusy} onClick={() => void save("paid_ai_enabled")}>Save</button></label>
              <label className="settings-toggle"><span>Scaleway enabled</span><input type="checkbox" checked={draft.scaleway_enabled} disabled={allMutationsBusy} onChange={(event) => setDraft({ ...draft, scaleway_enabled: event.target.checked })} /><button disabled={allMutationsBusy} onClick={() => void save("scaleway_enabled")}>Save</button></label>
              <label><span>Monthly token cap</span><span className="settings-field"><input inputMode="numeric" value={draft.scaleway_monthly_token_cap} disabled={allMutationsBusy} onChange={(event) => setDraft({ ...draft, scaleway_monthly_token_cap: event.target.value })} /><button disabled={allMutationsBusy} onClick={() => void save("scaleway_monthly_token_cap")}>Save</button></span></label>
              <label><span>Hard-stop token cap</span><span className="settings-field"><input inputMode="numeric" value={draft.scaleway_hard_stop_token_cap} disabled={allMutationsBusy} onChange={(event) => setDraft({ ...draft, scaleway_hard_stop_token_cap: event.target.value })} /><button disabled={allMutationsBusy} onClick={() => void save("scaleway_hard_stop_token_cap")}>Save</button></span></label>
              <label><span>Direct continuations <small>0–16</small></span><span className="settings-field"><input inputMode="numeric" value={draft.max_direct_continuations} disabled={allMutationsBusy} onChange={(event) => setDraft({ ...draft, max_direct_continuations: event.target.value })} /><button disabled={allMutationsBusy} onClick={() => void save("max_direct_continuations")}>Save</button></span></label>
            </div>
          )}
        </Surface>

        <Surface className="settings-card">
          <h2>Provider catalogue</h2>
          <p className="settings-card__summary">Configured providers and policy permission. Enabled configuration does not prove that a runtime or model is reachable. Credentials are shown only as status.</p>
          <div className="settings-fields" data-provider-settings-list>
            {providers?.providers.map((provider) => (
              <div key={provider.provider_id} data-provider-id={provider.provider_id}>
                <strong>{providerName(provider.provider_id)}</strong>
                <p className="settings-muted">{capabilitySummary(provider)}</p>
                <p data-provider-egress-state={provider.external_calls_allowed ? "allowed" : "blocked"}>
                  Provider access: {provider.external_calls_allowed ? "Allowed" : "Blocked"}
                  {provider.blocking_reason ? ` — ${readableCode(provider.blocking_reason, "Blocked by policy")}` : ""}
                </p>
                <p>{credentialSummary(provider)}</p>
                <details><summary>Technical details</summary><p>Provider code · {provider.provider_id}</p><p>Kind · {provider.kind}</p><p>Execution class · {provider.execution_class}</p><p>Blocking reason code · {provider.blocking_reason ?? "none"}</p><p>Credential reason code · {provider.credential.reason_code ?? "none"}</p><p>Credential source code · {provider.credential.effective_source}</p><p>Persisted state code · {provider.credential.persisted_state}</p></details>
              </div>
            )) ?? <p>Checking provider registry.</p>}
          </div>
        </Surface>

        <Surface className="settings-card" data-provider-credential-owner="scaleway">
          <h2>Scaleway credential</h2>
          <p className="settings-card__summary">{scaleway ? credentialSummary(scaleway) : "Checking credential state."}</p>
          <details><summary>Technical details</summary><p>Effective source code · {scaleway?.credential.effective_source ?? secret?.effective_source ?? "checking"}</p><p>Persisted state code · {scaleway?.credential.persisted_state ?? secret?.persisted_state ?? "checking"}</p><p>Storage mode · {secret?.storage_mode ?? "checking"}</p><p>Reason code · {scaleway?.credential.reason_code ?? secret?.reason_code ?? "none"}</p></details>
          <form className="settings-secret" onSubmit={replaceCredential}>
            <label>Replace API key<input ref={credentialInputRef} type="password" autoComplete="new-password" value={apiKey} disabled={allMutationsBusy || !scaleway?.credential_capabilities.replace_persisted} onChange={(event) => setApiKey(event.target.value)} /></label>
            <button disabled={allMutationsBusy || !apiKey.trim() || !scaleway?.credential_capabilities.replace_persisted}>Store securely</button>
          </form>
          {scaleway?.credential.effective_source === "environment" && <p className="settings-muted">Environment credentials override persisted credentials and cannot be replaced here. Persisted damage remains visible independently.</p>}
          {!confirmDelete ? (
            <button ref={deleteTriggerRef} className="button-secondary" disabled={allMutationsBusy || !scaleway?.credential_capabilities.delete_persisted} onClick={() => setConfirmDelete(true)}>Delete persisted credential</button>
          ) : (
            <div className="settings-confirm" role="group" aria-label="Confirm credential deletion" onKeyDown={(event) => { if (event.key === "Escape") cancelDelete(); }}>
              <span>Delete the persisted credential?</span>
              <button ref={deleteConfirmRef} disabled={allMutationsBusy || !scaleway?.credential_capabilities.delete_persisted} onClick={() => void removeCredential()}>Delete</button>
              <button className="button-secondary" disabled={credentialBusy} onClick={cancelDelete}>Cancel</button>
            </div>
          )}
        </Surface>

        <Surface className="settings-card">
          <h2>Current usage</h2>
          <dl className="settings-facts">
            <div><dt>Spend this month</dt><dd>{providers?.spend_month_to_date_usd != null || status?.spend_month_to_date_usd != null ? `$${providers?.spend_month_to_date_usd ?? status?.spend_month_to_date_usd}` : loading ? "Checking" : "Unavailable"}</dd></div>
            <div><dt>Scaleway token usage</dt><dd>{status?.usage_total_tokens ?? (loading ? "Checking" : "Unavailable")}</dd></div>
            <div><dt>Budget status</dt><dd>{readableCode(status?.budget_status, "Checking")}</dd></div>
            <div><dt>Default provider</dt><dd>{providerName(providers?.default_provider_id ?? status?.provider_id ?? "checking")}</dd></div>
          </dl>
          <details><summary>Technical details</summary><p>Budget status code · {status?.budget_status ?? "checking"}</p><p>Default provider code · {providers?.default_provider_id ?? status?.provider_id ?? "checking"}</p></details>
        </Surface>

        <Surface className="settings-card">
          <h2>System</h2>
          <dl className="settings-facts">
            <div><dt>Environment</dt><dd>{system?.environment ?? "checking"}</dd></div>
            <div><dt>Data root</dt><dd>{system ? (system.data_root_exists ? "Available" : "Missing") : "checking"}</dd></div>
            <div><dt>Database</dt><dd>{system ? (system.database.ready ? "Ready" : "Not ready") : "checking"}</dd></div>
            <div><dt>Provider configured</dt><dd>{system ? (system.ai.provider_configured ? "Yes" : "No") : "checking"}</dd></div>
          </dl>
          <details>
            <summary>Advanced diagnostics</summary>
            <p>Policy: {providers?.policy_mode ?? settings?.policy_mode ?? "checking"}</p>
            <p>Provider mode: {status?.provider_mode ?? "checking"}</p>
            <p>Credential reason: {scaleway?.credential.reason_code ?? secret?.reason_code ?? "none"}</p>
          </details>
        </Surface>
      </div>

    </section>
  );
}

export default Settings;
