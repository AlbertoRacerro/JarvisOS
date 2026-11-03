import AppLink, { type Navigate } from "../../app/AppLink";

type WorkspaceGroup = "memory" | "development" | "coding";

type Props = Readonly<{
  group: WorkspaceGroup;
  active: string;
  navigate: Navigate;
}>;

const groups = {
  memory: {
    eyebrow: "Memory",
    title: "Project knowledge",
    tabs: [
      ["Project Basis", "/memory/project-basis", "project-basis"],
      ["Models", "/memory/models", "models"],
      ["Literature", "/memory/literature", "literature"]
    ]
  },
  development: {
    eyebrow: null,
    title: "Development",
    tabs: [
      ["Roadmap", "/development/roadmap/timeline", "roadmap"]
    ]
  },
  coding: {
    eyebrow: null,
    title: "Coding",
    tabs: [
      ["Repository", "/coding/repository", "repository"],
      ["Runtime", "/coding/runtime", "runtime"]
    ]
  }
} as const;

export default function FinalWorkspaceHeader({ group, active, navigate }: Props) {
  const config = groups[group];
  return (
    <header className="final-fusion__workspace-head">
      {config.eyebrow && <p className="eyebrow">{config.eyebrow}</p>}
      <h1>{config.title}</h1>
      <nav className="final-fusion__peer-tabs" aria-label={`${config.title} destinations`}>
        {config.tabs.map(([label, href, id]) => (
          <AppLink key={href} href={href} navigate={navigate} className={active === id ? "is-active" : ""} aria-current={active === id ? "page" : undefined}>
            {label}
          </AppLink>
        ))}
      </nav>
    </header>
  );
}
