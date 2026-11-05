import { useEffect, useRef, useState } from "react";
import { controlLlama, readLlamaStatus, type LlamaAction, type LlamaStatus } from "../../api/llamaRuntime";

const busyStatus = (s: LlamaStatus | null) => s?.reason_code === "LLAMACPP_LOADING" || s?.reason_code === "LLAMACPP_RECOVERING";
export function buttons(s: LlamaStatus | null, busy: boolean) {
  return {
    start: !!s?.configured && !s.runtime_reachable && !s.spawned_by_jarvis && !busy && !busyStatus(s),
    stop: !!s?.spawned_by_jarvis && !busy && !busyStatus(s),
    restart: !!s?.spawned_by_jarvis && !busy && !busyStatus(s),
  };
}
const show = (value: string | number | null) => value ?? "Not reported";
const yes = (value: boolean) => value ? "Yes" : "No";

export default function LocalModelPanel() {
  const [status, setStatus] = useState<LlamaStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState<LlamaAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const actionLock = useRef(false);
  const generation = useRef(0);
  const polls = useRef(0);
  const refresh = async () => {
    const current = ++generation.current;
    setLoading(true);
    try { const next = await readLlamaStatus(); if (current === generation.current) { setStatus(next); setError(null); } }
    catch (cause) { if (current === generation.current) setError(cause instanceof Error ? cause.message : "Status request failed"); }
    finally { if (current === generation.current) setLoading(false); }
  };
  useEffect(() => { void refresh(); return () => { generation.current++; }; }, []);
  useEffect(() => {
    if (error || (!pending && !busyStatus(status)) || polls.current >= 20) { if (!pending && !busyStatus(status)) polls.current = 0; return; }
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible" && polls.current < 20) { polls.current++; void refresh(); }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [pending, status?.reason_code, error]);
  const act = async (action: LlamaAction) => {
    if (actionLock.current || !buttons(status, loading || !!pending)[action]) return;
    if (action !== "start" && !window.confirm(`${action === "stop" ? "Stop" : "Restart"} the local model?`)) return;
    actionLock.current = true; setPending(action); setError(null); ++generation.current;
    try {
      const next = await controlLlama(action); ++generation.current;
      if (action !== "stop" && next.reason_code && !["LLAMACPP_LOADING", "LLAMACPP_RECOVERING"].includes(next.reason_code)) {
        setError(`${next.reason_code}: ${next.message}`);
      } else setStatus(next);
    }
    catch (cause) { ++generation.current; setError(cause instanceof Error ? cause.message : "Action failed"); }
    finally { actionLock.current = false; setPending(null); setLoading(false); }
  };
  const enabled = buttons(status, loading || !!pending || !!error);
  return <section className="coding-control-runtime" aria-label="Local model">
    <div className="coding-control-runtime__head"><h3>Local model</h3><button type="button" onClick={() => { polls.current = 0; void refresh(); }} disabled={loading || !!pending}>Refresh</button></div>
    {loading ? <p role="status">Loading local model status…</p> : null}
    {pending ? <p role="status">{pending} pending…</p> : null}
    {error ? <p role="alert">Error: {error}{status ? " — Last known status follows." : ""}</p> : null}
    {status ? <>
      <p>{status.configured ? "Configured" : "Not configured"} · {status.message}</p>
      <div className="final-fusion__summary-strip">
        <span>Reachable · {yes(status.runtime_reachable)}</span><span>Model loaded · {yes(status.model_loaded)}</span>
        <span>Reason code · {show(status.reason_code)}</span><span>Model file · {show(status.model_file?.split(/[\\/]/).pop() ?? null)}</span>
        <span>Context size · {show(status.ctx_size)}</span><span>GPU layers · {show(status.n_gpu_layers)}</span>
        {status.vram_bytes != null ? <span>VRAM · {status.vram_bytes} bytes</span> : null}{status.ram_bytes != null ? <span>RAM · {status.ram_bytes} bytes</span> : null}
        <span>Spawned by Jarvis · {yes(status.spawned_by_jarvis)}</span><span>Crash count · {status.crash_count}</span><span>Last crash · {show(status.last_crash_at)}</span>
      </div>
    </> : null}
    <div className="final-fusion__toolbar-line">{(["start", "stop", "restart"] as const).map(action => <button key={action} type="button" disabled={!enabled[action]} onClick={() => void act(action)}>{action[0].toUpperCase() + action.slice(1)}</button>)}</div>
  </section>;
}
