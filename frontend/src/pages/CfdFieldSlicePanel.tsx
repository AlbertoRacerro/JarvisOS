import { useEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import { getComponentCfdRunFields, type CfdRun } from "../api/designProject";
import { designTarget, type DesignTargetId } from "../app/designTargets";
import type { DesignPanelState } from "../app/workspaceActionSurface";
import { DesignStatus, Empty } from "../components/design/DesignStatus";
import { buildPolygons, fieldRange, fieldUnits, legendGradient, parseFieldPayload, polygonValues, projectPoints, sampleValues, type FieldName, type ParsedField } from "./cfdFieldSlice";

/** `run` is the run this section shows (null: nothing to show yet). Only the `primary` block carries the stable targets. */
type Props = { projectId: string; versionId: string; componentId: string; run: CfdRun | null; primary?: boolean; onFieldState?: (state: DesignPanelState) => void };
const WIDTH = 640, HEIGHT = 320;
// Spec 229: a long thin tube section is drawn at least this fraction of the viewport tall, with the stretch declared.
const MIN_FILL = 0.6;
const message = (cause: unknown, fallback: string) => cause instanceof Error ? cause.message : fallback;
const fmt = (value: number) => Number(value).toPrecision(4);

/** Spec 216: pressure and |U| on the axial symmetry plane of a finished CFD run. Evidence only; nothing is interpolated or invented. */
export default function CfdFieldSlicePanel({ projectId, versionId, componentId, run: selectedRun, primary = false, onFieldState }: Props) {
  const target = (id: DesignTargetId) => primary ? designTarget(id) : {};
  // Only a finished run has fields to read.
  const runId = selectedRun?.state === "finished" ? selectedRun.run_id : null;
  const [field, setField] = useState<FieldName>("p");
  const [result, setResult] = useState<ParsedField | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [sampleIndex, setSampleIndex] = useState<number | null>(null);
  const drag = useRef<{ x: number; y: number; panX: number; panY: number; moved: boolean } | null>(null);

  useEffect(() => {
    setResult(null); setError(null); setSampleIndex(null);
    if (!runId) return;
    let active = true;
    setLoading(true);
    getComponentCfdRunFields(projectId, versionId, componentId, runId)
      .then((payload) => { if (active) setResult(parseFieldPayload(payload)); })
      .catch((cause) => { if (active) setError(message(cause, "The field slice request failed.")); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [projectId, versionId, componentId, runId]);

  // ADR-061 R22: publish the primary field's loading state while its owner read is pending.
  const fieldState: DesignPanelState = !runId ? "empty" : error ? "unavailable"
    : loading || !result ? "loading" : result.kind === "available" ? "available" : "unavailable";
  useEffect(() => { if (primary) onFieldState?.(fieldState); }, [fieldState, onFieldState, primary]);

  const slice = result?.kind === "available" ? result.slice : null;
  const gradient = selectedRun?.measurement?.status === "measured" ? selectedRun.measurement.pressure_gradient_pa_m : null;
  const reynolds = selectedRun?.measurement?.status === "measured" ? selectedRun.measurement.reynolds : undefined;
  const drawn = useMemo(() => slice ? buildPolygons(slice, field, WIDTH, HEIGHT, MIN_FILL) : null, [slice, field]);
  const positions = useMemo(() => slice ? projectPoints(slice) : [], [slice]);
  const values = useMemo(() => slice ? (slice.association === "cell" ? polygonValues(slice, field) : sampleValues(slice, field)) : [], [slice, field]);
  // Spec 230: the viewBox is cropped to the drawn section (plus a margin), so the field fills the figure instead of a
  // fixed 2:1 box. Coordinates are unchanged; only the visible window is chosen.
  const view = useMemo(() => {
    if (!drawn || !positions.length) return { x: 0, y: 0, w: WIDTH, h: HEIGHT };
    let top = Infinity, bottom = -Infinity;
    for (const point of positions) { const y = drawn.fit.offsetY - point[1] * drawn.fit.scaleY; top = Math.min(top, y); bottom = Math.max(bottom, y); }
    const pad = 10;
    return { x: 0, y: Math.max(0, top - pad), w: WIDTH, h: Math.min(HEIGHT, bottom - top + 2 * pad) };
  }, [drawn, positions]);
  const center = { x: view.x + view.w / 2, y: view.y + view.h / 2 };
  const sample = slice && sampleIndex !== null && values[sampleIndex] !== undefined ? (() => {
    if (slice.association === "point") return { position: positions[sampleIndex], value: values[sampleIndex] };
    const polygon = slice.polygons[sampleIndex];
    if (!polygon) return null;
    const position: [number, number] = [
      polygon.reduce((sum, index) => sum + positions[index]![0], 0) / polygon.length,
      polygon.reduce((sum, index) => sum + positions[index]![1], 0) / polygon.length
    ];
    return { position, value: values[sampleIndex] };
  })() : null;
  const toView = (event: { clientX: number; clientY: number }, svg: SVGSVGElement) => {
    const bounds = svg.getBoundingClientRect();
    return { x: view.x + (event.clientX - bounds.left) * view.w / bounds.width, y: view.y + (event.clientY - bounds.top) * view.h / bounds.height };
  };
  const inspect = (polygonIndex: number, event: PointerEvent<SVGPolygonElement>) => {
    if (!slice || !drawn || drag.current?.moved) return;
    if (slice.association === "cell") { setSampleIndex(polygonIndex); return; }
    const polygon = slice.polygons[polygonIndex];
    const svg = event.currentTarget.ownerSVGElement;
    if (!polygon || !svg) return;
    const { x, y } = toView(event, svg);
    let nearest = polygon[0]!, distance = Infinity;
    for (const index of polygon) {
      const point = positions[index]!;
      const dx = point[0] * drawn.fit.scaleX + drawn.fit.offsetX;
      const dy = drawn.fit.offsetY - point[1] * drawn.fit.scaleY;
      const d = (x - (center.x + (dx - center.x) * zoom + pan.x)) ** 2 + (y - (center.y + (dy - center.y) * zoom + pan.y)) ** 2;
      if (d < distance) { nearest = index; distance = d; }
    }
    setSampleIndex(nearest);
  };
  const resetView = () => { setZoom(1); setPan({ x: 0, y: 0 }); };
  const units = slice ? fieldUnits(slice, field) : "";
  const range = slice ? fieldRange(slice, field) : null;

  return <section aria-label="CFD field slice" className="design-project__field-slice" {...target("design.panel.cfd-field")}>
    <div className="design-project__field-head">
      <h4>Field</h4>
      {slice && drawn ? <>
        <div className="design-project__segmented" role="group" aria-label="Field">
          <button type="button" aria-pressed={field === "p"} onClick={() => setField("p")} {...target("design.field.pressure")}>p</button>
          <button type="button" aria-pressed={field === "Umag"} onClick={() => setField("Umag")} {...target("design.field.velocity")}>|U|</button>
        </div>
        <div className="design-project__field-tools" role="group" aria-label="Field view controls">
          <button type="button" onClick={() => setZoom((value) => Math.max(1, value / 1.5))} {...target("design.field.zoom-out")}>Zoom out</button>
          <span aria-live="polite">{Math.round(zoom * 100)}%</span>
          <button type="button" onClick={() => setZoom((value) => Math.min(16, value * 1.5))} {...target("design.field.zoom-in")}>Zoom in</button>
          <button type="button" onClick={resetView} {...target("design.field.reset")}>Reset view</button>
        </div>
      </> : null}
    </div>
    {error ? <p role="alert" className="design-project__error"><DesignStatus state="failed" title={error} /></p> : null}
    <dl className="design-project__field-facts">
      <div><dt>Re</dt><dd>{typeof reynolds === "number" ? Math.round(reynolds) : <Empty />}</dd></div>
      <div><dt>Measured ΔP/L</dt><dd>{typeof gradient === "number" && Number.isFinite(gradient) ? `${fmt(gradient)} Pa/m` : <Empty />}</dd></div>
      <div><dt title="From the developed-pipe force balance">Wall shear τ<sub>w</sub></dt><dd>{selectedRun?.measurement?.status === "measured" && typeof selectedRun.measurement.wall_shear_pa === "number"
        ? `${fmt(selectedRun.measurement.wall_shear_pa)} Pa` : <Empty />}</dd></div>
      <div><dt>Section</dt><dd>{slice ? `2D axial · t = ${slice.time} · ${slice.cellCount} cells` : <Empty />}</dd></div>
    </dl>
    {loading ? <p role="status"><DesignStatus state="checking" /></p> : null}
    {result?.kind === "unavailable" ? <p role="status"><DesignStatus state="empty" title={result.reason} /></p> : null}
    {result?.kind === "too_large" ? <p role="status"><span className="status-word status-word--missing" title={result.reason}><span aria-hidden="true">○</span> Too large</span></p> : null}
    {result?.kind === "invalid" ? <p role="alert" className="design-project__error"><DesignStatus state="failed" title={result.error} /></p> : null}
    {!runId ? <div className="design-project__viewport-empty" role="status" title={selectedRun ? "This run has no field: it did not finish" : "No finished CFD run for this component"}><DesignStatus state="empty" /></div> : null}
    {slice && drawn && range ? <figure className="design-project__field-figure">
      <svg viewBox={`${view.x} ${view.y} ${view.w} ${view.h}`} style={{ aspectRatio: `${view.w} / ${view.h}` }} role="img" tabIndex={0}
        aria-label={`${field === "p" ? "Pressure" : "Velocity magnitude"} on the axial section (2D); drag to pan, arrow keys to inspect samples, + and - to zoom, 0 to reset`}
        onPointerDown={(event) => { if (event.button !== 0) return; event.currentTarget.setPointerCapture(event.pointerId); drag.current = { x: event.clientX, y: event.clientY, panX: pan.x, panY: pan.y, moved: false }; }}
        onPointerMove={(event) => { if (!drag.current) return; const bounds = event.currentTarget.getBoundingClientRect(); const dx = event.clientX - drag.current.x, dy = event.clientY - drag.current.y; if (Math.abs(dx) + Math.abs(dy) > 3) drag.current.moved = true; setPan({ x: drag.current.panX + dx * view.w / bounds.width, y: drag.current.panY + dy * view.h / bounds.height }); }}
        onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}
        onKeyDown={(event) => {
          if (event.key === "+" || event.key === "=") { event.preventDefault(); setZoom((value) => Math.min(16, value * 1.5)); return; }
          if (event.key === "-") { event.preventDefault(); setZoom((value) => Math.max(1, value / 1.5)); return; }
          if (event.key === "0") { event.preventDefault(); resetView(); return; }
          if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
          event.preventDefault(); const length = values.length; if (length) setSampleIndex((current) => (Math.max(0, current ?? 0) + (event.key === "ArrowRight" ? 1 : length - 1)) % length); }}>
        <g transform={`translate(${center.x + pan.x} ${center.y + pan.y}) scale(${zoom}) translate(${-center.x} ${-center.y})`}>
          {drawn.polygons.map((polygon, index) => <polygon key={index} points={polygon.points} fill={polygon.fill} stroke={polygon.fill} strokeWidth={0.5 / zoom} onPointerMove={(event) => inspect(index, event)} />)}
        </g>
      </svg>
      <figcaption>
        <div className="design-project__field-legend" aria-label="Colour legend">
          <span>{field === "p" ? "p" : "|U|"}</span>
          <span>{fmt(range.min)}</span>
          <span className="design-project__field-legend-bar" style={{ background: legendGradient() }} />
          <span>{fmt(range.max)}</span>
          <span>{units}</span>
        </div>
        <p className="design-project__field-readout" role="status">{sample?.position && sample.value !== undefined
          ? <><strong>{fmt(sample.value)} {units}</strong> at x = {fmt(sample.position[0])}, y = {fmt(sample.position[1])} ({slice.association === "cell" ? `cell ${sampleIndex! + 1}` : `point ${sampleIndex! + 1}`})</>
          : <span className="design-empty" title="Or focus the section and use the arrow keys">Point at the section to read a value</span>}</p>
        {drawn.fit.stretchAxis ? <p role="note" className="design-project__field-stretch">Display only: the {drawn.fit.stretchAxis === "y" ? "vertical" : "horizontal"} axis is stretched ×{drawn.fit.stretch.toPrecision(3)} so the thin section is readable. Field values and geometry data are unchanged.</p> : null}
      </figcaption>
    </figure> : null}
    {primary ? <details className="design-project__field-about"><summary>Details</summary><p>Pressure and velocity magnitude on the axial symmetry plane of a finished run, as stored by the solver{slice ? ` (${slice.association} data)` : ""}. Positions in metres. Evidence only: this view does not qualify a study or publish a reduced model.</p></details> : null}
  </section>;
}
