import { createContext, useContext } from "react";
import { NOTICE } from "./dynamicLogic";

/** True inside a panel whose header already shows the notice: its sections leave theirs out, so a screen carries it once. */
export const NoticeShown = createContext(false);

/** Spec 173 decision 16: the honest limits appear on the editor, results and comparison. */
export function SimulationNotice() {
  return useContext(NoticeShown) ? null : <p className="dyn-notice" role="note">{NOTICE}</p>;
}

/** A refusal shows the backend code and message and changes nothing. */
export function Refusal({ code, message }: { code: string | null | undefined; message: string }) {
  return <p className="dyn-error" role="alert" data-refusal-code={code ?? ""}>{code ? <strong>{code}: </strong> : null}{message}</p>;
}
