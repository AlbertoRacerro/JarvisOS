// Owner words come from the registry `owner` projection; nothing is inferred from unit types.
const OWNER_WORDS: Record<string, { short: string; long: string }> = {
  jarvis_bio: { short: "Jarvis", long: "Jarvis · biology" },
  dwsim: { short: "DWSIM", long: "DWSIM" },
};

const humanise = (owner: string) => owner.replace(/_/g, " ").replace(/^./, (letter) => letter.toUpperCase());

/** Compact badge text, for the canvas and palette headings. */
export const ownerShort = (owner?: string | null): string =>
  owner ? OWNER_WORDS[owner]?.short ?? humanise(owner) : "Unknown owner";

/** Inspector wording, for example "Jarvis · biology". */
export const ownerLabel = (owner?: string | null): string =>
  owner ? OWNER_WORDS[owner]?.long ?? humanise(owner) : "Unknown owner";
