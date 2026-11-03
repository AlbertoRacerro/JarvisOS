import type { ReactNode } from "react";

type TopBarProps = Readonly<{
  title: string;
  panelControls: ReactNode;
  appearanceControl: ReactNode;
}>;

function TopBar({ title, panelControls, appearanceControl }: TopBarProps) {
  return (
    <header className="shell-topbar">
      <div className="shell-topbar__title">
        <span>Current route</span>
        <strong>{title}</strong>
      </div>
      <div className="shell-topbar__controls">
        {panelControls}
        {appearanceControl}
      </div>
    </header>
  );
}

export default TopBar;
