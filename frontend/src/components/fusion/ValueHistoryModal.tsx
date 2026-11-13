import { useEffect, useState } from "react";
import {
  compareValues,
  getParameterHistory,
  getResultValueHistory,
  type ValueComparisonRead,
  type ValueVersionChainRead,
  type ValueVersionItem
} from "../../api/projectKnowledge";

export type HistoryTarget = {
  kind: "parameter" | "simulation_result_value" | "result_value";
  id: string;
  name?: string;
};

type Props = {
  workspaceId: string;
  target: HistoryTarget;
  onClose: () => void;
};

function fmtDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export default function ValueHistoryModal({ workspaceId, target, onClose }: Props) {
  const [chain, setChain] = useState<ValueVersionChainRead | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [versionA, setVersionA] = useState<string>("");
  const [versionB, setVersionB] = useState<string>("");
  const [comparison, setComparison] = useState<ValueComparisonRead | null>(null);
  const [compareLoading, setCompareLoading] = useState(false);
  const [compareError, setCompareError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    setComparison(null);
    setCompareError(null);

    const loader =
      target.kind === "parameter"
        ? getParameterHistory(workspaceId, target.id)
        : getResultValueHistory(workspaceId, target.id);

    loader
      .then((data) => {
        if (!alive) return;
        setChain(data);
        setLoading(false);
        if (data.versions.length >= 2) {
          const first = data.versions[0];
          const last = data.versions[data.versions.length - 1];
          const valA = target.kind === "parameter" ? first.id : String(first.version_index);
          const valB = target.kind === "parameter" ? last.id : String(last.version_index);
          setVersionA(valA);
          setVersionB(valB);
        } else if (data.versions.length === 1) {
          const first = data.versions[0];
          const valA = target.kind === "parameter" ? first.id : String(first.version_index);
          setVersionA(valA);
          setVersionB("");
        }
      })
      .catch((err: Error) => {
        if (!alive) return;
        setError(err.message);
        setLoading(false);
      });

    return () => {
      alive = false;
    };
  }, [workspaceId, target.kind, target.id]);

  const handleCompare = async () => {
    if (!versionA || !versionB || versionA === versionB) return;
    setCompareLoading(true);
    setCompareError(null);
    try {
      const params =
        target.kind === "parameter"
          ? {
              value_kind: "parameter",
              parameter_a_id: versionA,
              parameter_b_id: versionB
            }
          : {
              value_kind: "simulation_result_value",
              value_id: target.id,
              version_a: versionA,
              version_b: versionB
            };
      const res = await compareValues(workspaceId, params);
      setComparison(res);
    } catch (err: unknown) {
      setCompareError(err instanceof Error ? err.message : "Failed to compare versions");
    } finally {
      setCompareLoading(false);
    }
  };

  const renderValidationState = (item: ValueVersionItem | { status?: string | null; validation_state: string }) => {
    const isException = item.status === "exception" || item.validation_state === "exception";
    if (isException) {
      return (
        <span className="validation-state validation-state--exception">
          <span className="red-dot-indicator" aria-label="Exception red dot" style={{ color: "#d93025", marginRight: "4px" }}>
            ●
          </span>
          exception
        </span>
      );
    }
    return <span className="validation-state">{item.validation_state || item.status || "—"}</span>;
  };

  return (
    <div
      className="value-history-backdrop"
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        width: "100%",
        height: "100%",
        backgroundColor: "rgba(0, 0, 0, 0.5)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
        overflowY: "auto",
        padding: "20px"
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="value-history-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="Value history"
        style={{
          backgroundColor: "#fff",
          color: "#111",
          borderRadius: "8px",
          maxWidth: "960px",
          width: "100%",
          maxHeight: "90vh",
          overflowY: "auto",
          padding: "24px",
          boxShadow: "0 4px 20px rgba(0, 0, 0, 0.2)"
        }}
      >
        <header
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            borderBottom: "1px solid #e0e0e0",
            paddingBottom: "12px",
            marginBottom: "16px"
          }}
        >
          <div>
            <p className="eyebrow" style={{ margin: 0, fontSize: "12px", textTransform: "uppercase", color: "#666" }}>
              {target.kind === "parameter" ? "Parameter History" : "Result Value History"}
            </p>
            <h2 style={{ margin: "4px 0 0 0" }}>{target.name || target.id}</h2>
          </div>
          <button type="button" onClick={onClose} aria-label="Close history view">
            Close
          </button>
        </header>

        {loading && <p>Loading history…</p>}
        {error && (
          <div className="runs-inline-error" role="alert">
            <p>Failed to load history: {error}</p>
          </div>
        )}

        {chain && (
          <div>
            {chain.intermediate_versions_unknown && (
              <p
                className="runs-note"
                role="status"
                style={{
                  backgroundColor: "#fff8e1",
                  borderLeft: "4px solid #ffb300",
                  padding: "8px 12px",
                  marginBottom: "16px"
                }}
              >
                Note: intermediate_versions_unknown — Intermediate versions are unknown for this value because historical
                overwritten corrections were backfilled.
              </p>
            )}

            <section aria-label="Ordered version chain" style={{ marginBottom: "24px" }}>
              <h3>Version chain</h3>
              {chain.versions.length === 0 ? (
                <p>No recorded versions found.</p>
              ) : (
                <table
                  className="value-history-table"
                  style={{ width: "100%", borderCollapse: "collapse", textAlign: "left", fontSize: "14px" }}
                >
                  <thead>
                    <tr style={{ borderBottom: "2px solid #ccc" }}>
                      <th style={{ padding: "6px" }}>Version</th>
                      <th style={{ padding: "6px" }}>Value</th>
                      <th style={{ padding: "6px" }}>Unit</th>
                      <th style={{ padding: "6px" }}>Validation state</th>
                      <th style={{ padding: "6px" }}>Source</th>
                      <th style={{ padding: "6px" }}>Actor</th>
                      <th style={{ padding: "6px" }}>Reason</th>
                      <th style={{ padding: "6px" }}>Timestamp</th>
                    </tr>
                  </thead>
                  <tbody>
                    {chain.versions.map((ver) => {
                      const verKey = target.kind === "parameter" ? ver.id : String(ver.version_index);
                      return (
                        <tr
                          key={ver.id || ver.version_index}
                          data-version-index={ver.version_index}
                          style={{ borderBottom: "1px solid #eee" }}
                        >
                          <td style={{ padding: "6px" }}>
                            <strong>Version {ver.version_index}</strong>
                          </td>
                          <td style={{ padding: "6px" }}>{ver.value != null ? String(ver.value) : "—"}</td>
                          <td style={{ padding: "6px" }}>{ver.unit || "—"}</td>
                          <td style={{ padding: "6px" }}>{renderValidationState(ver)}</td>
                          <td style={{ padding: "6px" }}>{ver.source}</td>
                          <td style={{ padding: "6px" }}>{ver.actor || "—"}</td>
                          <td style={{ padding: "6px" }}>{ver.reason || "—"}</td>
                          <td style={{ padding: "6px" }}>
                            <time dateTime={ver.created_at}>{fmtDate(ver.created_at)}</time>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </section>

            <section
              className="value-compare-section"
              aria-label="Compare versions"
              style={{
                borderTop: "1px solid #e0e0e0",
                paddingTop: "16px"
              }}
            >
              <h3>Compare versions</h3>
              {chain.versions.length < 2 ? (
                <p>At least two versions are required to compare.</p>
              ) : (
                <>
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "12px",
                      marginBottom: "16px",
                      flexWrap: "wrap"
                    }}
                  >
                    <label>
                      Version A:{" "}
                      <select
                        aria-label="Select Version A"
                        value={versionA}
                        onChange={(e) => setVersionA(e.target.value)}
                      >
                        <option value="">Choose version A</option>
                        {chain.versions.map((v) => {
                          const val = target.kind === "parameter" ? v.id : String(v.version_index);
                          return (
                            <option key={val} value={val}>
                              Version {v.version_index} ({String(v.value)} {v.unit})
                            </option>
                          );
                        })}
                      </select>
                    </label>

                    <label>
                      Version B:{" "}
                      <select
                        aria-label="Select Version B"
                        value={versionB}
                        onChange={(e) => setVersionB(e.target.value)}
                      >
                        <option value="">Choose version B</option>
                        {chain.versions.map((v) => {
                          const val = target.kind === "parameter" ? v.id : String(v.version_index);
                          return (
                            <option key={val} value={val}>
                              Version {v.version_index} ({String(v.value)} {v.unit})
                            </option>
                          );
                        })}
                      </select>
                    </label>

                    <button
                      type="button"
                      disabled={compareLoading || !versionA || !versionB || versionA === versionB}
                      onClick={handleCompare}
                    >
                      {compareLoading ? "Comparing…" : "Compare"}
                    </button>
                  </div>

                  {compareError && (
                    <div className="runs-inline-error" role="alert">
                      <p>Comparison failed: {compareError}</p>
                    </div>
                  )}

                  {comparison && (
                    <section
                      className="value-comparison-details"
                      aria-label="Value comparison result"
                      style={{
                        backgroundColor: "#f9f9f9",
                        border: "1px solid #e0e0e0",
                        borderRadius: "6px",
                        padding: "16px"
                      }}
                    >
                      <h4>
                        Comparison: Version {comparison.version_a.version_index ?? "A"} vs Version{" "}
                        {comparison.version_b.version_index ?? "B"}
                      </h4>
                      <table
                        className="comparison-table"
                        style={{ width: "100%", borderCollapse: "collapse", fontSize: "14px" }}
                      >
                        <thead>
                          <tr style={{ borderBottom: "2px solid #ccc", textAlign: "left" }}>
                            <th style={{ padding: "8px", width: "20%" }}>Property</th>
                            <th style={{ padding: "8px", width: "40%" }}>
                              Version {comparison.version_a.version_index ?? "A"}
                            </th>
                            <th style={{ padding: "8px", width: "40%" }}>
                              Version {comparison.version_b.version_index ?? "B"}
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr style={{ borderBottom: "1px solid #eee" }}>
                            <td style={{ padding: "8px" }}>
                              <strong>Value & Unit</strong>
                            </td>
                            <td style={{ padding: "8px" }}>
                              {String(comparison.version_a.value)} {comparison.version_a.unit}
                            </td>
                            <td style={{ padding: "8px" }}>
                              {String(comparison.version_b.value)} {comparison.version_b.unit}
                            </td>
                          </tr>
                          <tr style={{ borderBottom: "1px solid #eee" }}>
                            <td style={{ padding: "8px" }}>
                              <strong>Source</strong>
                            </td>
                            <td style={{ padding: "8px" }}>{comparison.version_a.source}</td>
                            <td style={{ padding: "8px" }}>{comparison.version_b.source}</td>
                          </tr>
                          <tr style={{ borderBottom: "1px solid #eee" }}>
                            <td style={{ padding: "8px" }}>
                              <strong>Validation state</strong>
                            </td>
                            <td style={{ padding: "8px" }}>{renderValidationState(comparison.version_a)}</td>
                            <td style={{ padding: "8px" }}>{renderValidationState(comparison.version_b)}</td>
                          </tr>
                          <tr style={{ borderBottom: "1px solid #eee" }}>
                            <td style={{ padding: "8px" }}>
                              <strong>Conditions</strong>
                            </td>
                            <td style={{ padding: "8px" }}>
                              {Object.keys(comparison.version_a.conditions).length > 0
                                ? JSON.stringify(comparison.version_a.conditions)
                                : "—"}
                            </td>
                            <td style={{ padding: "8px" }}>
                              {Object.keys(comparison.version_b.conditions).length > 0
                                ? JSON.stringify(comparison.version_b.conditions)
                                : "—"}
                            </td>
                          </tr>
                          <tr style={{ borderBottom: "1px solid #eee" }}>
                            <td style={{ padding: "8px" }}>
                              <strong>Actor</strong>
                            </td>
                            <td style={{ padding: "8px" }}>{comparison.version_a.actor || "—"}</td>
                            <td style={{ padding: "8px" }}>{comparison.version_b.actor || "—"}</td>
                          </tr>
                          <tr style={{ borderBottom: "1px solid #eee" }}>
                            <td style={{ padding: "8px" }}>
                              <strong>Reason</strong>
                            </td>
                            <td style={{ padding: "8px" }}>{comparison.version_a.reason || "—"}</td>
                            <td style={{ padding: "8px" }}>{comparison.version_b.reason || "—"}</td>
                          </tr>
                          <tr style={{ borderBottom: "1px solid #eee" }}>
                            <td style={{ padding: "8px" }}>
                              <strong>Created at</strong>
                            </td>
                            <td style={{ padding: "8px" }}>{fmtDate(comparison.version_a.created_at)}</td>
                            <td style={{ padding: "8px" }}>{fmtDate(comparison.version_b.created_at)}</td>
                          </tr>
                          <tr style={{ borderBottom: "1px solid #eee" }}>
                            <td style={{ padding: "8px", verticalAlign: "top" }}>
                              <strong>Runs used</strong>
                            </td>
                            <td style={{ padding: "8px", verticalAlign: "top" }}>
                              {comparison.version_a.runs.length > 0 ? (
                                <ul style={{ margin: 0, paddingLeft: "20px" }}>
                                  {comparison.version_a.runs.map((r) => (
                                    <li key={r.id}>
                                      <strong>{r.run_label || r.id}</strong> <span>({r.status})</span>
                                    </li>
                                  ))}
                                </ul>
                              ) : (
                                <p style={{ margin: 0, color: "#666" }}>No runs used this version</p>
                              )}
                            </td>
                            <td style={{ padding: "8px", verticalAlign: "top" }}>
                              {comparison.version_b.runs.length > 0 ? (
                                <ul style={{ margin: 0, paddingLeft: "20px" }}>
                                  {comparison.version_b.runs.map((r) => (
                                    <li key={r.id}>
                                      <strong>{r.run_label || r.id}</strong> <span>({r.status})</span>
                                    </li>
                                  ))}
                                </ul>
                              ) : (
                                <p style={{ margin: 0, color: "#666" }}>No runs used this version</p>
                              )}
                            </td>
                          </tr>
                          <tr>
                            <td style={{ padding: "8px", verticalAlign: "top" }}>
                              <strong>Requirement checks</strong>
                            </td>
                            <td style={{ padding: "8px", verticalAlign: "top" }}>
                              {comparison.version_a.requirement_checks.length > 0 ? (
                                <ul style={{ margin: 0, paddingLeft: "20px" }}>
                                  {comparison.version_a.requirement_checks.map((chk) => (
                                    <li key={chk.id}>
                                      <strong>{chk.requirement_id}</strong>: {chk.outcome} ({chk.reason_code})
                                    </li>
                                  ))}
                                </ul>
                              ) : (
                                <p style={{ margin: 0, color: "#666" }}>No requirement checks used this version</p>
                              )}
                            </td>
                            <td style={{ padding: "8px", verticalAlign: "top" }}>
                              {comparison.version_b.requirement_checks.length > 0 ? (
                                <ul style={{ margin: 0, paddingLeft: "20px" }}>
                                  {comparison.version_b.requirement_checks.map((chk) => (
                                    <li key={chk.id}>
                                      <strong>{chk.requirement_id}</strong>: {chk.outcome} ({chk.reason_code})
                                    </li>
                                  ))}
                                </ul>
                              ) : (
                                <p style={{ margin: 0, color: "#666" }}>No requirement checks used this version</p>
                              )}
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </section>
                  )}
                </>
              )}
            </section>
          </div>
        )}
      </div>
    </div>
  );
}
