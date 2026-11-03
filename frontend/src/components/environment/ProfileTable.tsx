import { useEffect, useState } from "react";
import type { ProfileValues } from "./types";
import { CHANNEL_LABELS, displayUnit, displayValue, storageValue } from "./types";

import NumericInput from "../ui/NumericInput";
export type CellChange = { channel: string; index: number; value: number | null };

type Props = {
  profile: ProfileValues;
  page: number;
  timezone: string;
  onPage: (page: number) => void;
  onSave: (changes: CellChange[]) => void;
  disabled: boolean;
};

function formatTime(value: string, timezone: string) {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone,
    dateStyle: "short",
    timeStyle: "short",
    hourCycle: "h23",
  }).format(new Date(value));
}

export function ProfileTable({ profile, page, timezone, onPage, onSave, disabled }: Props) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [editError, setEditError] = useState("");
  useEffect(() => { setDrafts({}); setEditError(""); }, [profile.digest, page]);
  const columns = Object.keys(profile.channels);
  const changeValue = (channel: string, position: number, current: number | null) => {
    const key = `${channel}:${position}`;
    const raw = drafts[key] ?? String(displayValue(channel, current) ?? "");
    if (raw.trim() === "") return { channel, index: profile.indices[position], value: null };
    const shown = Number(raw);
    return { channel, index: profile.indices[position], value: storageValue(channel, shown) };
  };
  const save = () => {
    if (Object.values(drafts).some((raw) => raw.trim() !== "" && !Number.isFinite(Number(raw)))) {
      setEditError("Cell edits must be finite numbers or blank for null.");
      return;
    }
    setEditError("");
    const changes = Object.keys(drafts).map((key) => {
      const [channel, positionText] = key.split(":");
      const position = Number(positionText);
      return changeValue(channel, position, profile.channels[channel][position]);
    }).filter((change) => {
      const position = profile.indices.indexOf(change.index);
      return position >= 0 && change.value !== profile.channels[change.channel][position];
    });
    if (changes.length) onSave(changes);
  };
  const moveCell = (event: React.KeyboardEvent<HTMLInputElement>, row: number, column: number) => {
    if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) return;
    const delta = event.key === "ArrowUp" ? [-1, 0] : event.key === "ArrowDown" ? [1, 0]
      : event.key === "ArrowLeft" ? [0, -1] : [0, 1];
    const target = event.currentTarget.closest("table")?.querySelector<HTMLInputElement>(
      `[data-cell-row="${row + delta[0]}"][data-cell-column="${column + delta[1]}"]`,
    );
    if (target) { event.preventDefault(); target.focus(); }
  };

  return (
    <>
      {editError && <p className="environment-error" role="alert">{editError}</p>}
      <div className="environment-table-wrap">
        <table aria-label="Environment profile values">
          <thead><tr><th scope="col">Site time · {timezone}</th>{columns.map((channel) => <th key={channel} scope="col">
            {CHANNEL_LABELS[channel] ?? channel} ({displayUnit(channel, profile.units[channel])})
          </th>)}</tr></thead>
          <tbody>{profile.timestamps.map((timestamp, row) => <tr key={timestamp}>
            <th scope="row">{formatTime(timestamp, timezone)}</th>
            {columns.map((channel, column) => {
              const key = `${channel}:${row}`;
              const value = profile.channels[channel][row];
              return <td key={channel}><NumericInput
                value={drafts[key] ?? (displayValue(channel, value) ?? "")}
                data-cell-row={row}
                data-cell-column={column}
                aria-label={`${CHANNEL_LABELS[channel] ?? channel} at ${formatTime(timestamp, timezone)} ${timezone}`}
                onValueChange={(text) => setDrafts((current) => ({ ...current, [key]: text }))}
                onKeyDown={(event) => moveCell(event, row, column)}
              /></td>;
            })}
          </tr>)}</tbody>
        </table>
      </div>
      <div className="environment-pages">
        <span>{profile.total} points · page {page + 1}</span>
        <button type="button" disabled={disabled || page === 0} onClick={() => onPage(page - 1)}>Previous</button>
        <button type="button" disabled={disabled || (page + 1) * 50 >= profile.total} onClick={() => onPage(page + 1)}>Next</button>
        <button type="button" disabled={disabled || Object.keys(drafts).length === 0} onClick={save}>Save cell edits</button>
      </div>
    </>
  );
}
