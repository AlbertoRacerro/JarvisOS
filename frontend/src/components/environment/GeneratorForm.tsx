import { useState } from "react";

import NumericInput from "../ui/NumericInput";
import { numericOrUndefined } from "../ui/numericStep";
export type GenerateRequest = {
  name: string;
  kind: "clear_sky" | "synthetic_day";
  parameters: Record<string, number | string>;
};

type Props = { onGenerate: (request: GenerateRequest) => void; disabled: boolean };

export function GeneratorForm({ onGenerate, disabled }: Props) {
  const [name, setName] = useState("Clear sky · 3 days");
  const [start, setStart] = useState(() => new Date().toISOString().slice(0, 16));
  const [resolution, setResolution] = useState(15);
  const [days, setDays] = useState(3);
  const [kind, setKind] = useState<GenerateRequest["kind"]>("clear_sky");
  const [clearness, setClearness] = useState(1);
  const [photoperiod, setPhotoperiod] = useState(12);
  const [peakPar, setPeakPar] = useState(1500);
  const [temperatureMean, setTemperatureMean] = useState(293.15);
  const [temperatureAmplitude, setTemperatureAmplitude] = useState(5);

  const generate = () => {
    const startDate = new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(start) ? start : `${start}Z`);
    if (!Number.isFinite(startDate.getTime())) return;
    const parameters: GenerateRequest["parameters"] = {
      start: startDate.toISOString(),
      count: (days * 24 * 60) / resolution,
      resolution_minutes: resolution,
    };
    if (kind === "clear_sky") parameters.clearness_factor = clearness;
    else Object.assign(parameters, {
      photoperiod, peak_par: peakPar, temperature_mean: temperatureMean,
      temperature_amplitude: temperatureAmplitude,
    });
    onGenerate({ name, kind, parameters });
  };

  return (
    <section className="environment-card">
      <h3>Create profile</h3>
      <label>Profile name<input value={name} onChange={(e) => setName(e.target.value)} /></label>
      <label>First interval end (UTC)<input value={start} onChange={(e) => setStart(e.target.value)} /></label>
      <p className="environment-help">The first value is stamped one interval after this start time.</p>
      <div className="environment-form-row">
        <label>Resolution<select value={resolution} onChange={(e) => setResolution(Number(e.target.value))}>
          {[5, 10, 15, 30, 60, 1440].map((step) => <option key={step} value={step}>
            {step === 1440 ? "Daily · 1440 min" : `${step} min`}
          </option>)}
        </select></label>
        <label>Days<NumericInput min={1} max={730} value={days} onValueChange={(text) => setDays((numericOrUndefined(text) ?? 0))} /></label>
      </div>
      <label>Generator<select value={kind} onChange={(e) => setKind(e.target.value as GenerateRequest["kind"])}>
        <option value="clear_sky">Clear sky</option><option value="synthetic_day">Synthetic day</option>
      </select></label>
      {kind === "clear_sky" ? (
        <label>Clearness factor<NumericInput min={0} max={1}
          value={clearness} onValueChange={(text) => setClearness((numericOrUndefined(text) ?? 0))} /></label>
      ) : (
        <div className="environment-form-row">
          <label>Photoperiod (h)<NumericInput min={0.1} max={24}
            value={photoperiod} onValueChange={(text) => setPhotoperiod((numericOrUndefined(text) ?? 0))} /></label>
          <label>Peak PAR (µmol/(m²·s))<NumericInput min={0} value={peakPar}
            onValueChange={(text) => setPeakPar((numericOrUndefined(text) ?? 0))} /></label>
          <label>Mean air temperature (K)<NumericInput min={200} max={350}
            value={temperatureMean} onValueChange={(text) => setTemperatureMean((numericOrUndefined(text) ?? 0))} /></label>
          <label>Temperature amplitude (K)<NumericInput min={0}
            value={temperatureAmplitude} onValueChange={(text) => setTemperatureAmplitude((numericOrUndefined(text) ?? 0))} /></label>
        </div>
      )}
      <button type="button" disabled={disabled} onClick={generate}>Generate</button>
    </section>
  );
}
