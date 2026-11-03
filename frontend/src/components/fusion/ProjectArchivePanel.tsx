import { useCallback, useEffect, useState, type FormEvent } from "react";

import { archiveWorkspace, listWorkspaces, renameWorkspace, restoreWorkspace, type Workspace } from "../../api/client";
import { isExampleWorkspace } from "../../app/exampleWorkspace";

type Props = Readonly<{ onProjectsChanged(): void }>;

// Archive hides a project from the rail and every page; nothing is deleted and Restore brings it back.
// Rename changes only the shown name.
function ProjectArchivePanel({ onProjectsChanged }: Props) {
  const [projects, setProjects] = useState<Workspace[] | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<{ id: string; name: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => listWorkspaces({ includeArchived: true })
    .then(setProjects)
    .catch((cause: unknown) => setError(cause instanceof Error ? cause.message : "Projects could not be loaded.")), []);
  useEffect(() => { void load(); }, [load]);

  const run = async (projectId: string, action: () => Promise<unknown>) => {
    setBusyId(projectId); setError(null);
    try {
      await action();
      await load();
      onProjectsChanged();
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The project could not be changed.");
      return false;
    } finally {
      setBusyId(null);
    }
  };
  const submitRename = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!renaming || !renaming.name.trim()) return;
    const { id, name } = renaming;
    if (await run(id, () => renameWorkspace(id, name.trim()))) setRenaming(null);
  };

  return <section className="final-settings__projects" aria-labelledby="settings-projects-title">
    <h2 id="settings-projects-title">Projects</h2>
    <p>Archived projects disappear from the project list. Their data is kept, and you can restore them at any time.</p>
    {error ? <p role="alert">{error}</p> : null}
    {projects === null ? <p role="status">Loading projects…</p> : <ul className="final-settings__project-list">
      {projects.map((project) => <li key={project.id}>
        {renaming?.id === project.id
          ? <form className="final-settings__project-rename" onSubmit={(event) => void submitRename(event)}>
            <input aria-label={`New name for ${project.name}`} value={renaming.name} maxLength={200} autoFocus
              onChange={(event) => setRenaming({ id: project.id, name: event.target.value })} />
            <button type="submit" disabled={busyId !== null || !renaming.name.trim()}>{busyId === project.id ? "Saving…" : "Save"}</button>
            <button type="button" disabled={busyId !== null} onClick={() => setRenaming(null)}>Cancel</button>
          </form>
          : <>
            <span><strong>{project.name}</strong>{isExampleWorkspace(project) ? " · example" : ""}{project.status === "archived" ? " · archived" : ""}</span>
            <span className="final-settings__project-actions">
              <button type="button" disabled={busyId !== null} onClick={() => setRenaming({ id: project.id, name: project.name })}>Rename</button>
              <button type="button" disabled={busyId !== null}
                onClick={() => void run(project.id, () => project.status === "archived" ? restoreWorkspace(project.id) : archiveWorkspace(project.id))}>
                {busyId === project.id ? "Saving…" : project.status === "archived" ? "Restore" : "Archive"}
              </button>
            </span>
          </>}
      </li>)}
    </ul>}
  </section>;
}

export default ProjectArchivePanel;
