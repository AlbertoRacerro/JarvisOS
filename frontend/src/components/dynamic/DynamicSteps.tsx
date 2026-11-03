import type { ReactNode } from "react";
import { designTarget } from "../../app/designTargets";
import type { DynamicStep, DynamicTab } from "./dynamicLogic";

const SECTION_LABEL: Record<DynamicTab, string> = { scenario: "Scenarios", schedule: "Schedules", controller: "Controllers", runs: "Runs and results", plant: "Plant overview", compare: "Compare runs" };
const STEP_HINT: Record<DynamicStep["n"], string> = {
  1: "A scenario sets the conditions of a run: the time window, the environment profile (weather and light) and the starting state. Save it before running.",
  2: "Optional. A schedule changes things at set times (feed, dilution, harvest); a controller adjusts an input to hold a setpoint. Attach them to a scenario in step 1.",
  3: "Choose a saved scenario and start the run. A run never starts by itself; open View results on a run to read it.",
  4: "The plant overview shows the key results of finished runs; Compare runs overlays up to four of them."
};

/** Design › Dynamic: the numbered path through the panel's sections. Each step shows its state and opens its first section;
 *  `children` (a section's own action) follow the current step's hint. */
export default function DynamicSteps({ steps, tab, onOpen, children }: { steps: readonly DynamicStep[]; tab: DynamicTab | undefined; onOpen(tab: DynamicTab): void; children?: ReactNode }) {
  const current = steps.find((step) => tab !== undefined && step.sections.includes(tab));
  return <>
    <nav className="dyn-steps" aria-label="Dynamic simulation steps" {...designTarget("design.dynamic.steps")}>
      <ol>{steps.map((step) => <li key={step.n}>
        <button type="button" aria-current={step === current ? "step" : undefined} className={step.done ? "is-done" : undefined} onClick={() => onOpen(step.sections[0])}>
          <span className="dyn-steps__n" aria-hidden="true">{step.n}</span>
          <span><strong>{step.title}</strong><span className="dyn-sr"> · </span><small>{step.status}</small></span>
        </button></li>)}</ol>
      {current && current.sections.length > 1 ? <div className="dyn-steps__sections" role="group" aria-label={`${current.title} sections`}>
        {current.sections.map((key) => <button key={key} type="button" aria-pressed={tab === key} onClick={() => onOpen(key)}>{SECTION_LABEL[key]}</button>)}</div> : null}
    </nav>
    {current ? <div className="dyn-steps__hint"><p className="dyn-hint">{STEP_HINT[current.n]}</p>{children}</div> : null}
  </>;
}
