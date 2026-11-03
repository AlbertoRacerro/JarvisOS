import { exampleTooltip, isExampleWorkspace, type ExampleCandidate } from "./exampleWorkspace";

/** "Example" badge for example workspaces; renders nothing for ordinary projects. */
export default function ExampleBadge({ workspace }: Readonly<{ workspace: ExampleCandidate | null | undefined }>) {
  if (!workspace || !isExampleWorkspace(workspace)) return null;
  return <span className="example-badge" title={exampleTooltip(workspace)}>Example</span>;
}
