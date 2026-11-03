import { useCallback, useEffect, useState } from "react";
import { patchDraft, type DraftProjection, type DraftOp } from "../../api/processDraft";
import { contentOf, dynamicSteps, suggestedTab, type DynamicTab, type LinkInfo } from "./dynamicLogic";
import { failureText, listDraftLinks, listEnvProfiles, listRunLinks, type EnvProfile } from "./dynamicApi";
import { type OnPatch } from "./fields";
import { NoticeShown, Refusal, SimulationNotice } from "./SimulationNotice";
import ScenarioEditor from "./ScenarioEditor";
import ScheduleEditor from "./ScheduleEditor";
import ControllerEditor from "./ControllerEditor";
import RunsPanel, { useDynamicRuns } from "./RunsPanel";
import Comparison from "./Comparison";
import PlantView from "./PlantView";
import DynamicSteps from "./DynamicSteps";
import "./DynamicPanel.css";

/** `guided` (Design › Dynamic): numbered steps replace the section tabs and, until the operator picks one, open the step still to do. */
export default function DynamicPanel({ workspaceId, draft, onUpdated, guided = false }: { workspaceId: string; draft: DraftProjection; onUpdated(next: DraftProjection): void; guided?: boolean }) {
  const [profiles, setProfiles] = useState<EnvProfile[]>([]);
  const [links, setLinks] = useState<LinkInfo[]>([]);
  const [runLinks, setRunLinks] = useState<Record<string, LinkInfo>>({});
  const [problem, setProblem] = useState<{ code: string | null; message: string } | null>(null);
  const [tab, setTab] = useState<DynamicTab | null>(guided ? null : "plant");
  const { runs, refresh, loaded, problem: runProblem } = useDynamicRuns(workspaceId, draft.draft_id, draft.revision);
  const refreshProfiles = useCallback(() => { void listEnvProfiles(workspaceId).then((next) => { setProfiles(next); setProblem(null); }).catch((cause) => setProblem(failureText(cause))); }, [workspaceId]);
  const reloadLinks = useCallback(() => { void Promise.all([listDraftLinks(workspaceId, draft.draft_id), listRunLinks(workspaceId, draft.draft_id)])
    .then(([nextLinks, nextRuns]) => { setLinks(nextLinks); setRunLinks(nextRuns); }).catch((cause) => setProblem(failureText(cause))); }, [workspaceId, draft.draft_id]);
  useEffect(() => { refreshProfiles(); reloadLinks(); }, [refreshProfiles, reloadLinks, draft.revision]);
  const onPatch: OnPatch = async (ops) => {
    try {
      const next = await patchDraft(workspaceId, draft.draft_id, draft.revision, ops as DraftOp[]);
      onUpdated(next); reloadLinks(); return { ok: true, seq: next.seq };
    } catch (cause) { return { ok: false, ...failureText(cause) }; }
  };
  const content = contentOf(draft.dynamic as Parameters<typeof contentOf>[0]);
  const suggested = tab === null ? suggestedTab(content, loaded ? runs : null) : undefined;
  // The suggestion is taken once: saving the first scenario must not move the operator off the editor.
  useEffect(() => { if (suggested) setTab(suggested); }, [suggested]);
  const shown = tab ?? suggested;
  const tabs = [["plant", "Plant overview"], ["scenario", "Scenarios"], ["schedule", "Schedules"], ["controller", "Controllers"], ["runs", "Runs and results"], ["compare", "Compare"]] as const;
  const refreshButton = <button type="button" onClick={refreshProfiles}>Refresh environment profiles</button>;
  return <section className="dyn-panel" aria-label="Dynamic Studio">
    {guided ? <header><SimulationNotice />
      <DynamicSteps steps={dynamicSteps(content, loaded ? runs : null)} tab={shown} onOpen={setTab}>{shown === "scenario" ? refreshButton : null}</DynamicSteps></header>
      : <><header><h3>Dynamic Studio</h3><SimulationNotice />
        <p>Create a saved scenario, schedule and controller for the Tier 1 process model. Changes create a draft revision. Start and re-run are explicit.</p>
        {refreshButton}</header>
      <div className="dyn-tabs" role="tablist" aria-label="Dynamic Studio sections">{tabs.map(([key, label]) => <button key={key} type="button" role="tab" aria-selected={shown === key} onClick={() => setTab(key)}>{label}</button>)}</div></>}
    {problem && <Refusal {...problem} />}
    {/* The header carries the simulation notice; the sections below leave theirs out so the screen shows it once. */}
    <NoticeShown.Provider value={true}>
      {shown === undefined ? <p role="status">Checking this draft's runs…</p> : null}
      {shown === "plant" && <PlantView key={`${workspaceId}:${draft.draft_id}`} workspaceId={workspaceId} draft={draft} runs={runs} />}
      {shown === "scenario" && <ScenarioEditor key={draft.draft_id} draft={draft} content={content} profiles={profiles} onPatch={onPatch} />}
      {shown === "schedule" && <ScheduleEditor key={draft.draft_id} draft={draft} content={content} links={links} onPatch={onPatch} />}
      {shown === "controller" && <ControllerEditor key={draft.draft_id} draft={draft} content={content} onPatch={onPatch} />}
      {shown === "runs" && <RunsPanel workspaceId={workspaceId} draftId={draft.draft_id} headSeq={draft.seq} scenarios={Object.keys(content.scenarios)} runs={runs} refresh={refresh} runLinks={runLinks} problem={runProblem} />}
      {shown === "compare" && <Comparison workspaceId={workspaceId} draftId={draft.draft_id} headSeq={draft.seq} runs={runs} />}
    </NoticeShown.Provider>
  </section>;
}
