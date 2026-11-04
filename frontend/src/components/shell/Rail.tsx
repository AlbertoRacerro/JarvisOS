import type { ReactNode } from "react";
import { ArrowLeft, ArrowRight } from "@phosphor-icons/react";

import AppLink, { type Navigate } from "../../app/AppLink";
import { designTarget, type DesignTargetId } from "../../app/designTargets";
import { useHistoryPosition } from "../../app/navHistory";
import { DESIGN_TOOL_ITEMS, PRIMARY_NAV_ITEMS, type PrimaryNavId } from "../../app/routes";

const RAIL_TARGET: Readonly<Record<PrimaryNavId, DesignTargetId>> = {
  design: "rail.design", memory: "rail.memory", development: "rail.development", coding: "rail.coding", settings: "rail.settings"
};

type RailProps = Readonly<{
  current: PrimaryNavId | undefined;
  currentPath: string;
  navigate: Navigate;
  project?: ReactNode;
}>;

// Back and Forward, always in the rail's top corner on every page, walk the app's own history: pages and the
// Design view (stage, component, node). Each is disabled when there is no app page to go to.
function HistoryArrows() {
  const { canGoBack, canGoForward } = useHistoryPosition();
  return (
    <div className="shell-history" role="group" aria-label="History">
      <button type="button" className="shell-history__button" aria-label="Back" title="Back" disabled={!canGoBack} onClick={() => window.history.back()} {...designTarget("rail.back")}>
        <ArrowLeft size={16} aria-hidden="true" />
      </button>
      <button type="button" className="shell-history__button" aria-label="Forward" title="Forward" disabled={!canGoForward} onClick={() => window.history.forward()} {...designTarget("rail.forward")}>
        <ArrowRight size={16} aria-hidden="true" />
      </button>
    </div>
  );
}

// Spec 229: Design opens New Design, the single Design entry. The older Design surfaces and the Design-adjacent
// pages live in one secondary, collapsed group under it, on every page, so each stays one expand and one click away.
function Rail({ current, currentPath, navigate, project }: RailProps) {
  const onTool = DESIGN_TOOL_ITEMS.some((tool) => tool.href === currentPath);
  return (
    <nav className="shell-rail" aria-label="Primary navigation">
      <HistoryArrows />
      <div className="shell-rail__identity" aria-label="JarvisOS">
        <strong>JarvisOS</strong>
        <span>Engineering workspace</span>
      </div>
      {project}
      <div className="shell-rail__links">
        {PRIMARY_NAV_ITEMS.map((item) => (
          <div key={item.id} className="shell-rail__group">
            <AppLink
              href={item.href}
              navigate={navigate}
              className="shell-nav-link"
              aria-current={current === item.id ? (item.id === "design" && currentPath !== item.href ? "true" : "page") : undefined}
              {...designTarget(RAIL_TARGET[item.id])}
            >
              {item.label}
            </AppLink>
            {item.id === "design" ? (
              <nav className="shell-rail__tools" aria-label="Design tools" {...designTarget("rail.tools")}>
                <details key={onTool ? "open" : "closed"} open={onTool}>
                  <summary>More Design tools</summary>
                  <ul className="shell-rail__switcher">
                    {DESIGN_TOOL_ITEMS.map((tool) => (
                      <li key={tool.href}>
                        <AppLink href={tool.href} navigate={navigate} aria-current={currentPath === tool.href ? "page" : undefined} {...designTarget(tool.target)}>
                          {tool.label}
                        </AppLink>
                      </li>
                    ))}
                  </ul>
                </details>
              </nav>
            ) : null}
          </div>
        ))}
      </div>
    </nav>
  );
}

export default Rail;
