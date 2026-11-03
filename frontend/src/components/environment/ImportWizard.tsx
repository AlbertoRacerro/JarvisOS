import { useState } from "react";
import { environmentApi, environmentUrl } from "./api";
import { CHANNEL_LABELS, type Mapping, type Preview } from "./types";

const CHANNELS = Object.keys(CHANNEL_LABELS);
const unitFor = (channel: string) => {
  if (["ghi", "dni", "dhi"].includes(channel)) return "W m-2";
  if (["air_temperature", "sea_temperature"].includes(channel)) return "°C";
  if (channel === "par") return "umol m-2 s-1";
  if (channel === "wind_speed") return "m s-1";
  if (channel === "cloud_cover") return "1";
  if (channel === "wave_height") return "m";
  if (channel === "wave_period") return "s";
  return "K";
};

type Props = {
  workspaceId: string;
  timezone: string;
  disabled: boolean;
  onCreated: (profile: { profile_id: string }) => void;
  onError: (message: string) => void;
};

export function ImportWizard({ workspaceId, timezone, disabled, onCreated, onError }: Props) {
  const [upload, setUpload] = useState<{ upload_id: string; filename: string } | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [timestampColumn, setTimestampColumn] = useState("");
  const [mappings, setMappings] = useState<Mapping[]>([]);
  const [resolution, setResolution] = useState(60);
  const [stampConvention, setStampConvention] = useState<"start" | "end">("end");
  const [fold, setFold] = useState<"" | "0" | "1">("");
  const [profileName, setProfileName] = useState("");
  const columns = preview?.preview?.columns ?? preview?.columns ?? [];

  const chooseFile = async (file: File) => {
    onError("");
    setUpload(null);
    setPreview(null);
    try {
      const response = await fetch(
        `${environmentUrl(workspaceId, "/uploads")}?filename=${encodeURIComponent(file.name)}`,
        { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file },
      );
      const contentType = response.headers.get("content-type") ?? "";
      const body = contentType.includes("application/json") ? await response.json() : {};
      if (!response.ok) throw new Error(body.detail?.error ?? "Upload failed.");
      const staged = { upload_id: body.upload_id as string, filename: body.filename as string };
      setUpload(staged);
      setProfileName(staged.filename);
      const imported = await environmentApi<Preview>(
        workspaceId,
        `/uploads/${staged.upload_id}/preview?filename=${encodeURIComponent(staged.filename)}`,
        { method: "POST" },
      );
      setPreview(imported);
      if (imported.preview?.columns.length) {
        setTimestampColumn(imported.preview.columns[0]);
        setMappings(imported.preview.columns.length > 1 ? [{
          channel: "ghi", column: imported.preview.columns[1], unit: "W m-2",
        }] : []);
      }
    } catch (error) {
      onError(error instanceof Error ? error.message : "Import preview failed.");
    }
  };

  const addMapping = () => setMappings((current) => [...current, {
    channel: CHANNELS.find((channel) => !current.some((entry) => entry.channel === channel)) ?? "ghi",
    column: columns.find((column) => column !== timestampColumn) ?? "",
    unit: "W m-2",
  }]);

  const updateMapping = (index: number, update: Partial<Mapping>) => {
    setMappings((current) => current.map((mapping, position) => position === index
      ? { ...mapping, ...update, ...(update.channel ? { unit: unitFor(update.channel) } : {}) }
      : mapping));
  };

  const confirm = async () => {
    if (!upload || !preview) return;
    try {
      const payload = preview.format === "csv" ? {
        format: "csv",
        mapping: {
          name: profileName,
          timestamp_column: timestampColumn,
          timezone,
          resolution_minutes: resolution,
          stamp_convention: stampConvention,
          ...(fold === "" ? {} : { fold: Number(fold) }),
          channels: Object.fromEntries(mappings.map(({ channel, column, unit }) => [channel, { column, unit }])),
        },
      } : { format: preview.format, name: profileName, timezone };
      const result = await environmentApi<{ profile_id: string }>(
        workspaceId,
        `/uploads/${upload.upload_id}/confirm?filename=${encodeURIComponent(upload.filename)}`,
        { method: "POST", body: JSON.stringify(payload) },
      );
      onCreated(result);
      setPreview(null);
      setUpload(null);
    } catch (error) {
      onError(error instanceof Error ? error.message : "Import confirmation failed.");
    }
  };

  return (
    <section className="environment-card">
      <h3>Import local file</h3>
      <label>CSV or EPW file<input type="file" accept=".csv,.epw" disabled={disabled} onChange={(e) => {
        const file = e.target.files?.[0];
        if (file) void chooseFile(file);
      }} /></label>
      {preview && upload && <div className="environment-import">
        <p>{preview.format} · {preview.preview?.row_count ?? preview.row_count} rows · preview ready</p>
        {preview.format === "csv" && preview.preview && <>
          <div className="environment-preview-table"><table aria-label="CSV preview">
            <thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead>
            <tbody>{preview.preview.rows.map((row, rowIndex) => <tr key={rowIndex}>
              {row.map((cell, columnIndex) => <td key={columnIndex}>{cell}</td>)}
            </tr>)}</tbody>
          </table></div>
          <label>Timestamp column<select value={timestampColumn} onChange={(e) => setTimestampColumn(e.target.value)}>
            {columns.map((column) => <option key={column}>{column}</option>)}
          </select></label>
          <label>Resolution<select value={resolution} onChange={(e) => setResolution(Number(e.target.value))}>
            {[5, 10, 15, 30, 60].map((step) => <option key={step} value={step}>{step} min</option>)}
          </select></label>
          <label>Timestamp convention<select value={stampConvention}
            onChange={(e) => setStampConvention(e.target.value as "start" | "end")}>
            <option value="end">Interval end</option><option value="start">Interval start</option>
          </select></label>
          <label>Repeated local time<select value={fold} onChange={(e) => setFold(e.target.value as "" | "0" | "1")}>
            <option value="">Reject ambiguity</option>
            <option value="0">First occurrence</option>
            <option value="1">Second occurrence</option>
          </select></label>
          {mappings.map((mapping, index) => <div className="environment-map-row" key={index}>
            <label>Value column<select value={mapping.column} onChange={(e) => updateMapping(index, { column: e.target.value })}>
              {columns.filter((column) => column !== timestampColumn).map((column) => <option key={column}>{column}</option>)}
            </select></label>
            <label>Channel<select value={mapping.channel} onChange={(e) => updateMapping(index, { channel: e.target.value })}>
              {CHANNELS.map((channel) => <option key={channel} value={channel}>{CHANNEL_LABELS[channel]}</option>)}
            </select></label>
            <label>Unit<input value={mapping.unit} onChange={(e) => updateMapping(index, { unit: e.target.value })} /></label>
            <button type="button" aria-label="Remove channel mapping"
              onClick={() => setMappings((current) => current.filter((_, i) => i !== index))}>Remove</button>
          </div>)}
          <button type="button" onClick={addMapping}>Add channel mapping</button>
          <label>Profile name<input value={profileName} onChange={(e) => setProfileName(e.target.value)} /></label>
        </>}
        {preview.format !== "csv" && <label>Profile name<input value={profileName}
          onChange={(e) => setProfileName(e.target.value)} /></label>}
        <button type="button" disabled={disabled || (preview.format === "csv" && mappings.length === 0)} onClick={() => void confirm()}>
          Create imported profile
        </button>
      </div>}
    </section>
  );
}
