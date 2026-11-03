import type { Workspace } from "../../api/client";
import { designTarget } from "../../app/designTargets";
import { caseStudyLabel, homeProject, isExampleWorkspace } from "../../app/exampleWorkspace";

type ProjectSwitcherProps = Readonly<{
  workspaces: readonly Workspace[];
  workspaceId: string | null;
  onChange(workspaceId: string): void;
}>;

// The app has one active project for every page, chosen here. Examples are case studies of the home project,
// opened from Design's version picker and never offered as projects; while one is open, the rail says so and leads back.
function ProjectSwitcher({ workspaces, workspaceId, onChange }: ProjectSwitcherProps) {
  const projects = workspaces.filter((item) => !isExampleWorkspace(item));
  const current = workspaces.find((item) => item.id === workspaceId);
  if (!current) return null;
  if (isExampleWorkspace(current)) {
    const home = homeProject(workspaces);
    return <div className="shell-project" aria-label="Active project">
      <span className="shell-project__label">Case study</span>
      <strong className="shell-project__name" title={current.name}>{caseStudyLabel(current)}</strong>
      {home ? <button type="button" className="shell-project__back" onClick={() => onChange(home.id)}>Back to {home.name}</button> : null}
    </div>;
  }
  return <div className="shell-project">
    <label htmlFor="shell-project-picker" className="shell-project__label">Project</label>
    <select id="shell-project-picker" value={current.id} onChange={(event) => onChange(event.target.value)} {...designTarget("design.project-picker")}>
      {projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
    </select>
  </div>;
}

export default ProjectSwitcher;
