import AppLink, { type Navigate } from "../../app/AppLink";
import InlineNotice from "../ui/InlineNotice";
import Surface from "../ui/Surface";

type MigrationLink = Readonly<{ href: string; label: string }>;

type MigrationPendingSurfaceProps = Readonly<{
  title: string;
  description: string;
  navigate: Navigate;
  links?: readonly MigrationLink[];
  unavailable?: boolean;
}>;

function MigrationPendingSurface({
  title,
  description,
  navigate,
  links = [],
  unavailable = false
}: MigrationPendingSurfaceProps) {
  return (
    <section className="shell-placeholder" aria-labelledby="shell-placeholder-title">
      <div className="page-header">
        <p className="eyebrow">{unavailable ? "Unavailable" : "Migration pending"}</p>
        <h1 id="shell-placeholder-title">{title}</h1>
      </div>
      <Surface as="div" className="shell-placeholder__surface">
        <InlineNotice tone={unavailable ? "neutral" : "info"}>{description}</InlineNotice>
        {links.length > 0 && (
          <nav className="shell-placeholder__links" aria-label={`${title} related routes`}>
            {links.map((link) => (
              <AppLink key={link.href} href={link.href} navigate={navigate} className="shell-text-link">
                {link.label}
              </AppLink>
            ))}
          </nav>
        )}
      </Surface>
    </section>
  );
}

export default MigrationPendingSurface;
