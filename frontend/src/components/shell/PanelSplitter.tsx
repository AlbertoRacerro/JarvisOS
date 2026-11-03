import { useRef, type KeyboardEvent, type PointerEvent } from "react";
import { PANEL_WIDTH_BOUNDS, clampPanelWidth, keyboardPanelWidth, type ShellPanel } from "./panelWidths";

type PanelSplitterProps = Readonly<{
  panel: ShellPanel;
  /** Rendered width of the panel, used as the drag and keyboard origin. */
  width(): number;
  onResize(width: number): void;
  onCommit(width: number): void;
  onReset(): void;
}>;

const LABELS: Readonly<Record<ShellPanel, string>> = { navigator: "Resize navigator", sidecar: "Resize Jarvis sidecar" };

// Spec 213: a WAI-ARIA window splitter (focusable separator) for the shell navigator and sidecar.
function PanelSplitter({ panel, width, onResize, onCommit, onReset }: PanelSplitterProps) {
  const drag = useRef<{ startX: number; startWidth: number; last: number } | null>(null);
  const direction = panel === "navigator" ? 1 : -1;
  const current = Math.round(width());

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    const startWidth = width();
    drag.current = { startX: event.clientX, startWidth, last: startWidth };
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return;
    const next = clampPanelWidth(panel, drag.current.startWidth + (event.clientX - drag.current.startX) * direction);
    drag.current.last = next;
    onResize(next);
  };
  const endDrag = () => {
    if (!drag.current) return;
    onCommit(drag.current.last);
    drag.current = null;
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const next = keyboardPanelWidth(panel, width(), event.key, event.shiftKey);
    if (next === null) return;
    event.preventDefault();
    onResize(next);
    onCommit(next);
  };

  return (
    <div
      className={`shell-splitter shell-splitter--${panel}`}
      role="separator"
      aria-orientation="vertical"
      aria-label={LABELS[panel]}
      aria-controls={`shell-${panel}`}
      aria-valuemin={PANEL_WIDTH_BOUNDS[panel].min}
      aria-valuemax={PANEL_WIDTH_BOUNDS[panel].max}
      aria-valuenow={current}
      title={`${LABELS[panel]} (drag, arrow keys; double-click to reset)`}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onKeyDown={onKeyDown}
      onDoubleClick={onReset}
    />
  );
}

export default PanelSplitter;
