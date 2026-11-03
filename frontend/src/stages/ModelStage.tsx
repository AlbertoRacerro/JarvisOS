import BluecadWorkbench from "../components/bluecad/BluecadWorkbench";
import "../styles/bluecad-final.css";
import type { PrimaryStageProps } from "./registry";

function ModelStage({ workspaceId, onSelectionChange, onShellRegionsChange, requestShellRegionOpen }: PrimaryStageProps) {
  return (
    <section className="bluecad-final-stage design-stage" aria-labelledby="bluecad-final-title">
      <header className="design-stage__header bluecad-final-stage__header">
        <div className="design-stage__title-row">
          <div>
            <p className="eyebrow">Design</p>
            <h1 id="bluecad-final-title">BlueCad workspace</h1>
            <p className="panel-subtitle">Inspect server-built geometry, create deterministic template parts and download STL, STEP or 3MF for printing.</p>
          </div>
          <span className="design-stage__truth-state">Geometry · Server-owned</span>
        </div>
      </header>

      <div className="bluecad-final-stage__body">
        <BluecadWorkbench
          appWorkspaceId={workspaceId}
          onSelectionChange={onSelectionChange}
          onShellRegionsChange={onShellRegionsChange}
          requestShellRegionOpen={requestShellRegionOpen}
        />
      </div>
    </section>
  );
}

export default ModelStage;
