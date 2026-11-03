import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Camera, Eye, HandPointing, Microphone } from "@phosphor-icons/react";

import type { BubuIntegration } from "./BubuBubble";
import JarvisMessageText from "../ai/JarvisMessageText";
import { BubuPointer } from "./BubuPointer";
import { captureErrorText, captureTab, openTabStream, type TabStream } from "./captureTab";
import { latestReply, pickImages, pickedMention, withPicked } from "./bubuExtras";
import { useBubuConversation, type BubuExtras } from "./conversationStore";
import { collectPageContext, type UiTarget } from "./pageContext";
import { parsePointDirectives, type PointDirective } from "./pointerDirectives";
import { useDictation } from "./useDictation";
import { useUserPointer } from "./useUserPointer";

const replyText = (text: string): ReactNode => <JarvisMessageText text={parsePointDirectives(text).text} />;

/**
 * BUBU-2 wiring: the page is always described (DOM-first); screenshots only when asked; dictation fills the draft;
 * the user can point at an element; [[point:id]] in a reply shows where. Nothing here clicks or edits the page.
 */
export function useBubuIntegration(): { integration: BubuIntegration; overlay: ReactNode } {
  const conversation = useBubuConversation();
  const [insert, setInsert] = useState<{ text: string; nonce: number } | undefined>();
  const [picked, setPicked] = useState<UiTarget[]>([]);
  const [shot, setShot] = useState<string | null>(null);
  const [vision, setVision] = useState(false);
  const [mediaError, setMediaError] = useState<string | null>(null);
  const [points, setPoints] = useState<PointDirective[]>([]);
  const stream = useRef<TabStream | null>(null);
  const append = (text: string) => setInsert((current) => ({ text, nonce: (current?.nonce ?? 0) + 1 }));

  const dictation = useDictation({ onFinal: append });
  const pointer = useUserPointer((target) => { setPicked((current) => [target, ...current.filter((item) => item.id !== target.id)].slice(0, 5)); append(pickedMention(target)); });

  const stopVision = useCallback(() => { stream.current?.close(); stream.current = null; setVision(false); }, []);
  useEffect(() => stopVision, [stopVision]);

  const toggleVision = async () => {
    setMediaError(null);
    if (vision) { stopVision(); return; }
    try { stream.current = await openTabStream(); setVision(true); }
    catch (error) { setMediaError(captureErrorText(error)); }
  };
  const takeShot = async () => {
    setMediaError(null);
    try { setShot(await captureTab()); } catch (error) { setMediaError(captureErrorText(error)); }
  };

  // The newest reply's directives stay on screen until the next reply (or Sleep) replaces them.
  const reply = latestReply(conversation.turns);
  useEffect(() => { setPoints(reply ? parsePointDirectives(reply.text).points : []); }, [reply?.id, reply?.text]);

  const getExtras = async (): Promise<BubuExtras> => {
    let frame: string | null = null;
    if (vision && stream.current) {
      if (stream.current.live) { try { frame = await stream.current.grab(); } catch { frame = null; } }
      else { stopVision(); setMediaError("Vision stopped: the tab is no longer shared"); }
    }
    const extras = { ui_context: withPicked(collectPageContext(), picked), images: pickImages(shot, frame) };
    setShot(null); setPicked([]);
    return extras;
  };

  const onSleep = () => { stopVision(); dictation.stop(); pointer.cancel(); setShot(null); setPicked([]); setPoints([]); setMediaError(null); };

  const status = mediaError ? <span className="bubu-status bubu-status--error">{mediaError}</span>
    : dictation.error ? <span className="bubu-status bubu-status--error">{dictation.error}</span>
    : dictation.listening ? <span className="bubu-status">{dictation.interim || "Listening…"}</span>
    : pointer.active ? <span className="bubu-status">Click what you mean · Esc cancels</span>
    : shot || vision ? <span className="bubu-status">{[shot ? "Screenshot attached" : null, vision ? "Vision on" : null].filter(Boolean).join(" · ")}</span>
    : null;

  const tool = (label: string, title: string, active: boolean, onClick: () => void, icon: ReactNode, disabled = false) =>
    <button type="button" className={`bubu-bubble__btn${active ? " is-active" : ""}`} aria-label={label} aria-pressed={active} title={title} disabled={disabled} onClick={onClick}>{icon}</button>;
  const extraTools = <>
    {tool("Dictate", dictation.supported ? (dictation.listening ? "Stop dictation" : "Dictate") : "Dictation isn't available in this browser", dictation.listening, dictation.toggle, <Microphone size={16} aria-hidden="true" />, !dictation.supported)}
    {tool("Point at something", pointer.active ? "Cancel (Esc)" : "Point at something on the page", pointer.active, () => (pointer.active ? pointer.cancel() : pointer.start()), <HandPointing size={16} aria-hidden="true" />)}
    {tool("Screenshot", shot ? "Remove the screenshot" : "Attach a screenshot of this tab", !!shot, () => (shot ? setShot(null) : void takeShot()), <Camera size={16} aria-hidden="true" />)}
    {tool("Vision", vision ? "Vision on: each message carries what you see · click to stop" : "Let Jarvis see this tab until Sleep", vision, () => void toggleVision(), <Eye size={16} aria-hidden="true" />)}
  </>;

  const integration: BubuIntegration = { extraTools, getExtras, renderReply: replyText, draftInsert: insert, status, listening: dictation.listening, onSleep };
  return { integration, overlay: <BubuPointer points={points} onDismiss={(id) => setPoints((current) => current.filter((p) => p.id !== id))} /> };
}
