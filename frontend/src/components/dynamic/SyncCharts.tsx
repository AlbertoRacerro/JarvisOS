import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import type uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { channelLabel, fmtNum, groupChannelsByUnit, panRange, tableWindow, zoomRange, type SeriesData } from "./dynamicLogic";
import { chartSeriesColor, useChartAppearance } from "../chartPalette";

import NumericInput from "../ui/NumericInput";
import { numericOrUndefined } from "../ui/numericStep";
// Spec 173 decision 9: uPlot panels share one cursor and one x-range, group channels by their manifest unit, and each carries a values
// table and keyboard range control. uPlot is loaded lazily, as in the environment profile chart.
export const SYNC_KEY = "jarvis-dynamic";
export type Line = { key: string; label: string; values: (number | null)[]; dash?: boolean };
type Range = [number, number];

async function loadUPlot() {
  const original = Intl.NumberFormat;
  if (!navigator.language.includes("@")) return import("uplot");
  // uPlot reads navigator.language at module load; normalise the host's nonstandard tag and restore the global.
  const Safe = class extends original {
    constructor(locales?: Intl.LocalesArgument, options?: Intl.NumberFormatOptions) { super(typeof locales === "string" ? locales.split("@")[0] : locales, options); }
  };
  Intl.NumberFormat = Safe as unknown as typeof Intl.NumberFormat;
  try { return await import("uplot"); } finally { Intl.NumberFormat = original; }
}

export function RangeControls({ range, bounds, onRange }: { range: Range | null; bounds: Range; onRange(next: Range | null): void }) {
  const [start, end] = range ?? bounds;
  const days = (seconds: number) => String(Number((seconds / 86400).toPrecision(8)));
  const commit = (nextStart: number, nextEnd: number) => { if (Number.isFinite(nextStart) && Number.isFinite(nextEnd) && nextEnd > nextStart) onRange([Math.max(bounds[0], nextStart * 86400), Math.min(bounds[1], nextEnd * 86400)]); };
  return <div className="dyn-range" role="group" aria-label="Chart range">
    <label>Range start [d]<NumericInput aria-label="Range start [d]" value={days(start)} onValueChange={(text) => commit((numericOrUndefined(text) ?? 0), end / 86400)} /></label>
    <label>Range end [d]<NumericInput aria-label="Range end [d]" value={days(end)} onValueChange={(text) => commit(start / 86400, (numericOrUndefined(text) ?? 0))} /></label>
    <button type="button" onClick={() => onRange(panRange([start, end], bounds, -0.25))}>Pan earlier</button>
    <button type="button" onClick={() => onRange(panRange([start, end], bounds, 0.25))}>Pan later</button>
    <button type="button" onClick={() => onRange(zoomRange([start, end], bounds, 0.5))}>Zoom in</button>
    <button type="button" onClick={() => onRange(zoomRange([start, end], bounds, 2))}>Zoom out</button>
    <button type="button" disabled={!range} onClick={() => onRange(null)}>Reset range</button>
  </div>;
}

type PanelProps = { title: string; unit: string; x: number[]; lines: Line[]; range: Range | null; bounds: Range; onRange(next: Range | null): void; height?: number };

export function ChartPanel({ title, unit, x, lines, range, bounds, onRange, height: baseHeight = 220 }: PanelProps) {
  // Spec 230: an expanded chart takes the full row and most of the viewport height; data and scales are unchanged.
  const [expanded, setExpanded] = useState(false);
  const height = expanded ? Math.max(baseHeight, Math.round(window.innerHeight * 0.6)) : baseHeight;
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<InstanceType<typeof uPlot> | null>(null);
  const rangeRef = useRef(range); rangeRef.current = range;
  const onRangeRef = useRef(onRange); onRangeRef.current = onRange;
  const boundsRef = useRef(bounds); boundsRef.current = bounds;
  const quiet = useRef(false);
  const [problem, setProblem] = useState("");
  const appearance = useChartAppearance();
  const keyed = useMemo(() => lines.map((line) => line.key).join("|"), [lines]);

  useEffect(() => {
    let active = true; let observer: ResizeObserver | undefined; setProblem("");
    void loadUPlot().then(({ default: UPlot }) => {
      if (!active || !host.current || !lines.length || !x.length) return;
      plot.current?.destroy();
      const data = [x, ...lines.map((line) => line.values)] as uPlot.AlignedData;
      plot.current = new UPlot({
        width: Math.max(320, host.current.clientWidth), height, legend: { show: false },
        scales: { x: { time: false } },
        cursor: { drag: { x: true, y: false, setScale: true }, sync: { key: SYNC_KEY, scales: ["x", null] } },
        hooks: { setScale: [(chart, key) => {
          if (key !== "x" || quiet.current) return;
          const min = chart.scales.x.min, max = chart.scales.x.max;
          if (min === undefined || max === undefined) return;
          const current = rangeRef.current ?? boundsRef.current;
          if (Math.abs(min - current[0]) > 1e-6 || Math.abs(max - current[1]) > 1e-6) onRangeRef.current([min, max]);
        }] },
        series: [{}, ...lines.map((line, index) => ({ label: line.label, stroke: chartSeriesColor(index), width: 1.5, spanGaps: true, dash: line.dash ? [6, 4] : undefined }))],
        axes: [
          { label: "Elapsed time [d]", space: 70, values: (_u, values) => values.map((value) => fmtNum(value / 86400)) },
          { label: unit, size: 64 },
        ],
      }, data, host.current);
      if (rangeRef.current) { quiet.current = true; try { plot.current.setScale("x", { min: rangeRef.current[0], max: rangeRef.current[1] }); } finally { quiet.current = false; } }
      observer = new ResizeObserver(() => plot.current?.setSize({ width: Math.max(320, host.current?.clientWidth ?? 320), height }));
      observer.observe(host.current);
    }).catch((cause: unknown) => { if (active) setProblem(cause instanceof Error ? cause.message : "The chart could not be rendered."); });
    return () => { active = false; observer?.disconnect(); plot.current?.destroy(); plot.current = null; };
  }, [x, keyed, lines, unit, height, appearance]);

  useEffect(() => {
    const chart = plot.current;
    if (!chart || !x.length) return;
    const target = range ?? [x[0], x[x.length - 1]];
    quiet.current = true;
    try { chart.setScale("x", { min: target[0], max: target[1] }); } finally { quiet.current = false; }
  }, [range, x]);

  const rows = useMemo(() => tableWindow(x, range), [x, range]);
  return <figure className={expanded ? "dyn-chart dyn-chart--expanded" : "dyn-chart"} aria-label={`${title} [${unit}]`}>
    <figcaption><strong>{title}</strong> [{unit}]<button type="button" className="dyn-chart__expand" aria-pressed={expanded} onClick={() => setExpanded((current) => !current)}>{expanded ? "Collapse chart" : "Expand chart"}</button></figcaption>
    {problem ? <p className="dyn-error" role="alert">Chart unavailable: {problem}</p> : null}
    <div ref={host} className="dyn-plot" role="img" aria-label={`${title} chart in ${unit}. ${lines.map((line) => line.label).join(", ")}.`} />
    <div className="dyn-legend" role="list" aria-label={`${title} series`}>{lines.map((line, index) => <span role="listitem" key={line.key}><i style={{ backgroundColor: `var(--color-chart-series-${index % 8 + 1})` }} />{line.label}</span>)}</div>
    <details className="dyn-values"><summary>Values table</summary>
      <p>{rows.total === rows.indices.length ? `${rows.total} rows in range.` : `Showing ${rows.indices.length} of ${rows.total} rows in range.`}</p>
      <div className="dyn-table-scroll"><table aria-label={`${title} values`}><thead><tr><th scope="col">t [s]</th>{lines.map((line) => <th scope="col" key={line.key}>{line.label}</th>)}</tr></thead>
        <tbody>{rows.indices.map((index) => <tr key={index}><td>{fmtNum(x[index])}</td>{lines.map((line) => <td key={line.key}>{line.values[index] === null ? "absent" : fmtNum(line.values[index])}</td>)}</tr>)}</tbody></table></div></details>
  </figure>;
}

/** Keyboard range control for a group of panels: arrows pan, plus/minus zoom, Escape resets. */
export function RangeKeys({ children, range, bounds, onRange, label }: { children: React.ReactNode; range: Range | null; bounds: Range; onRange(next: Range | null): void; label: string }) {
  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest("input, select, textarea, summary")) return;
    const current = range ?? bounds;
    const next = event.key === "ArrowLeft" ? panRange(current, bounds, -0.25) : event.key === "ArrowRight" ? panRange(current, bounds, 0.25)
      : event.key === "+" || event.key === "=" ? zoomRange(current, bounds, 0.5) : event.key === "-" ? zoomRange(current, bounds, 2) : event.key === "Escape" ? null : undefined;
    if (next === undefined) return;
    event.preventDefault(); onRange(next);
  };
  return <div className="dyn-charts" role="group" tabIndex={0} aria-label={label} aria-describedby="dyn-range-help" onKeyDown={onKeyDown}>
    <p id="dyn-range-help" className="dyn-hint">Focus the charts, then use Left and Right to pan, plus and minus to zoom, Escape to reset. The range inputs do the same.</p>{children}</div>;
}

/** Panels of one run, one per physical unit, all on the same x-range. */
export function SyncCharts({ series, units, names, range, onRange, bounds }: { series: SeriesData; units: Record<string, string>; names: string[]; range: Range | null; onRange(next: Range | null): void; bounds: Range }) {
  const x = series.channels.t_s ?? [];
  const groups = useMemo(() => groupChannelsByUnit(Object.fromEntries(names.filter((name) => series.channels[name]).map((name) => [name, units[name] ?? "unit not recorded"]))), [names, series, units]);
  return <RangeKeys label="Time-series charts" range={range} bounds={bounds} onRange={onRange}>
    <RangeControls range={range} bounds={bounds} onRange={onRange} />
    {groups.map((group) => <ChartPanelFor key={group.unit} unit={group.unit} names={group.names} series={series} x={x} range={range} bounds={bounds} onRange={onRange} />)}
    {!groups.length ? <p>No channel is selected.</p> : null}
  </RangeKeys>;
}

function ChartPanelFor({ unit, names, series, x, range, bounds, onRange }: { unit: string; names: string[]; series: SeriesData; x: number[]; range: Range | null; bounds: Range; onRange(next: Range | null): void }) {
  const lines = useMemo<Line[]>(() => names.map((name) => ({ key: name, label: channelLabel(name, unit), values: series.channels[name] })), [names, series, unit]);
  return <ChartPanel title={`Channels in ${unit}`} unit={unit} x={x} lines={lines} range={range} bounds={bounds} onRange={onRange} />;
}
