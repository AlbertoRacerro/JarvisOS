import { CHANNEL_LABELS, type Profile } from "./types";

type Props = { profiles: Profile[]; selectedDigest: string | null; onChoose: (digest: string) => void };

export function ProfileList({ profiles, selectedDigest, onChoose }: Props) {
  return (
    <section className="environment-card">
      <h3>Profiles</h3>
      <div className="environment-profile-list">
        {profiles.map((profile) => <button
          type="button"
          key={profile.digest}
          aria-pressed={selectedDigest === profile.digest}
          className={profile.integrity_error ? "is-integrity-error" : undefined}
          disabled={Boolean(profile.integrity_error)}
          onClick={() => onChoose(profile.digest)}
        >
          <strong>{profile.name}</strong>
          <span>{Object.keys(profile.channels).map((channel) => CHANNEL_LABELS[channel] ?? channel).join(", ")}
            {profile.label ? ` · ${profile.label}` : ""}</span>
          <small>{profile.resolution_minutes ?? "Irregular"} min · {String(profile.provenance.kind ?? "source unknown")}
            {profile.start ? ` · ${profile.start.slice(0, 10)} → ${profile.end.slice(0, 10)}` : ""}</small>
          {profile.integrity_error && <small role="alert">Integrity failure: {profile.integrity_error}</small>}
          <small>{profile.digest.slice(0, 19)}{profile.parent_digest ? ` · parent ${profile.parent_digest.slice(0, 19)}` : ""}</small>
        </button>)}
        {profiles.length === 0 && <p>No environment profiles yet.</p>}
      </div>
    </section>
  );
}
