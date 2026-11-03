import Settings from "../../pages/Settings";
import "../../styles/final-settings.css";
import ProjectArchivePanel from "./ProjectArchivePanel";

type SettingsSection = "appearance" | "ai" | "system";

type Props = Readonly<{
  section: SettingsSection;
  navigate(path: string): void;
  onProjectsChanged?(): void;
}>;

const SETTINGS_TABS: ReadonlyArray<Readonly<{ id: SettingsSection; label: string; href: string }>> = [
  { id: "appearance", label: "Appearance", href: "/settings/appearance" },
  { id: "ai", label: "AI", href: "/settings/ai" },
  { id: "system", label: "System", href: "/settings/system" }
];

function FinalSettingsSurface({ section, navigate, onProjectsChanged }: Props) {
  return (
    <section className={`final-settings final-settings--${section}`} aria-labelledby="final-settings-title">
      <header className="final-settings__header">
        <div>
          <h1 id="final-settings-title">Settings</h1>
        </div>
      </header>
      <nav className="final-settings__tabs" aria-label="Settings sections">
        {SETTINGS_TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            className={tab.id === section ? "final-settings__tab is-active" : "final-settings__tab"}
            aria-current={tab.id === section ? "page" : undefined}
            onClick={() => navigate(tab.href)}
          >
            {tab.label}
          </button>
        ))}
      </nav>
      {section === "system" && onProjectsChanged ? <ProjectArchivePanel onProjectsChanged={onProjectsChanged} /> : null}
      <Settings />
    </section>
  );
}

export default FinalSettingsSurface;
