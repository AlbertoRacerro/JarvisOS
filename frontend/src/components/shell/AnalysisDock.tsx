import { useEffect, useRef, type KeyboardEvent, type ReactNode } from "react";
import Button from "../ui/Button";
import InlineNotice from "../ui/InlineNotice";

type AnalysisDockProps = Readonly<{ open: boolean; onClose(): void; content?: ReactNode }>;
function AnalysisDock({ open, onClose, content }: AnalysisDockProps) {
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  useEffect(() => { if (open) headingRef.current?.focus(); }, [open]);
  const onPanelKeyDown = (event: KeyboardEvent<HTMLElement>) => { if (event.key === "Escape") { event.stopPropagation(); onClose(); } };
  if (!open) return null;
  return <section id="shell-analysis-dock" className="shell-panel shell-analysis-dock" aria-labelledby="shell-analysis-title" onKeyDown={onPanelKeyDown}><div className="shell-panel__header"><h2 id="shell-analysis-title" ref={headingRef} tabIndex={-1}>Analysis dock</h2><Button variant="ghost" onClick={onClose}>Close analysis dock</Button></div>{content ?? <InlineNotice tone="neutral">Analytics are not available in APP-SHELL-1. Real-data analysis belongs to spec 089.</InlineNotice>}</section>;
}
export default AnalysisDock;
