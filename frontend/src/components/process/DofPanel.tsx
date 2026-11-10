import { useEffect, useState } from "react";
import {
  getDraftDof,
  unitLabel,
  type DofReport,
} from "../../api/processDraft";
import "./DofPanel.css";

export interface DofPanelProps {
  workspaceId: string;
  draftId: string;
  revision: string;
}

/**
 * Spec 257: Read-only panel for degrees-of-freedom and missing-inputs report.
 * Shows required specifications for units and feed streams as Set, Unset, or Available from a run.
 */
export function DofPanel({ workspaceId, draftId, revision }: Readonly<DofPanelProps>) {
  const [report, setReport] = useState<DofReport | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);

    getDraftDof(workspaceId, draftId)
      .then((data) => {
        if (!active) return;
        setReport(data);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (!active) return;
        setError(err instanceof Error ? err.message : "Failed to load degrees of freedom");
        setLoading(false);
      });

    return () => {
      active = false;
    };
  }, [workspaceId, draftId, revision]);

  if (loading) {
    return (
      <section className="dof-panel" aria-label="Degrees of freedom">
        <header className="dof-panel__header">
          <h3>Degrees of freedom</h3>
        </header>
        <p className="draft-hint" role="status">Loading degrees of freedom…</p>
      </section>
    );
  }

  if (error) {
    return (
      <section className="dof-panel" aria-label="Degrees of freedom">
        <header className="dof-panel__header">
          <h3>Degrees of freedom</h3>
        </header>
        <p className="draft-field-error" role="alert">{error}</p>
      </section>
    );
  }

  if (!report) {
    return null;
  }

  return (
    <section className="dof-panel" aria-label="Degrees of freedom">
      <header className="dof-panel__header">
        <h3>Degrees of freedom</h3>
      </header>
      <div className="dof-counts" aria-label="Degrees of freedom counts">
        <span>Set: {report.counts.set}</span>
        <span> · </span>
        <span>Unset: {report.counts.unset}</span>
        <span> · </span>
        <span>Available: {report.counts.available}</span>
      </div>
      {report.entries.length === 0 ? (
        <p className="draft-hint">No degrees of freedom entries.</p>
      ) : (
        <ul className="dof-entries" aria-label="Degrees of freedom entries">
          {report.entries.map((entry, index) => (
            <li key={`${entry.object}-${entry.key}-${index}`} className={`dof-entry dof-entry--${entry.status}`}>
              <div className="dof-entry__header">
                <span className="dof-entry__title">{entry.object} · {entry.label}</span>
                <span className={`dof-chip dof-chip--${entry.status}`}>
                  {entry.status === "set" ? "Set" : entry.status === "unset" ? "Unset" : "Available from a run"}
                </span>
              </div>
              {entry.status === "available" && entry.design_value && (
                <div className="dof-entry__detail">
                  <span className="dof-entry__value">
                    {entry.design_value.value} {unitLabel(entry.design_value.unit)}
                  </span>
                  <span className="dof-entry__source">
                    Run {entry.design_value.source.run_id}
                  </span>
                  {entry.design_value.validation === "red_dot" ? (
                    <span className="dof-entry__validation dof-entry__validation--red-dot" role="status">
                      <span className="red-dot-indicator" aria-hidden="true" style={{ color: "#d93025", marginRight: "4px" }}>●</span>
                      Not validated
                    </span>
                  ) : (
                    <span className="dof-entry__validation dof-entry__validation--passed">
                      Passed
                    </span>
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default DofPanel;
