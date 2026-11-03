import { useCallback, useEffect, useRef, useState } from "react";

export type DictationState = { supported: boolean; listening: boolean; interim: string; error: string | null };

export type DictationResult = { readonly isFinal: boolean; readonly [index: number]: { readonly transcript: string } };

type RecognitionErrorEvent = { error: string };
type RecognitionEvent = { resultIndex: number; results: ArrayLike<DictationResult> };
interface RecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((event: RecognitionEvent) => void) | null;
  onerror: ((event: RecognitionErrorEvent) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}
type RecognitionCtor = new () => RecognitionLike;

export function dictationErrorText(code: string): string {
  switch (code) {
    case "not-allowed":
    case "service-not-allowed":
      return "Microphone blocked";
    case "no-speech":
      return "No speech heard";
    case "audio-capture":
      return "No microphone";
    case "network":
      return "Dictation needs a connection";
    default:
      return "Dictation stopped";
  }
}

// Finals from fromIndex on are the new text to hand over; interim is the still-changing tail.
export function mergeResults(results: ArrayLike<DictationResult>, fromIndex = 0): { final: string; interim: string } {
  const finals: string[] = [];
  const interims: string[] = [];
  for (let i = 0; i < results.length; i++) {
    const result = results[i];
    const text = (result[0]?.transcript ?? "").trim();
    if (!text) continue;
    if (!result.isFinal) interims.push(text);
    else if (i >= fromIndex) finals.push(text);
  }
  return { final: finals.join(" "), interim: interims.join(" ") };
}

function recognitionCtor(): RecognitionCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

export function useDictation(options: { lang?: string; onFinal(text: string): void }): DictationState & { start(): void; stop(): void; toggle(): void } {
  const { lang } = options;
  const onFinalRef = useRef(options.onFinal);
  onFinalRef.current = options.onFinal;
  const recognitionRef = useRef<RecognitionLike | null>(null);
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const [error, setError] = useState<string | null>(null);
  const supported = recognitionCtor() !== null;

  const start = useCallback(() => {
    const Ctor = recognitionCtor();
    if (!Ctor || recognitionRef.current) return;
    const recognition = new Ctor();
    recognition.lang = lang || navigator.language;
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.onresult = (event) => {
      const merged = mergeResults(event.results, event.resultIndex);
      if (merged.final) onFinalRef.current(merged.final);
      if (recognitionRef.current === recognition) setInterim(merged.interim);
    };
    recognition.onerror = (event) => {
      if (recognitionRef.current !== recognition) return;
      recognitionRef.current = null;
      setListening(false);
      setInterim("");
      setError(dictationErrorText(event.error));
    };
    recognition.onend = () => {
      if (recognitionRef.current !== recognition) return;
      recognitionRef.current = null;
      setListening(false);
      setInterim("");
    };
    recognitionRef.current = recognition;
    try {
      recognition.start();
      setListening(true);
      setInterim("");
      setError(null);
    } catch {
      recognitionRef.current = null;
      setError(dictationErrorText(""));
    }
  }, [lang]);

  const stop = useCallback(() => {
    const recognition = recognitionRef.current;
    if (!recognition) return;
    recognitionRef.current = null;
    setListening(false);
    setInterim("");
    recognition.stop();
  }, []);

  const toggle = useCallback(() => {
    if (recognitionRef.current) stop();
    else start();
  }, [start, stop]);

  useEffect(() => () => {
    const recognition = recognitionRef.current;
    recognitionRef.current = null;
    recognition?.abort();
  }, []);

  return { supported, listening, interim, error, start, stop, toggle };
}
