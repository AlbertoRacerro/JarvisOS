import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { ChatCircleDots, ChartLine, SidebarSimple } from "@phosphor-icons/react";

import type { Navigate } from "../app/AppLink";
import type { AppRouteDefinition } from "../app/routes";
import type { StageSelection } from "../app/selection";
import type { ShellRegion, ShellRegionContributions } from "../stages/registry";
import { APPEARANCE_OPTIONS, applyAppearancePreference, applyResolvedAppearance, readAppearancePreference, subscribeToSystemAppearance, subscribeToVisualPreferenceUpdates, writeAppearancePreference, type AppearancePreference } from "../theme";
import AnalysisDock from "./shell/AnalysisDock";
import ContextualNavigator from "./shell/ContextualNavigator";
import ContextualSidecar from "./shell/ContextualSidecar";
import PanelSplitter from "./shell/PanelSplitter";
import { readPanelWidths, writePanelWidths, type PanelWidths, type ShellPanel } from "./shell/panelWidths";
import Rail from "./shell/Rail";
import BubuBubble, { type BubuIntegration } from "./bubu/BubuBubble";
import BubuLauncher from "./shell/BubuLauncher";
import TopBar from "./shell/TopBar";
import Button from "./ui/Button";
import Field from "./ui/Field";

type LayoutProps = Readonly<{
  route: AppRouteDefinition;
  navigate: Navigate;
  selection: StageSelection | null;
  propertiesContent?: ReactNode;
  shellRegions: ShellRegionContributions;
  shellRegionRequest: Readonly<{ region: ShellRegion; nonce: number }> | null;
  project?: ReactNode;
  /** Extra Bubu wiring (dictation, page description, reply rendering) supplied by the app. */
  bubu?: BubuIntegration;
  children: ReactNode;
}>;


function Layout({ route, navigate, selection, propertiesContent, shellRegions, shellRegionRequest, project, bubu, children }: LayoutProps) {
  const [bubbleOpen, setBubbleOpen] = useState(false);
  const [asleep, setAsleep] = useState(false);
  const [appearance, setAppearance] = useState<AppearancePreference>(() => readAppearancePreference());
  const [navigatorOpen, setNavigatorOpen] = useState(false);
  const [sidecarOpen, setSidecarOpen] = useState(false);
  const [dockOpen, setDockOpen] = useState(false);
  const mainRef = useRef<HTMLElement>(null);
  const navigatorToggleRef = useRef<HTMLButtonElement>(null);
  const sidecarToggleRef = useRef<HTMLButtonElement>(null);
  const dockToggleRef = useRef<HTMLButtonElement>(null);
  const shellRef = useRef<HTMLDivElement>(null);
  const [panelWidths, setPanelWidths] = useState<PanelWidths>(() => readPanelWidths());

  useEffect(() => { applyAppearancePreference(appearance); return subscribeToSystemAppearance(appearance, applyResolvedAppearance); }, [appearance]);
  useEffect(() => subscribeToVisualPreferenceUpdates(() => setAppearance(readAppearancePreference())), []);
  useEffect(() => {
    // BUBU-LAUNCHER-1: pages start with the full work area; Bubu reopens Jarvis.
    // BlueCad keeps its existing model/feature navigator.
    setNavigatorOpen(route.id === "design-bluecad");
    setSidecarOpen(false);
    setDockOpen(false);
    document.title = `${route.title} · JarvisOS`;
    const frame = window.requestAnimationFrame(() => mainRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [route.id, route.path, route.title]);
  useEffect(() => {
    if (!shellRegionRequest) return;
    if (shellRegionRequest.region === "navigator") setNavigatorOpen(true);
    if (shellRegionRequest.region === "sidecar") setSidecarOpen(true);
    if (shellRegionRequest.region === "dock") setDockOpen(true);
  }, [shellRegionRequest]);

  // Spec 213: drag updates the CSS variable directly; the persisted width is committed on release.
  const panelWidthVariable = (panel: ShellPanel) => `--shell-${panel}-width`;
  const renderedPanelWidth = (panel: ShellPanel) => document.getElementById(`shell-${panel}`)?.getBoundingClientRect().width ?? panelWidths[panel] ?? 0;
  const previewPanelWidth = (panel: ShellPanel, width: number) => shellRef.current?.style.setProperty(panelWidthVariable(panel), `${width}px`);
  const commitPanelWidth = (panel: ShellPanel, width: number) => setPanelWidths((current) => { const next = { ...current, [panel]: width }; writePanelWidths(next); return next; });
  const resetPanelWidth = (panel: ShellPanel) => { shellRef.current?.style.removeProperty(panelWidthVariable(panel)); setPanelWidths((current) => { const next = { ...current }; delete next[panel]; writePanelWidths(next); return next; }); };
  const shellStyle = Object.fromEntries((Object.entries(panelWidths) as [ShellPanel, number][]).map(([panel, width]) => [panelWidthVariable(panel), `${width}px`])) as CSSProperties;
  const splitter = (panel: ShellPanel) => <PanelSplitter panel={panel} width={() => renderedPanelWidth(panel)} onResize={(width) => previewPanelWidth(panel, width)} onCommit={(width) => commitPanelWidth(panel, width)} onReset={() => resetPanelWidth(panel)} />;
  const onAppearanceChange = (preference: AppearancePreference) => { writeAppearancePreference(preference); applyAppearancePreference(preference); setAppearance(preference); };
  const appearanceSelect = <select aria-label="Appearance preference" value={appearance} onChange={(event) => onAppearanceChange(event.target.value as AppearancePreference)}>{APPEARANCE_OPTIONS.map((option) => <option key={option} value={option}>{option[0].toUpperCase() + option.slice(1)}</option>)}</select>;
  // Bubu is always mounted, including while the Sidecar is closed.
  const closeNavigator = useCallback(() => { setNavigatorOpen(false); window.requestAnimationFrame(() => navigatorToggleRef.current?.focus()); }, []);
  const closeSidecar = useCallback(() => { setSidecarOpen(false); window.requestAnimationFrame(() => sidecarToggleRef.current?.focus()); }, []);
  // BUBU-2: Bubu opens a speech bubble; Expand opens the full Jarvis panel (the old Bubu click).
  const toggleBubble = () => {
    if (sidecarOpen) { closeSidecar(); return; }
    if (asleep) { setAsleep(false); setBubbleOpen(true); return; }
    setBubbleOpen((current) => !current);
  };
  const expandJarvis = () => {
    setBubbleOpen(false);
    // Some existing pages own their conversation in-page. Reveal that same composer.
    if (!shellRegions.sidecar) {
      const composer = document.getElementById("jarvis-prompt");
      if (composer) {
        composer.scrollIntoView({ block: "center", inline: "nearest" });
        composer.focus({ preventScroll: true });
        return;
      }
    }
    setSidecarOpen(true);
  };
  const sleepBubu = () => { setBubbleOpen(false); setAsleep(true); bubu?.onSleep?.(); };
  useEffect(() => { if (sidecarOpen) setBubbleOpen(false); }, [sidecarOpen]);
  const closeDock = useCallback(() => { setDockOpen(false); window.requestAnimationFrame(() => dockToggleRef.current?.focus()); }, []);
  const panelControls = <><Button ref={navigatorToggleRef} variant="ghost" aria-expanded={navigatorOpen} aria-controls="shell-navigator" onClick={() => setNavigatorOpen((current) => !current)}><SidebarSimple size={16} aria-hidden="true" />{navigatorOpen ? "Hide navigator" : "Show navigator"}</Button><Button variant="ghost" aria-expanded={sidecarOpen} aria-controls="shell-sidecar" onClick={() => setSidecarOpen((current) => !current)}><ChatCircleDots size={16} aria-hidden="true" />{sidecarOpen ? "Hide Jarvis" : "Show Jarvis"}</Button><Button ref={dockToggleRef} variant="ghost" aria-expanded={dockOpen} aria-controls="shell-analysis-dock" onClick={() => setDockOpen((current) => !current)}><ChartLine size={16} aria-hidden="true" />{dockOpen ? "Hide analysis" : "Show analysis"}</Button></>;
  const finalOperatorRoute = route.primaryNav !== undefined;


  return <div ref={shellRef} style={shellStyle} className={`application-shell${finalOperatorRoute ? " application-shell--final" : ""}`}><a className="shell-skip-link" href="#app-main">Skip to main content</a>{!finalOperatorRoute && <TopBar title={route.title} panelControls={panelControls} appearanceControl={<Field className="appearance-control shell-appearance-control" label="Appearance" control={appearanceSelect} />} />}<Rail current={route.primaryNav} currentPath={route.path} navigate={navigate} project={project} /><div className="shell-workspace">{finalOperatorRoute && !navigatorOpen && shellRegions.navigator ? <button ref={navigatorToggleRef} type="button" className="shell-reopen shell-reopen--navigator" aria-controls="shell-navigator" aria-expanded="false" onClick={() => setNavigatorOpen(true)} title="Show navigator"><SidebarSimple size={16} aria-hidden="true" /><span>Navigator</span></button> : <ContextualNavigator open={navigatorOpen} route={route} navigate={navigate} onClose={closeNavigator} content={shellRegions.navigator} />}<main id="app-main" className="shell-main" ref={mainRef} tabIndex={-1}>{children}</main>{finalOperatorRoute && navigatorOpen && shellRegions.navigator ? splitter("navigator") : null}{finalOperatorRoute && sidecarOpen && shellRegions.sidecar ? splitter("sidecar") : null}<ContextualSidecar open={sidecarOpen} selection={selection} onClose={closeSidecar} content={shellRegions.sidecar} propertiesContent={propertiesContent} /></div><AnalysisDock open={dockOpen} onClose={closeDock} content={shellRegions.dock} /><BubuLauncher buttonRef={sidecarToggleRef} expanded={sidecarOpen} bubbleOpen={bubbleOpen} asleep={asleep} listening={bubu?.listening} onActivate={toggleBubble} /><BubuBubble open={bubbleOpen} onClose={() => setBubbleOpen(false)} onExpand={expandJarvis} onSleep={sleepBubu} returnFocusRef={sidecarToggleRef} extraTools={bubu?.extraTools} getExtras={bubu?.getExtras} renderReply={bubu?.renderReply} draftInsert={bubu?.draftInsert} status={bubu?.status} /></div>;
}

export default Layout;