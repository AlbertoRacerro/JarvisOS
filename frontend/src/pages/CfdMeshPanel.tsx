import { useEffect, useState } from "react";
import { listComponentCfdRuns, type CfdRun } from "../api/designProject";
import { Empty } from "../components/design/DesignStatus";
import { readableProse, runLabel } from "../components/design/designText";

type Props = { projectId: string; versionId: string; componentId: string };

/** Mesh evidence belongs to completed and attempted CFD runs; no mesh is implied before a run. */
export default function CfdMeshPanel({ projectId, versionId, componentId }: Props) {
  const [runs, setRuns] = useState<CfdRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    setRuns(null); setError(null);
    listComponentCfdRuns(projectId, versionId, componentId)
      .then((items) => { if (active) setRuns(items); })
      .catch((cause: unknown) => { if (active) setError(cause instanceof Error ? cause.message : "Mesh evidence is unavailable."); });
    return () => { active = false; };
  }, [projectId, versionId, componentId]);

  const measured = (run: CfdRun) => run.measurement?.status === "measured" ? run.measurement : null;
  return <div aria-label="CFD mesh evidence">
    {error ? <p role="alert" className="design-project__error">{error}</p> : null}
    <table className="design-project__table" aria-label="Mesh per CFD run" title="Each CFD run builds and checks its own tube mesh">
      <thead><tr><th>Run</th><th>Mesh</th><th>Level</th><th>Cells</th></tr></thead>
      <tbody>{runs === null && !error ? <tr><td colSpan={4}>Loading…</td></tr>
        : !runs?.length ? <tr><td><Empty /></td><td><Empty /></td><td><Empty /></td><td><Empty /></td></tr>
        : runs.map((run) => <tr key={run.run_id} title={run.run_id}>
          <td>{runLabel(run)}</td>
          <td title={run.mesh_reason ?? undefined}>{run.mesh_status ? readableProse(run.mesh_status) : <Empty />}</td>
          <td>{measured(run)?.mesh_level ?? <Empty />}</td>
          <td>{measured(run)?.cells ?? <Empty />}</td>
        </tr>)}</tbody>
    </table>
  </div>;
}
