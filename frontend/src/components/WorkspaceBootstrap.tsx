import { useState, type FormEvent } from "react";

import { createWorkspace, type Workspace } from "../api/client";
import Button from "./ui/Button";
import "./WorkspaceBootstrap.css";

type Props = Readonly<{ error?: string | null; onRetry?: () => void; onCreated: (workspace: Workspace) => void }>;
const ACTIVE_WORKSPACE_STORAGE_KEY = "jarvisos.active-workspace";

export function readStoredWorkspaceId(): string | null {
  try { return window.localStorage.getItem(ACTIVE_WORKSPACE_STORAGE_KEY); } catch { return null; }
}

export function writeStoredWorkspaceId(workspaceId: string | null): void {
  try { if (workspaceId) window.localStorage.setItem(ACTIVE_WORKSPACE_STORAGE_KEY, workspaceId); else window.localStorage.removeItem(ACTIVE_WORKSPACE_STORAGE_KEY); } catch { /* storage can be unavailable */ }
}

function slugFromName(name: string): string {
  const slug = name.trim().toLocaleLowerCase().normalize("NFKD").replace(/[\u0300-\u036f]/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);
  return slug || `workspace-${crypto.randomUUID().slice(0, 8)}`;
}

export default function WorkspaceBootstrap({ error, onRetry, onCreated }: Props) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || !name.trim() || !slug.trim()) return;
    setBusy(true); setFormError(null);
    try { onCreated(await createWorkspace({ name: name.trim(), slug: slug.trim(), description: description.trim() || null })); }
    catch (cause) { setFormError(cause instanceof Error ? cause.message : "Workspace could not be created."); }
    finally { setBusy(false); }
  };
  if (error) return <div className="workspace-bootstrap" aria-labelledby="workspace-bootstrap-title"><section className="workspace-bootstrap__card">
    <p className="workspace-bootstrap__eyebrow">JarvisOS · workspace access</p><h1 id="workspace-bootstrap-title">Workspaces could not be loaded</h1>
    <div className="workspace-bootstrap__error" role="alert"><strong>Workspace service unavailable</strong><p>{error}</p>{onRetry ? <Button type="button" variant="secondary" onClick={onRetry}>Retry</Button> : null}</div>
  </section></div>;
  return <div className="workspace-bootstrap" aria-labelledby="workspace-bootstrap-title"><section className="workspace-bootstrap__card">
    <p className="workspace-bootstrap__eyebrow">JarvisOS · first setup</p><h1 id="workspace-bootstrap-title">Create your first workspace</h1>
    <p>Start with a project workspace. You can add requirements, ideas, literature and planning records after it is created.</p>
    <form onSubmit={submit}><label>Workspace name<input required autoFocus maxLength={200} value={name} onChange={(event) => { const value = event.target.value; setName(value); if (!slugEdited) setSlug(slugFromName(value)); }} placeholder="e.g. My project" disabled={busy} /></label>
      <details><summary>Advanced workspace settings</summary><label>Workspace slug<input required maxLength={80} pattern="[a-z0-9][a-z0-9-]*" value={slug} onChange={(event) => { setSlugEdited(true); setSlug(event.target.value.toLocaleLowerCase()); }} disabled={busy} /></label><label>Description<textarea maxLength={2000} value={description} onChange={(event) => setDescription(event.target.value)} disabled={busy} placeholder="Optional project description" /></label></details>
      {formError ? <p className="workspace-bootstrap__error" role="alert">{formError}</p> : null}<Button type="submit" disabled={busy || !name.trim() || !slug.trim()}>{busy ? "Creating workspace…" : "Create workspace"}</Button>
    </form>
  </section></div>;
}
