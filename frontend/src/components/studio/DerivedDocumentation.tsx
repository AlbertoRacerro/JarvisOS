import { useEffect, useRef } from "react";
import { MathTree } from "../process/BiologyModelLibrary";
import type { StudioDerived, StudioMathNode, StudioStep } from "../../api/studio";

const inner = (tree: StudioMathNode) => tree.children ?? [];
const equation = (target: StudioMathNode, expr: StudioMathNode): StudioMathNode => ({ tag: "math", children: [{ tag: "mrow", children: [...inner(target), { tag: "mo", text: "=" }, ...inner(expr)] }] });

type Selection = { selectedLine?: number | null; selectedRange?: { from: number; to: number } | null; onSelectLine?(line: number): void };
const linked = (line: number, { selectedLine, selectedRange }: Selection) => selectedRange ? line >= selectedRange.from && line <= selectedRange.to : selectedLine === line;
function Steps({ steps, ...selection }: { steps: StudioStep[] } & Selection) {
  const { onSelectLine } = selection;
  return <>{steps.map((step, index) => {
    if (step.kind === "equation") return <div className={linked(step.line, selection) ? "studio-equation is-linked" : "studio-equation"} key={index}>{onSelectLine ? <button type="button" className="studio-equation-link" aria-pressed={linked(step.line, selection)} onClick={() => onSelectLine(step.line)} title={`Show Python line ${step.line}`}><MathTree node={equation(step.target, step.expr)} /><span className="studio-eqno">({step.line})</span></button> : <MathTree node={equation(step.target, step.expr)} />}</div>;
    if (step.kind === "code") return <div className={linked(step.line, selection) ? "studio-unrendered is-linked" : "studio-unrendered"} key={index} title={`Not rendered: ${step.reason}`}><pre>{step.text}</pre><span className="studio-eqno">({step.line})</span></div>;
    if (step.form === "piecewise") return <div className={linked(step.line, selection) ? "studio-piecewise is-linked" : "studio-piecewise"} key={index}>{step.cases.map((item, n) => <div className="studio-equation" key={n}><MathTree node={equation(step.target, item.expr)} /><span>{item.condition ? "if" : "otherwise"}</span>{item.condition ? <MathTree node={item.condition} /> : null}</div>)}</div>;
    return <div className={linked(step.line, selection) ? "studio-branches is-linked" : "studio-branches"} key={index}>{step.branches.map((branch, n) => <div key={n}><div className="studio-equation"><span>{branch.label}</span>{branch.condition ? <MathTree node={branch.condition} /> : null}</div><div className="studio-branch-steps"><Steps steps={branch.steps} {...selection} /></div></div>)}</div>;
  })}</>;
}

export default function DerivedDocumentation({ derived, selectedLine, selectedRange, onSelectLine, entry = "evaluate" }: { derived: StudioDerived; entry?: "evaluate" | "build" } & Selection) {
  const root = useRef<HTMLDivElement>(null);
  const selection = { selectedLine, selectedRange, onSelectLine };
  useEffect(() => { if (selectedRange) root.current?.querySelector(".is-linked")?.scrollIntoView?.({ block: "nearest" }); }, [selectedRange?.from, selectedRange?.to]);
  if (derived.status === "syntax_error") return <p className="studio-warning">Python syntax error{derived.error?.line ? ` on line ${derived.error.line}` : ""}: {derived.error?.message}. No equations can be derived until it is fixed.</p>;
  return <div className="studio-derived" ref={root}>
    {derived.status === "entry_missing" ? <p className="studio-warning">The source does not define <code>{entry}</code>, so no equations can be derived.</p> : null}
    {derived.module_constants.length ? <article><strong>Constants</strong><Steps steps={derived.module_constants} {...selection} /></article> : null}
    {derived.functions.map((item) => <article key={item.name}><strong>{item.name}({item.args.join(", ")})</strong>{item.docstring ? <p className="studio-docstring">{item.docstring}</p> : null}{item.steps.length ? <Steps steps={item.steps} {...selection} /> : <p>No statements to derive.</p>}</article>)}
  </div>;
}
