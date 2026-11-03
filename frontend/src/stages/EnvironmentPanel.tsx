import { useCallback, useEffect, useRef, useState } from "react";
import { environmentApi, errorMessage } from "../components/environment/api";
import { GeneratorForm, type GenerateRequest } from "../components/environment/GeneratorForm";
import { ImportWizard } from "../components/environment/ImportWizard";
import { ProfileChart } from "../components/environment/ProfileChart";
import { ProfileList } from "../components/environment/ProfileList";
import { ProfileTable, type CellChange } from "../components/environment/ProfileTable";
import { Provenance } from "../components/environment/Provenance";
import { SiteForm, sitePayload, toSiteDraft, type SiteDraft } from "../components/environment/SiteForm";
import type { Profile, ProfileValues, Site } from "../components/environment/types";
import "uplot/dist/uPlot.min.css";
import "./EnvironmentPanel.css";

import NumericInput from "../components/ui/NumericInput";
type Props = { workspaceId: string; onClose: () => void };

const TABLE_PAGE_SIZE = 50;
const ALLOWED_STEPS = [5, 10, 15, 30, 60, 1440];

function validTimezone(value: string): boolean {
  try {
    new Intl.DateTimeFormat("en", { timeZone: value });
    return true;
  } catch {
    return false;
  }
}

export default function EnvironmentPanel({ workspaceId, onClose }: Props) {
  const dialog = useRef<HTMLDivElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);
  const [site, setSite] = useState<Site | null>(null);
  const [siteDraft, setSiteDraft] = useState<SiteDraft>(toSiteDraft(null));
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selected, setSelected] = useState<Profile | null>(null);
  const [chartValues, setChartValues] = useState<ProfileValues | null>(null);
  const [tableValues, setTableValues] = useState<ProfileValues | null>(null);
  const [tablePage, setTablePage] = useState(0);
  const [tableRange, setTableRange] = useState<[string, string] | null>(null);
  const [parFactor, setParFactor] = useState("2.06");
  const [replacePar, setReplacePar] = useState(false);
  const [bulkChannel, setBulkChannel] = useState("");
  const [bulkOperation, setBulkOperation] = useState<"scale" | "offset">("scale");
  const [bulkValue, setBulkValue] = useState("1");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [pageCount, setPageCount] = useState(0);

  const refresh = useCallback(async (preferredDigest?: string | null) => {
    const [savedSite, listed] = await Promise.all([
      environmentApi<Site | null>(workspaceId, "/site"),
      environmentApi<Profile[]>(workspaceId, "/profiles"),
    ]);
    setSite(savedSite);
    setSiteDraft(toSiteDraft(savedSite));
    setProfiles(listed);
    const target = preferredDigest ?? listed[0]?.digest ?? null;
    if (target && listed.some((profile) => profile.digest === target)) {
      setSelected(listed.find((profile) => profile.digest === target) ?? null);
    } else if (listed.length) {
      setSelected(listed[0]);
    } else {
      setSelected(null);
    }
  }, [workspaceId]);

  const choose = useCallback((digest: string) => {
    const profile = profiles.find((item) => item.digest === digest);
    if (profile) {
      setSelected(profile);
      setTablePage(0);
      setTableRange(null);
      setMessage("");
      setError("");
    }
  }, [profiles]);

  const chooseCreated = useCallback(async (profile: { profile_id: string }) => {
    await refresh(profile.profile_id);
  }, [refresh]);

  const run = useCallback(async <T,>(operation: () => Promise<T>, success: string) => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await operation();
      const digest = typeof result === "object" && result !== null && "profile_id" in result
        ? String((result as { profile_id: string }).profile_id)
        : selected?.digest ?? null;
      setMessage(success);
      await refresh(digest);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }, [refresh, selected?.digest]);

  useEffect(() => {
    void refresh().catch((reason) => setError(errorMessage(reason)));
  }, [refresh]);

  useEffect(() => {
    previouslyFocused.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const firstButton = dialog.current?.querySelector<HTMLElement>("button, input, select, textarea");
    firstButton?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
      if (event.key !== "Tab" || !dialog.current) return;
      const focusable = [...dialog.current.querySelectorAll<HTMLElement>(
        "button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex='-1'])",
      )].filter((node) => node.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previouslyFocused.current?.focus();
    };
  }, [onClose]);

  useEffect(() => {
    if (!selected) {
      setChartValues(null);
      setTableValues(null);
      return;
    }
    let cancelled = false;
    const loadChart = async () => {
      const base = selected.resolution_minutes;
      const spanHours = (new Date(selected.end).getTime() - new Date(selected.start).getTime()) / 3_600_000;
      const estimatedPoints = base ? spanHours * 60 / base : 0;
      const chartResolution = base && estimatedPoints > 2500
        ? ALLOWED_STEPS.find((step) => step >= base && step % base === 0 && step >= 1440) ?? base
        : base;
      const pieces: ProfileValues[] = [];
      let offset = 0;
      let total = 0;
      do {
        const query = new URLSearchParams({ offset: String(offset), limit: "5000" });
        if (chartResolution) query.set("resolution_minutes", String(chartResolution));
        const result = await environmentApi<ProfileValues>(
          workspaceId,
          `/profiles/${encodeURIComponent(selected.digest)}?${query.toString()}`,
        );
        pieces.push(result);
        total = result.total;
        offset += result.timestamps.length;
        if (result.timestamps.length === 0) break;
      } while (offset < total);
      if (cancelled) return;
      const first = pieces[0];
      setChartValues({
        ...first,
        timestamps: pieces.flatMap((part) => part.timestamps),
        indices: pieces.flatMap((part) => part.indices),
        channels: Object.fromEntries(Object.keys(first.channels).map((channel) => [
          channel, pieces.flatMap((part) => part.channels[channel]),
        ])),
        total,
        resolution_minutes: chartResolution,
      });
    };
    void loadChart().catch((reason) => setError(errorMessage(reason)));
    return () => { cancelled = true; };
  }, [workspaceId, selected?.digest, selected?.start, selected?.end, selected?.resolution_minutes]);

  useEffect(() => {
    if (!selected) return;
    const query = new URLSearchParams({ offset: String(tablePage * TABLE_PAGE_SIZE), limit: String(TABLE_PAGE_SIZE) });
    if (tableRange) {
      query.set("start", tableRange[0]);
      query.set("end", tableRange[1]);
    }
    void environmentApi<ProfileValues>(
      workspaceId,
      `/profiles/${encodeURIComponent(selected.digest)}?${query.toString()}`,
    ).then((values) => {
      setTableValues(values);
      setPageCount(values.total);
    }).catch((reason) => setError(errorMessage(reason)));
  }, [workspaceId, selected?.digest, tablePage, tableRange]);

  const saveSite = () => {
    const payload = sitePayload(siteDraft);
    if (!payload.name.trim() || !siteDraft.latitude.trim() || !siteDraft.longitude.trim()
      || !siteDraft.elevation_m.trim() || !Number.isFinite(payload.latitude)
      || !Number.isFinite(payload.longitude) || !Number.isFinite(payload.elevation_m)
      || !validTimezone(payload.timezone)) {
      setError("Enter a site name, finite coordinates and elevation, and a valid IANA timezone.");
      return;
    }
    void run(() => environmentApi<Site>(workspaceId, `/site?expected_revision=${siteDraft.revision}`, {
      method: "PUT", body: JSON.stringify(payload),
    }), "Site saved.");
  };

  const generate = (request: GenerateRequest) => {
    void run(() => environmentApi<Profile>(workspaceId, "/generate", {
      method: "POST", body: JSON.stringify(request),
    }), "Profile generated.");
  };

  const saveCells = async (changes: CellChange[]) => {
    if (!selected) return;
    setBusy(true);
    setError("");
    try {
      const result = await environmentApi<Profile>(
        workspaceId,
        `/profiles/${encodeURIComponent(selected.digest)}/edit`,
        {
          method: "POST",
          body: JSON.stringify({ operation: { type: "cells", changes } }),
        },
      );
      setMessage(`${changes.length} cell edit${changes.length === 1 ? "" : "s"} saved as a child profile.`);
      await refresh(result.digest);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const timezone = site?.timezone && validTimezone(site.timezone) ? site.timezone : "UTC";
  const hasPar = Boolean(selected?.channels.par);
  const updateTableRange = useCallback((start: string, end: string) => {
    setTableRange([start, end]);
    setTablePage(0);
  }, []);

  return (
    <div className="environment-overlay">
      <div ref={dialog} className="environment-panel" role="dialog" aria-modal="true" aria-labelledby="environment-title" tabIndex={-1}>
        <header className="environment-toolbar">
          <div><p className="eyebrow">Workspace inputs</p><h2 id="environment-title">Environment profiles</h2></div>
          <button type="button" onClick={onClose} aria-label="Close Environment">Close</button>
        </header>
        {error && <p className="environment-error" role="alert">{error}</p>}
        {message && <p className="environment-message" role="status">{message}</p>}
        <div className="environment-grid">
          <SiteForm value={siteDraft} onChange={setSiteDraft} onSave={saveSite} disabled={busy} />
          <GeneratorForm onGenerate={generate} disabled={busy || !site} />
          <ImportWizard workspaceId={workspaceId} timezone={timezone} disabled={busy}
            onCreated={(profile) => void chooseCreated(profile)} onError={setError} />
          <ProfileList profiles={profiles} selectedDigest={selected?.digest ?? null} onChoose={choose} />
        </div>
        {selected && <section className="environment-card environment-editor">
          <header className="environment-editor-header">
            <div>
              <h3>{selected.name}</h3>
              <p>{selected.label ?? Object.keys(selected.channels).join(" · ")} · {selected.resolution_minutes ?? "Irregular"} min</p>
            </div>
            <div className="environment-derive">
              <label>Conversion factor (µmol/J)<NumericInput min={0.000001} value={parFactor}
                onValueChange={(text) => setParFactor(text)} /></label>
              <small>0.45 PAR fraction × 4.57 µmol/J (screening)</small>
              {hasPar && <label className="environment-inline-check"><input type="checkbox" checked={replacePar}
                onChange={(event) => setReplacePar(event.target.checked)} />Replace existing PAR channel</label>}
              <button type="button" disabled={busy || !selected.channels.ghi || (hasPar && !replacePar)} onClick={() => void run(
                () => environmentApi<Profile>(workspaceId, `/profiles/${encodeURIComponent(selected.digest)}/derive-par`, {
                  method: "POST", body: JSON.stringify({ factor: Number(parFactor), replace: replacePar }),
                }), "Derived PAR profile created.",
              )}>Derive PAR</button>
            </div>
          </header>
          <div className="environment-bulk-edit">
            <label>Channel<select value={bulkChannel || Object.keys(selected.channels)[0]}
              onChange={(event) => setBulkChannel(event.target.value)}>
              {Object.keys(selected.channels).map((channel) => <option key={channel}>{channel}</option>)}
            </select></label>
            <label>Operation<select value={bulkOperation} onChange={(event) => setBulkOperation(event.target.value as "scale" | "offset")}>
              <option value="scale">Scale</option><option value="offset">Offset</option>
            </select></label>
            <label>{bulkOperation === "scale" ? "Factor" : "Offset"}<NumericInput value={bulkValue}
              onValueChange={(text) => setBulkValue(text)} /></label>
            <button type="button" disabled={busy} onClick={() => void run(() => environmentApi<Profile>(
              workspaceId,
              `/profiles/${encodeURIComponent(selected.digest)}/edit`,
              { method: "POST", body: JSON.stringify({ operation: {
                type: bulkOperation, channel: bulkChannel || Object.keys(selected.channels)[0], value: Number(bulkValue),
              } }) },
            ), "Range edit saved as a new profile.")}>Apply to channel</button>
          </div>
          {chartValues && <ProfileChart profile={chartValues} timezone={timezone}
            resolutionUsed={chartValues.resolution_minutes} onRange={updateTableRange} />}
          {tableValues && <ProfileTable profile={tableValues} page={tablePage} timezone={timezone} disabled={busy}
            onPage={(page) => { setTableRange(null); setTablePage(page); }} onSave={(changes) => void saveCells(changes)} />}
          <p className="environment-help">{pageCount} points in the selected table range.</p>
          <Provenance profile={selected} onChooseParent={choose} />
        </section>}
      </div>
    </div>
  );
}
