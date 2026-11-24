/**
 * Spec 350 Stage 3: Pure helper mapping active Process draft + Environment + CAD component
 * metadata into initial defaults for Dynamic, Motion/Mechanics, Axial O2, and Optics stages,
 * preserving user edits and labeling parameter provenance ("from Process", "from Environment", "manual").
 */

import type { DraftObject, DraftProjection } from "../../api/processDraft";
import type { Profile, ProfileValues } from "../environment/types";
import type { DesignComponent } from "../../api/designProject";
import type { BenchRunRow, BenchVerdict } from "../../api/benchRuns";
import type { DynRun } from "../dynamic/dynamicLogic";
import type { CfdRun } from "../../api/designProject";

export type ProvenanceSource = "from Process" | "from Environment" | "manual";

export interface ParameterWithProvenance {
  value: number;
  source: ProvenanceSource;
  unit?: string;
  label?: string;
}

export interface StringParameterWithProvenance {
  value: string;
  source: ProvenanceSource;
  label?: string;
}

export interface MotionDefaults {
  mass_kg: ParameterWithProvenance;
  added_mass_surge_kg: ParameterWithProvenance;
  added_mass_heave_kg: ParameterWithProvenance;
  pitch_inertia_kg_m2: ParameterWithProvenance;
  waterplane_area_m2: ParameterWithProvenance;
  gm_pitch_m: ParameterWithProvenance;
  damping_surge_n_s_m: ParameterWithProvenance;
  damping_heave_n_s_m: ParameterWithProvenance;
  damping_pitch_n_m_s_rad: ParameterWithProvenance;
  mooring_kx_n_m: ParameterWithProvenance;
  mooring_kz_n_m: ParameterWithProvenance;
  wave_amplitude_m: ParameterWithProvenance;
  wave_period_s: ParameterWithProvenance;
  water_density_kg_m3: ParameterWithProvenance;
  gravity_m_s2: ParameterWithProvenance;
}

export interface AxialO2Defaults {
  length: ParameterWithProvenance;
  velocity: ParameterWithProvenance;
  oxygen_production_rate: ParameterWithProvenance;
  saturation_concentration: ParameterWithProvenance;
  inlet_saturation_fraction: ParameterWithProvenance;
  max_saturation_fraction: ParameterWithProvenance;
}

export interface DynamicDefaults {
  temperature_c: ParameterWithProvenance;
  par_irradiance: ParameterWithProvenance;
}

export interface OpticsDefaults {
  optics_model: StringParameterWithProvenance;
  tube_inner_diameter_m: ParameterWithProvenance;
}

function extractNumber(obj: unknown, key: string): number | null {
  if (!obj || typeof obj !== "object") return null;
  const record = obj as Record<string, unknown>;
  const val = record[key];
  if (typeof val === "number" && Number.isFinite(val)) return val;
  if (typeof val === "string") {
    const parsed = Number.parseFloat(val);
    if (Number.isFinite(parsed)) return parsed;
  }
  if (val && typeof val === "object") {
    const inner = val as Record<string, unknown>;
    if (typeof inner.value === "number" && Number.isFinite(inner.value)) return inner.value;
    if (typeof inner.si === "number" && Number.isFinite(inner.si)) return inner.si;
    if (typeof inner.value === "string") {
      const parsed = Number.parseFloat(inner.value);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return null;
}

export function isTubularUnit(unit?: DraftObject | null): boolean {
  if (!unit) return false;
  if (unit.type === "PhotobioreactorT1" || unit.type === "PFR") return true;
  const params = unit.params ?? {};
  return (
    params.tube_length !== undefined ||
    params.tube_inner_diameter !== undefined ||
    params.tubular !== undefined
  );
}

/**
 * Maps active Environment profile / values and CAD component properties into Motion defaults.
 */
export function deriveMotionDefaults(
  envProfile?: Profile | null,
  envValues?: ProfileValues | null,
  component?: DesignComponent | null,
  userEdits: Partial<Record<keyof MotionDefaults, number>> = {},
): MotionDefaults {
  // Wave amplitude: from environment wave_height / 2 if available, else default 1.0
  let waveAmpVal = 1.0;
  let waveAmpSource: ProvenanceSource = "manual";
  if (userEdits.wave_amplitude_m !== undefined) {
    waveAmpVal = userEdits.wave_amplitude_m;
    waveAmpSource = "manual";
  } else if (envValues?.channels?.wave_height && envValues.channels.wave_height.length > 0) {
    const raw = envValues.channels.wave_height.find((v) => v !== null && v > 0);
    if (raw != null) {
      waveAmpVal = raw / 2.0;
      waveAmpSource = "from Environment";
    }
  } else if (envProfile?.channels?.wave_height) {
    waveAmpVal = 1.25;
    waveAmpSource = "from Environment";
  }

  // Wave period: from environment wave_period if available, else default 8.0
  let wavePeriodVal = 8.0;
  let wavePeriodSource: ProvenanceSource = "manual";
  if (userEdits.wave_period_s !== undefined) {
    wavePeriodVal = userEdits.wave_period_s;
    wavePeriodSource = "manual";
  } else if (envValues?.channels?.wave_period && envValues.channels.wave_period.length > 0) {
    const raw = envValues.channels.wave_period.find((v) => v !== null && v > 0);
    if (raw != null) {
      wavePeriodVal = raw;
      wavePeriodSource = "from Environment";
    }
  } else if (envProfile?.channels?.wave_period) {
    wavePeriodVal = 8.0;
    wavePeriodSource = "from Environment";
  }

  // Mass: from component properties or Process draft if present, else default 500,000 kg
  let massVal = 500000.0;
  let massSource: ProvenanceSource = "manual";
  if (userEdits.mass_kg !== undefined) {
    massVal = userEdits.mass_kg;
    massSource = "manual";
  } else if (component) {
    const hintedMass = extractNumber(component, "mass_kg") ?? extractNumber(component, "mass");
    if (hintedMass != null && hintedMass > 0) {
      massVal = hintedMass;
      massSource = "from Process";
    }
  }

  // Waterplane area: from component properties if present, else default 400 m2
  let awVal = 400.0;
  let awSource: ProvenanceSource = "manual";
  if (userEdits.waterplane_area_m2 !== undefined) {
    awVal = userEdits.waterplane_area_m2;
    awSource = "manual";
  } else if (component) {
    const hintedAw =
      extractNumber(component, "waterplane_area_m2") ??
      extractNumber(component, "waterplane_area");
    if (hintedAw != null && hintedAw > 0) {
      awVal = hintedAw;
      awSource = "from Process";
    }
  }

  return {
    wave_amplitude_m: { value: waveAmpVal, source: waveAmpSource, unit: "m", label: "Wave amplitude" },
    wave_period_s: { value: wavePeriodVal, source: wavePeriodSource, unit: "s", label: "Wave period" },
    mass_kg: { value: massVal, source: massSource, unit: "kg", label: "Displaced mass" },
    waterplane_area_m2: { value: awVal, source: awSource, unit: "m²", label: "Waterplane area" },
    added_mass_surge_kg: {
      value: userEdits.added_mass_surge_kg ?? 50000.0,
      source: "manual",
      unit: "kg",
      label: "Surge added mass",
    },
    added_mass_heave_kg: {
      value: userEdits.added_mass_heave_kg ?? 150000.0,
      source: "manual",
      unit: "kg",
      label: "Heave added mass",
    },
    pitch_inertia_kg_m2: {
      value: userEdits.pitch_inertia_kg_m2 ?? 5e8,
      source: "manual",
      unit: "kg·m²",
      label: "Pitch inertia",
    },
    gm_pitch_m: { value: userEdits.gm_pitch_m ?? 2.0, source: "manual", unit: "m", label: "Metacentric height (GM)" },
    damping_surge_n_s_m: {
      value: userEdits.damping_surge_n_s_m ?? 500000.0,
      source: "manual",
      unit: "N·s/m",
      label: "Surge damping",
    },
    damping_heave_n_s_m: {
      value: userEdits.damping_heave_n_s_m ?? 800000.0,
      source: "manual",
      unit: "N·s/m",
      label: "Heave damping",
    },
    damping_pitch_n_m_s_rad: {
      value: userEdits.damping_pitch_n_m_s_rad ?? 2e8,
      source: "manual",
      unit: "N·m·s/rad",
      label: "Pitch damping",
    },
    mooring_kx_n_m: {
      value: userEdits.mooring_kx_n_m ?? 500000.0,
      source: "manual",
      unit: "N/m",
      label: "Mooring stiffness (surge)",
    },
    mooring_kz_n_m: {
      value: userEdits.mooring_kz_n_m ?? 0.0,
      source: "manual",
      unit: "N/m",
      label: "Mooring stiffness (heave)",
    },
    water_density_kg_m3: {
      value: userEdits.water_density_kg_m3 ?? 1025.0,
      source: "manual",
      unit: "kg/m³",
      label: "Water density",
    },
    gravity_m_s2: {
      value: userEdits.gravity_m_s2 ?? 9.80665,
      source: "manual",
      unit: "m/s²",
      label: "Gravity acceleration",
    },
  };
}

/**
 * Maps active Process draft, tubular unit parameters, and latest Dynamic run into Axial O2 defaults.
 */
export function deriveAxialO2Defaults(
  draft?: DraftProjection | null,
  unit?: DraftObject | null,
  dynamicRun?: DynRun | null,
  envValues?: ProfileValues | null,
  userEdits: Partial<Record<keyof AxialO2Defaults, number>> = {},
): AxialO2Defaults {
  const chosenUnit =
    unit ?? draft?.objects?.find((item) => item.kind === "unit" && isTubularUnit(item));

  // Length: from unit tube_length / length or default 50.0
  let lengthVal = 50.0;
  let lengthSource: ProvenanceSource = "manual";
  if (userEdits.length !== undefined) {
    lengthVal = userEdits.length;
    lengthSource = "manual";
  } else if (chosenUnit) {
    const l =
      extractNumber(chosenUnit.params, "tube_length") ??
      extractNumber(chosenUnit.params, "length") ??
      extractNumber(chosenUnit.params, "total_length");
    if (l != null && l > 0) {
      lengthVal = l;
      lengthSource = "from Process";
    }
  }

  // Velocity: from unit velocity / liquid_velocity or default 0.5
  let velocityVal = 0.5;
  let velocitySource: ProvenanceSource = "manual";
  if (userEdits.velocity !== undefined) {
    velocityVal = userEdits.velocity;
    velocitySource = "manual";
  } else if (chosenUnit) {
    const u =
      extractNumber(chosenUnit.params, "velocity") ??
      extractNumber(chosenUnit.params, "liquid_velocity") ??
      extractNumber(chosenUnit.params, "u_l");
    if (u != null && u > 0) {
      velocityVal = u;
      velocitySource = "from Process";
    }
  }

  // Oxygen production rate: from unit oxygen_production_rate / reaction rate or default 0.003
  let rateVal = 0.003;
  let rateSource: ProvenanceSource = "manual";
  if (userEdits.oxygen_production_rate !== undefined) {
    rateVal = userEdits.oxygen_production_rate;
    rateSource = "manual";
  } else if (chosenUnit) {
    const r =
      extractNumber(chosenUnit.params, "oxygen_production_rate") ??
      extractNumber(chosenUnit.params, "volumetric_o2_rate") ??
      extractNumber(chosenUnit.params, "reaction_rate");
    if (r != null && r > 0) {
      rateVal = r;
      rateSource = "from Process";
    }
  }

  // Saturation concentration: default 0.25 mol/m3 (~8 mg/L at 20°C in water)
  // or adjusted if temperature is provided in environment or process
  let satVal = 0.25;
  let satSource: ProvenanceSource = "manual";
  if (userEdits.saturation_concentration !== undefined) {
    satVal = userEdits.saturation_concentration;
    satSource = "manual";
  } else if (envValues?.channels?.sea_temperature) {
    const t = envValues.channels.sea_temperature.find((v) => v !== null);
    if (t != null) {
      // Rough solubility variation ~ 0.25 * (1 - 0.015 * (t - 20))
      satVal = Math.max(0.15, Math.min(0.4, 0.25 * (1 - 0.015 * (t - 20))));
      satSource = "from Environment";
    }
  } else if (chosenUnit) {
    const t = extractNumber(chosenUnit.params, "temperature");
    if (t != null) {
      satVal = Math.max(0.15, Math.min(0.4, 0.25 * (1 - 0.015 * (t - 20))));
      satSource = "from Process";
    }
  }

  return {
    length: { value: lengthVal, source: lengthSource, unit: "m", label: "Tube length" },
    velocity: { value: velocityVal, source: velocitySource, unit: "m/s", label: "Liquid velocity" },
    oxygen_production_rate: {
      value: rateVal,
      source: rateSource,
      unit: "mol/(m³·s)",
      label: "Volumetric O₂ production rate",
    },
    saturation_concentration: {
      value: satVal,
      source: satSource,
      unit: "mol/m³",
      label: "O₂ saturation concentration",
    },
    inlet_saturation_fraction: {
      value: userEdits.inlet_saturation_fraction ?? 1.0,
      source: "manual",
      unit: "1",
      label: "Inlet saturation fraction",
    },
    max_saturation_fraction: {
      value: userEdits.max_saturation_fraction ?? 3.0,
      source: "manual",
      unit: "1",
      label: "Max saturation fraction",
    },
  };
}

export interface StageRunSummary {
  stage: "Plant" | "Dynamic" | "Motion/Mechanics" | "Axial O2" | "CFD" | "Optics";
  hasRuns: boolean;
  latestVerdict?: BenchVerdict | "succeeded" | "failed" | "finished" | "empty";
  runCount: number;
  label: string;
}

export function buildVariantStageSummary({
  benchRuns = [],
  dynamicRuns = [],
  cfdRuns = [],
  hasOpticsUnits = false,
}: {
  benchRuns?: readonly BenchRunRow[];
  dynamicRuns?: readonly DynRun[];
  cfdRuns?: readonly CfdRun[];
  hasOpticsUnits?: boolean;
}): StageRunSummary[] {
  const motionRuns = benchRuns.filter((r) => r.evaluator_id === "marine.rigid_body_waves");
  const axialRuns = benchRuns.filter(
    (r) =>
      r.evaluator_id === "bioprocess.tube_oxygen_buildup" ||
      r.evaluator_id === "photobioreactor.tube_oxygen",
  );

  const motionLatest = motionRuns[0];
  const axialLatest = axialRuns[0];
  const dynamicLatest = dynamicRuns.find((r) => r.status === "succeeded") ?? dynamicRuns[0];
  const cfdLatest = cfdRuns.find((r) => r.state === "finished") ?? cfdRuns[0];

  return [
    {
      stage: "Dynamic",
      hasRuns: dynamicRuns.length > 0,
      latestVerdict: dynamicLatest ? (dynamicLatest.status as any) : "empty",
      runCount: dynamicRuns.length,
      label: dynamicLatest
        ? `${dynamicRuns.filter((r) => r.status === "succeeded").length} succeeded · ${dynamicRuns.length} total`
        : "No runs yet",
    },
    {
      stage: "Motion/Mechanics",
      hasRuns: motionRuns.length > 0,
      latestVerdict: motionLatest ? motionLatest.verdict : "empty",
      runCount: motionRuns.length,
      label: motionLatest ? `${motionLatest.verdict} (${motionRuns.length} runs)` : "No runs yet",
    },
    {
      stage: "Axial O2",
      hasRuns: axialRuns.length > 0,
      latestVerdict: axialLatest ? axialLatest.verdict : "empty",
      runCount: axialRuns.length,
      label: axialLatest ? `${axialLatest.verdict} (${axialRuns.length} runs)` : "No runs yet",
    },
    {
      stage: "CFD",
      hasRuns: cfdRuns.length > 0,
      latestVerdict: cfdLatest ? (cfdLatest.state as any) : "empty",
      runCount: cfdRuns.length,
      label: cfdLatest
        ? `${cfdRuns.filter((r) => r.state === "finished").length} finished · ${cfdRuns.length} total`
        : "No runs yet",
    },
    {
      stage: "Optics",
      hasRuns: hasOpticsUnits,
      latestVerdict: hasOpticsUnits ? "succeeded" : "empty",
      runCount: hasOpticsUnits ? 1 : 0,
      label: hasOpticsUnits ? "Parameters configured" : "Not configured",
    },
  ];
}
