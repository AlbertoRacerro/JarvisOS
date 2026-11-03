import { useEffect, useRef, type KeyboardEvent, type ReactNode } from "react";
import AppLink, { type Navigate } from "../../app/AppLink";
import { PEER_NAV_ITEMS, ROADMAP_STAGE_ITEMS, type AppRouteDefinition } from "../../app/routes";
import Button from "../ui/Button";

type ContextualNavigatorProps = Readonly<{ open: boolean; route: AppRouteDefinition; navigate: Navigate; onClose(): void; content?: ReactNode }>;

function ContextualNavigator({ open, route, navigate, onClose, content }: ContextualNavigatorProps) {
  const panelRef = useRef<HTMLElement | null>(null);
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  useEffect(() => {
    if (open && !panelRef.current?.contains(document.activeElement)) headingRef.current?.focus();
  }, [open]);
  const onPanelKeyDown = (event: KeyboardEvent<HTMLElement>) => { if (event.key === "Escape") { event.stopPropagation(); onClose(); } };
  if (!open) return null;

  const peers = route.primaryNav ? PEER_NAV_ITEMS[route.primaryNav] : [];
  const defaultContent = peers.length > 0 ? (
    <>
      <nav className="shell-stage-links" aria-label={`${route.primaryNav} navigation`}>
        {peers.map((item) => <AppLink key={item.href} href={item.href} navigate={navigate} className="shell-nav-link" aria-current={route.path === item.href || (item.href === "/development/roadmap/timeline" && route.path.startsWith("/development/roadmap/")) ? "page" : undefined}>{item.label}</AppLink>)}
      </nav>
      {route.path.startsWith("/development/roadmap/") && <nav className="shell-stage-links" aria-label="Roadmap views">{ROADMAP_STAGE_ITEMS.map((item) => <AppLink key={item.href} href={item.href} navigate={navigate} className="shell-nav-link" aria-current={route.path === item.href ? "page" : undefined}>{item.label}</AppLink>)}</nav>}
    </>
  ) : <p className="panel-subtitle">No contextual navigation is available for this compatibility route.</p>;

  return <aside ref={panelRef} id="shell-navigator" className="shell-panel shell-navigator" aria-labelledby="shell-navigator-title" onKeyDown={onPanelKeyDown}><div className="shell-panel__header"><h2 id="shell-navigator-title" ref={headingRef} tabIndex={-1}>Navigator</h2><Button variant="ghost" onClick={onClose}>Close navigator</Button></div>{content ?? defaultContent}</aside>;
}
export default ContextualNavigator;
