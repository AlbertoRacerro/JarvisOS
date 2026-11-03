import { useCallback, useEffect, useMemo, useState } from "react";
import AppLink from "../app/AppLink";
import type { PrimaryStageProps } from "./registry";
import InlineNotice from "../components/ui/InlineNotice";
import Surface from "../components/ui/Surface";
import "./engineering-studies.css";
import {
  createStudy,
  createEnvelope,
  createEscalation,
  getEnvelope,
  listCapabilities,
  listEvaluators,
  listStudies,
  listStudyRuns,
  type Envelope,
  type EscalationRun,
  type StudyDefinition,
  type StudyRun,
} from "../api/engineeringStudies";
import type { CapabilityRead, EvaluatorRead } from "../api/generated/engineering";

import NumericInput from "../components/ui/NumericInput";
const QUALIFICATIONS = new Set(["unqualified", "candidate", "calibrated", "benchmarked", "qualified"]);
const reasonText = (state: string, reason: string | null | undefined) =>
  `${state}${reason ? ` · ${reason}` : " · reason unavailable"}`;
const asJson = (value: unknown) => JSON.stringify(value, null, 2);
const quantity = (value: number, unit: string) => ({ value, unit });
function pointQualification(evaluation: StudyRun["points"][number]["evaluation"]): string {
  if (!evaluation) return "Unknown (no evaluation returned)";
  const validity = evaluation.validity;
  const status =
    validity && typeof validity === "object" && "qualification_status" in validity
      ? (validity as { qualification_status: unknown }).qualification_status
      : evaluation.qualification_status;
  if (status === "unqualified") return "Unqualified";
  return typeof status === "string" && QUALIFICATIONS.has(status)
    ? status
    : "Unknown (server value preserved in details)";
}

function EngineeringStudiesStage({ workspaceId, navigate }: PrimaryStageProps) {
  const [evaluators, setEvaluators] = useState<EvaluatorRead[]>([]);
  const [capabilities, setCapabilities] = useState<CapabilityRead[]>([]);
  const [studies, setStudies] = useState<string[]>([]);
  const [selectedStudy, setSelectedStudy] = useState("");
  const [runs, setRuns] = useState<StudyRun[]>([]);
  const [selectedDigest, setSelectedDigest] = useState("");
  const [compareDigest, setCompareDigest] = useState("");
  const [envelope, setEnvelope] = useState<Envelope | null>(null);
  const [escalation, setEscalation] = useState<EscalationRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [evaluatorId, setEvaluatorId] = useState("");
  const [studyId, setStudyId] = useState("operator-study");
  const [subjectId, setSubjectId] = useState("study-subject");
  const [variableName, setVariableName] = useState("peak_par");
  const [lower, setLower] = useState("1200");
  const [upper, setUpper] = useState("1800");
  const [step, setStep] = useState("300");
  const [unit, setUnit] = useState("umol/(m**2*s)");
  const [fixedInputs, setFixedInputs] = useState("[]");
  const [objective, setObjective] = useState("volumetric_productivity");
  const [objectiveSense, setObjectiveSense] = useState<"maximize" | "minimize">("maximize");
  const [constraintOutput, setConstraintOutput] = useState("");
  const [constraintOperator, setConstraintOperator] = useState<"le" | "ge" | "eq">("le");
  const [constraintBound, setConstraintBound] = useState("");
  const [constraintUnit, setConstraintUnit] = useState("1");
  const [escalationEvaluatorIds, setEscalationEvaluatorIds] = useState<string[]>([]);
  const [seed, setSeed] = useState("108");
  const [budget, setBudget] = useState("3");
  const [method, setMethod] = useState<StudyDefinition["method"]>("latin_hypercube");

  const refresh = useCallback(async () => {
    if (!workspaceId) return;
    setLoading(true);
    setError(null);
    try {
      const [foundEvaluators, foundCapabilities, foundStudies] = await Promise.all([
        listEvaluators(workspaceId),
        listCapabilities(workspaceId),
        listStudies(workspaceId),
      ]);
      setEvaluators(foundEvaluators);
      setCapabilities(foundCapabilities);
      setStudies(foundStudies);
      const firstAvailable = foundEvaluators.find((item) => item.state === "available")?.evaluator_id ?? "";
      setEvaluatorId((current) =>
        foundEvaluators.some((item) => item.evaluator_id === current && item.state === "available")
          ? current
          : firstAvailable,
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Engineering capabilities could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [workspaceId]);
  useEffect(() => {
    void refresh();
  }, [refresh]);

  const loadRuns = useCallback(
    async (id: string) => {
      if (!workspaceId || !id) {
        setRuns([]);
        return;
      }
      try {
        setRuns(await listStudyRuns(workspaceId, id));
        setError(null);
      } catch (cause) {
        setRuns([]);
        setError(cause instanceof Error ? cause.message : "Study runs could not be loaded.");
      }
    },
    [workspaceId],
  );
  useEffect(() => {
    void loadRuns(selectedStudy);
  }, [loadRuns, selectedStudy]);
  const selectedRun = runs.find((run) => run.content_digest === selectedDigest) ?? null;
  const comparisonRun = runs.find((run) => run.content_digest === compareDigest) ?? null;
  const availableEvaluators = useMemo(() => evaluators.filter((item) => item.state === "available"), [evaluators]);
  const selectedEvaluator = evaluators.find((item) => item.evaluator_id === evaluatorId);
  const canRun = Boolean(
    workspaceId &&
      selectedEvaluator?.state === "available" &&
      !running &&
      budget !== "" &&
      Number(budget) > 0 &&
      Number(budget) <= 16,
  );

  const runStudy = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!workspaceId || !canRun) return;
    setRunning(true);
    setError(null);
    setEnvelope(null);
    setEscalation(null);
    try {
      const fixed = JSON.parse(fixedInputs) as StudyDefinition["fixed_inputs"];
      if (!Array.isArray(fixed)) throw new Error("Fixed inputs must be a JSON array.");
      const id = studyId.trim();
      const reference = (objectType: string, objectId: string) => ({
        authority_owner: "operator",
        object_type: objectType,
        object_id: objectId,
        workspace_id: workspaceId,
        revision: "operator",
      });
      const definition: StudyDefinition = {
        study_ref: reference("study", id),
        evaluator_id: evaluatorId,
        subject_ref: reference("dynamic_model", subjectId.trim()),
        method,
        variables: [
          {
            name: variableName.trim(),
            domain: {
              variable: variableName.trim(),
              lower: quantity(Number(lower), unit.trim()),
              upper: quantity(Number(upper), unit.trim()),
            },
            ...(method === "grid" ? { step: quantity(Number(step), unit.trim()) } : {}),
          },
        ],
        fixed_inputs: fixed,
        objectives: objective.trim() ? [{ output: objective.trim(), sense: objectiveSense }] : [],
        constraints:
          constraintOutput.trim() && constraintBound !== ""
            ? [
                {
                  output: constraintOutput.trim(),
                  operator: constraintOperator,
                  bound: quantity(Number(constraintBound), constraintUnit.trim()),
                },
              ]
            : [],
        seed: Number(seed),
        budget: Number(budget),
        sample_count: Number(budget),
        deadline_per_point_s: 90,
      };
      const run = await createStudy(workspaceId, definition);
      setStudies((current) => (current.includes(id) ? current : [...current, id].sort()));
      setSelectedStudy(id);
      setSelectedDigest(run.content_digest);
      setRuns((current) => [run, ...current.filter((item) => item.content_digest !== run.content_digest)]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Study request failed.");
    } finally {
      setRunning(false);
    }
  };
  const envelopeForBest = async () => {
    if (!workspaceId || !selectedRun || selectedRun.best_point_index === null) return;
    try {
      setError(null);
      setEnvelope(
        await createEnvelope(workspaceId, selectedStudy, selectedRun.content_digest, selectedRun.best_point_index),
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Envelope request failed.");
    }
  };
  const escalateBest = async () => {
    if (!workspaceId || !selectedRun || selectedRun.best_point_index === null) return;
    try {
      setError(null);
      const result = await createEscalation(
        workspaceId,
        selectedStudy,
        selectedRun.content_digest,
        escalationEvaluatorIds,
        selectedRun.best_point_index,
      );
      setEscalation(result);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Escalation request failed.");
    }
  };
  const openEnvelope = async (digest: string) => {
    if (!workspaceId || !selectedRun) return;
    try {
      setEnvelope(await getEnvelope(workspaceId, selectedStudy, selectedRun.content_digest, digest));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Envelope could not be read.");
    }
  };

  if (!workspaceId)
    return (
      <section className="shell-placeholder">
        <h1>Engineering studies</h1>
        <InlineNotice tone="neutral">Select a workspace to inspect engineering capabilities.</InlineNotice>
      </section>
    );
  return (
    <section
      className="shell-placeholder engineering-studies"
      data-testid="engineering-studies-surface"
      aria-labelledby="engineering-studies-title"
    >
      <div className="page-header">
        <div>
          <p className="eyebrow">Design · Engineering</p>
          <h1 id="engineering-studies-title">Studies & capability</h1>
          <p className="panel-subtitle">
            Configure a server evaluated study, then inspect its returned evidence and limits.
          </p>
        </div>
        <button type="button" className="secondary-button" onClick={() => void refresh()} disabled={loading}>
          Refresh availability
        </button>
      </div>
      {error && <InlineNotice tone="danger">{error}</InlineNotice>}
      {loading ? (
        <Surface as="div">
          <p role="status">Loading evaluator availability…</p>
        </Surface>
      ) : (
        <>
          <Surface as="section" className="engineering-studies__availability">
            <h2>Evaluator availability</h2>
            <p>Only evaluators reported as available by the backend can run a study.</p>
            {evaluators.length === 0 ? (
              <p>No evaluators are registered.</p>
            ) : (
              <ul>
                {evaluators.map((item) => (
                  <li key={item.evaluator_id} data-evaluator-id={item.evaluator_id}>
                    <strong>{item.evaluator_id}</strong>{" "}
                    <span className={`engineering-state engineering-state--${item.state}`}>
                      {item.state === "available"
                        ? "Available"
                        : item.state === "not_configured"
                          ? "Not configured"
                          : "Unavailable"}
                    </span>
                    <span>{item.reason_code ? ` · ${item.reason_code}` : ""}</span>
                    <details>
                      <summary>Technical details</summary>
                      <p>
                        Backend {item.backend_name ?? "unknown"} {item.backend_version ?? "version unavailable"} ·
                        fidelity {item.fidelity ?? "unavailable"} · kind {item.backend_kind ?? "unavailable"}
                      </p>
                      <p>
                        Raw state: {item.state} · reason code: {item.reason_code ?? "none returned"}
                      </p>
                    </details>
                  </li>
                ))}
              </ul>
            )}
            <h3>Capabilities</h3>
            {capabilities.length ? (
              <ul>
                {capabilities.map((capability) => (
                  <li key={capability.capability_id} data-capability-id={capability.capability_id}>
                    <strong>{capability.capability_id}</strong> · {capability.state} ·{" "}
                    {capability.reason_code ?? "reason unavailable"}
                  </li>
                ))}
              </ul>
            ) : (
              <p>No capability records returned.</p>
            )}
          </Surface>
          <Surface as="section" className="engineering-studies__configure">
            <h2>Configure and run</h2>
            <form onSubmit={(event) => void runStudy(event)}>
              <label>
                Evaluator
                <select
                  aria-label="Evaluator"
                  value={evaluatorId}
                  onChange={(event) => setEvaluatorId(event.target.value)}
                >
                  <option value="">Choose available evaluator</option>
                  {evaluators.map((item) => (
                    <option key={item.evaluator_id} value={item.evaluator_id} disabled={item.state !== "available"}>
                      {item.evaluator_id} · {item.state}
                      {item.reason_code ? ` (${item.reason_code})` : ""}
                    </option>
                  ))}
                </select>
              </label>
              {selectedEvaluator && selectedEvaluator.state !== "available" && (
                <InlineNotice tone="warning">
                  Cannot run: evaluator {selectedEvaluator.evaluator_id} is{" "}
                  {reasonText(selectedEvaluator.state, selectedEvaluator.reason_code)}.
                </InlineNotice>
              )}
              <div className="engineering-studies__fields">
                <label>
                  Study id
                  <input value={studyId} onChange={(event) => setStudyId(event.target.value)} required />
                </label>
                <label>
                  Subject id
                  <input value={subjectId} onChange={(event) => setSubjectId(event.target.value)} required />
                </label>
                <label>
                  Method
                  <select
                    value={method}
                    onChange={(event) => setMethod(event.target.value as StudyDefinition["method"])}
                  >
                    <option value="latin_hypercube">Latin hypercube</option>
                    <option value="grid">Grid</option>
                    <option value="monte_carlo">Monte Carlo</option>
                    <option value="single_objective_opt">Single objective optimization</option>
                  </select>
                </label>
                <label>
                  Variable
                  <input value={variableName} onChange={(event) => setVariableName(event.target.value)} required />
                </label>
                <label>
                  Lower bound
                  <NumericInput value={lower} onValueChange={(text) => setLower(text)} required />
                </label>
                <label>
                  Upper bound
                  <NumericInput value={upper} onValueChange={(text) => setUpper(text)} required />
                </label>
                <label>
                  Grid step
                  <NumericInput
                    value={step}
                    onValueChange={(text) => setStep(text)}
                    required={method === "grid"}
                    disabled={method !== "grid"}
                  />
                </label>
                <label>
                  Variable unit
                  <input value={unit} onChange={(event) => setUnit(event.target.value)} required />
                </label>
                <label>
                  Objective output
                  <input value={objective} onChange={(event) => setObjective(event.target.value)} />
                </label>
                <label>
                  Objective sense
                  <select
                    value={objectiveSense}
                    onChange={(event) => setObjectiveSense(event.target.value as "maximize" | "minimize")}
                  >
                    <option value="maximize">Maximize</option>
                    <option value="minimize">Minimize</option>
                  </select>
                </label>
                <label>
                  Constraint output (optional)
                  <input value={constraintOutput} onChange={(event) => setConstraintOutput(event.target.value)} />
                </label>
                <label>
                  Constraint operator
                  <select
                    value={constraintOperator}
                    onChange={(event) => setConstraintOperator(event.target.value as "le" | "ge" | "eq")}
                  >
                    <option value="le">≤</option>
                    <option value="ge">≥</option>
                    <option value="eq">=</option>
                  </select>
                </label>
                <label>
                  Constraint bound
                  <NumericInput
                    value={constraintBound}
                    onValueChange={(text) => setConstraintBound(text)}
                  />
                </label>
                <label>
                  Constraint unit
                  <input value={constraintUnit} onChange={(event) => setConstraintUnit(event.target.value)} />
                </label>
                <label>
                  Seed
                  <NumericInput value={seed} onValueChange={(text) => setSeed(text)} required />
                </label>
                <label>
                  Budget (maximum 16)
                  <NumericInput
                    min={1}
                    max={16}
                    value={budget}
                    onValueChange={(text) => setBudget(text)}
                    required
                  />
                </label>
              </div>
              <label>
                Fixed inputs (JSON array of named quantities)
                <textarea
                  value={fixedInputs}
                  onChange={(event) => setFixedInputs(event.target.value)}
                  rows={5}
                  spellCheck={false}
                />
              </label>
              <p>
                Runs and results are owned by the engineering backend. A failed request can be corrected and retried.
              </p>
              <button type="submit" disabled={!canRun}>
                {running ? "Study running…" : "Run study"}
              </button>
            </form>
          </Surface>
          <Surface as="section" className="engineering-studies__runs">
            <h2>Study runs</h2>
            <div className="engineering-studies__fields">
              <label>
                Study
                <select
                  value={selectedStudy}
                  onChange={(event) => {
                    setSelectedStudy(event.target.value);
                    setSelectedDigest("");
                    setEnvelope(null);
                    setEscalation(null);
                  }}
                >
                  <option value="">Select study</option>
                  {studies.map((id) => (
                    <option key={id} value={id}>
                      {id}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="secondary-button"
                onClick={() => void loadRuns(selectedStudy)}
                disabled={!selectedStudy}
              >
                Refresh runs
              </button>
            </div>
            {!selectedStudy ? (
              <p>Select a study, or run one above.</p>
            ) : runs.length === 0 ? (
              <p>No runs have been recorded for this study.</p>
            ) : (
              <div className="engineering-studies__run-list">
                {runs.map((run) => (
                  <button
                    key={run.content_digest}
                    type="button"
                    className="secondary-button"
                    aria-pressed={selectedDigest === run.content_digest}
                    onClick={() => {
                      setSelectedDigest(run.content_digest);
                      setEnvelope(null);
                      setEscalation(null);
                    }}
                  >
                    <strong>{run.status}</strong> ·{" "}
                    {run.qualification_status === "unqualified"
                      ? "Unqualified"
                      : QUALIFICATIONS.has(String(run.qualification_status))
                        ? String(run.qualification_status)
                        : "Qualification unknown"}{" "}
                    · {run.points.length} points · {run.content_digest.slice(0, 20)}…
                  </button>
                ))}
              </div>
            )}
          </Surface>
          {selectedRun && (
            <Surface as="section" className="engineering-studies__inspection">
              <div className="engineering-studies__heading">
                <div>
                  <h2>Run inspection</h2>
                  <p>
                    <strong>
                      {selectedRun.qualification_status === "unqualified"
                        ? "UNQUALIFIED"
                        : QUALIFICATIONS.has(String(selectedRun.qualification_status))
                          ? String(selectedRun.qualification_status).toUpperCase()
                          : "QUALIFICATION UNKNOWN"}
                    </strong>{" "}
                    · {selectedRun.status} · fidelity {selectedRun.fidelity ?? "unavailable"}
                  </p>
                </div>
                <label>
                  Compare with
                  <select value={compareDigest} onChange={(event) => setCompareDigest(event.target.value)}>
                    <option value="">Choose another run</option>
                    {runs
                      .filter((run) => run.content_digest !== selectedRun.content_digest)
                      .map((run) => (
                        <option key={run.content_digest} value={run.content_digest}>
                          {run.content_digest.slice(0, 24)}…
                        </option>
                      ))}
                  </select>
                </label>
              </div>
              <p>
                {selectedRun.availability_state}
                {selectedRun.availability_reason ? ` · ${selectedRun.availability_reason}` : ""}
              </p>
              <p>
                Feasible {selectedRun.feasible_count} · failed {selectedRun.failed_count} · infeasible{" "}
                {selectedRun.infeasible_count} · best point {selectedRun.best_point_index ?? "none"} · Pareto{" "}
                {selectedRun.pareto_point_indices.join(", ") || "none"}
              </p>
              <details>
                <summary>Definition and evidence references</summary>
                <p>
                  Definition digest: <code>{selectedRun.definition_digest}</code>
                </p>
                <p>
                  Content digest: <code>{selectedRun.content_digest}</code>
                </p>
                <pre>
                  {asJson(
                    selectedRun.points.map((point) => ({
                      index: point.index,
                      request_ref: point.request_ref,
                      evidence_refs: point.evaluation?.evidence_refs ?? [],
                    })),
                  )}
                </pre>
              </details>
              {selectedRun.points.length === 0 ? (
                <InlineNotice tone="warning">
                  The server returned no points for this run. Qualification is not inferred.
                </InlineNotice>
              ) : (
                <div className="engineering-studies__points">
                  {selectedRun.points.map((point) => (
                    <article key={point.index}>
                      <h3>
                        Point {point.index}
                        {point.index === selectedRun.best_point_index ? " · best" : ""}
                        {point.pareto_optimal ? " · Pareto" : ""}
                      </h3>
                      <p>
                        {point.status} · {point.feasible ? "Feasible" : "Infeasible"}
                      </p>
                      <dl>
                        <div>
                          <dt>Inputs</dt>
                          <dd>
                            {point.inputs
                              .map((item) => `${item.name}: ${item.value.value} ${item.value.unit}`)
                              .join(" · ") || "none returned"}
                          </dd>
                        </div>
                        <div>
                          <dt>Outputs</dt>
                          <dd>
                            {point.evaluation?.outputs
                              .map((item) => `${item.name}: ${item.value.value} ${item.value.unit}`)
                              .join(" · ") || "none returned"}
                          </dd>
                        </div>
                        <div>
                          <dt>Qualification</dt>
                          <dd>{pointQualification(point.evaluation)}</dd>
                        </div>
                      </dl>
                      {point.failure !== null && point.failure !== undefined && (
                        <details>
                          <summary>Failure detail</summary>
                          <pre>{asJson(point.failure)}</pre>
                        </details>
                      )}
                      <details>
                        <summary>Point evidence</summary>
                        <pre>{asJson(point.evaluation)}</pre>
                      </details>
                    </article>
                  ))}
                </div>
              )}
              {comparisonRun && (
                <div className="engineering-studies__compare">
                  <h3>Run comparison</h3>
                  <div>
                    <article>
                      <h4>Selected run</h4>
                      <p>{selectedRun.content_digest}</p>
                      <p>
                        {String(selectedRun.qualification_status)} · {selectedRun.fidelity ?? "fidelity unavailable"} ·{" "}
                        {selectedRun.feasible_count} feasible
                      </p>
                    </article>
                    <article>
                      <h4>Compared run</h4>
                      <p>{comparisonRun.content_digest}</p>
                      <p>
                        {String(comparisonRun.qualification_status)} ·{" "}
                        {comparisonRun.fidelity ?? "fidelity unavailable"} · {comparisonRun.feasible_count} feasible
                      </p>
                    </article>
                  </div>
                  <details>
                    <summary>Point-by-point server comparison</summary>
                    {Array.from(
                      new Set([
                        ...selectedRun.points.map((point) => point.index),
                        ...comparisonRun.points.map((point) => point.index),
                      ]),
                    )
                      .sort((left, right) => left - right)
                      .map((index) => (
                        <div className="engineering-studies__compare-points" key={index}>
                          <article>
                            <h4>Selected · point {index}</h4>
                            <pre>
                              {asJson(selectedRun.points.find((point) => point.index === index) ?? "No point returned")}
                            </pre>
                          </article>
                          <article>
                            <h4>Compared · point {index}</h4>
                            <pre>
                              {asJson(
                                comparisonRun.points.find((point) => point.index === index) ?? "No point returned",
                              )}
                            </pre>
                          </article>
                        </div>
                      ))}
                  </details>
                </div>
              )}
              <div className="engineering-studies__fields">
                <label>
                  Escalation evaluators (optional, higher fidelity)
                  <select
                    multiple
                    value={escalationEvaluatorIds}
                    onChange={(event) =>
                      setEscalationEvaluatorIds(Array.from(event.target.selectedOptions, (option) => option.value))
                    }
                  >
                    {availableEvaluators
                      .filter((item) => item.evaluator_id !== selectedRun.evaluator_id)
                      .map((item) => (
                        <option key={item.evaluator_id} value={item.evaluator_id}>
                          {item.evaluator_id} · {item.fidelity ?? "fidelity unavailable"}
                        </option>
                      ))}
                  </select>
                </label>
              </div>
              <div className="button-row">
                <button
                  type="button"
                  onClick={() => void envelopeForBest()}
                  disabled={selectedRun.best_point_index === null || selectedRun.feasible_count === 0}
                >
                  Create envelope for best feasible point
                </button>
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => void escalateBest()}
                  disabled={selectedRun.best_point_index === null}
                >
                  Request escalation for best point
                </button>
                <AppLink href="/design/bluecad" navigate={navigate} className="shell-text-link">
                  Open BlueCad candidates
                </AppLink>
              </div>
              {envelope && (
                <details open>
                  <summary>Process design envelope · {String(envelope.qualification_status)}</summary>
                  <p>
                    Envelope {envelope.envelope_digest} · point {envelope.selected_point_index} ·{" "}
                    {String(envelope.qualification_status)}
                  </p>
                  <pre>{asJson(envelope)}</pre>
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => void openEnvelope(envelope.envelope_digest)}
                  >
                    Reload envelope from server
                  </button>
                </details>
              )}
              {escalation && (
                <div>
                  <h3>Escalation results</h3>
                  {escalation.points.length === 0 ? (
                    <p>The server returned no triggered points.</p>
                  ) : (
                    escalation.points.map((point, index) => (
                      <article key={`${point.study_point_index}-${index}`}>
                        <strong>
                          Point {point.study_point_index} · {point.status}
                        </strong>
                        {point.status === "escalation_unavailable" && (
                          <p>Unavailable: {point.status_reason ?? "reason not provided"}</p>
                        )}
                        <details>
                          <summary>Technical evidence</summary>
                          <pre>{asJson(point)}</pre>
                        </details>
                      </article>
                    ))
                  )}
                </div>
              )}
            </Surface>
          )}
        </>
      )}
    </section>
  );
}

export default EngineeringStudiesStage;
