export type BubuPoseName = "listening" | "thinking" | "clarifying" | "sending-issue" | "sleeping";

export type BubuPoseState = {
  /** After Sleep, until Bubu is clicked again. */
  asleep?: boolean;
  /** Dictation is active (the integrator turns this on). */
  dictating?: boolean;
  pending?: boolean;
  error?: string | null;
  /** Text of the latest turn when it is a finished Jarvis reply; otherwise null. */
  latestReply?: string | null;
};

/** Which mascot pose matches the real conversation state. */
export function bubuPose(state: BubuPoseState): BubuPoseName {
  if (state.asleep) return "sleeping";
  if (state.dictating) return "listening";
  if (state.pending) return "thinking";
  // "sending-issue" is reserved for an actual issue report; an error is shown as text in the bubble.
  if (state.error) return "listening";
  if (state.latestReply && /[?？]["')\]]*\s*$/.test(state.latestReply.trim())) return "clarifying";
  return "listening";
}

export const BUBU_POSES: readonly BubuPoseName[] = ["listening", "thinking", "clarifying", "sending-issue", "sleeping"];
export const bubuPoseSrc = (pose: BubuPoseName) => `/mascots/bubu/bubu-${pose}.webp`;
