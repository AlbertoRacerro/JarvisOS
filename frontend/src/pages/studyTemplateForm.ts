// Spec 216 contract 8/9: pure contract building and validation for the Design component CFD study template form.
// The fluid domain always comes from the server-derived fluid-domain route; the user never types it.
import type { FluidDomain, PipeStudyContract } from "../api/designProject";

export type StudyRegime = "laminar" | "turbulent_rans";
export type TurbulenceModel = "kOmegaSST" | "kEpsilon";
export type WallTreatment = "resolved" | "low_re_resolved" | "wall_function";

export const DEFAULT_DENSITY_KG_M3 = 998.2;
export const DEFAULT_VISCOSITY_PA_S = 1.002e-3;
export const LAMINAR_RE_MAX = 2000;
export const TURBULENT_RE_MIN = 4000;
export const MAX_MESH_LEVEL = 4;
export const UPSTREAM_STATION_FRACTION = 0.65;
const LAMINAR_ENTRY_COEFF = 0.06;
const TURBULENT_ENTRY_COEFF = 4.4;
const DEVELOPMENT_MARGIN = 1.5;

export type StudyTemplateFormState = {
  regime: StudyRegime; turbulenceModel: TurbulenceModel; wallTreatment: WallTreatment;
  density: string; viscosity: string; velocity: string; meshLevel: string;
};

export const initialStudyTemplateForm = (): StudyTemplateFormState => ({
  regime: "turbulent_rans", turbulenceModel: "kOmegaSST", wallTreatment: "low_re_resolved",
  density: String(DEFAULT_DENSITY_KG_M3), viscosity: String(DEFAULT_VISCOSITY_PA_S), velocity: "", meshLevel: "1"
});

export const TURBULENCE_MODELS: TurbulenceModel[] = ["kOmegaSST", "kEpsilon"];

/** Wall treatments the contract accepts for a regime and model (kEpsilon is valid only with wall functions). */
export function wallTreatmentsFor(regime: StudyRegime, model: TurbulenceModel): WallTreatment[] {
  if (regime === "laminar") return ["resolved"];
  return model === "kEpsilon" ? ["wall_function"] : ["low_re_resolved", "wall_function"];
}

/** Keep the dependent selections valid after a regime or model change. */
export function normalizeStudyTemplateForm(form: StudyTemplateFormState): StudyTemplateFormState {
  const allowed = wallTreatmentsFor(form.regime, form.turbulenceModel);
  return allowed.includes(form.wallTreatment) ? form : { ...form, wallTreatment: allowed[0] };
}

const parse = (text: string): number | null => {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : null;
};

export type StudyTemplateBuild = { contract: PipeStudyContract | null; errors: string[]; reynolds: number | null };

/** Build the typed contract from the form and the derived domain, mirroring the server's validation. */
export function buildStudyContract(form: StudyTemplateFormState, domain: FluidDomain | null): StudyTemplateBuild {
  const errors: string[] = [];
  if (!domain) errors.push("The tube fluid domain is unknown; attach a single smooth tube geometry first.");
  const density = parse(form.density);
  const viscosity = parse(form.viscosity);
  const velocity = parse(form.velocity);
  const mesh = parse(form.meshLevel);
  if (density === null || density <= 0 || density > 5000) errors.push("Fluid density must be above 0 and at most 5000 kg/m³.");
  if (viscosity === null || viscosity <= 0 || viscosity > 10) errors.push("Dynamic viscosity must be above 0 and at most 10 Pa·s.");
  if (velocity === null || velocity <= 0 || velocity > 50) errors.push("Inlet mean velocity must be above 0 and at most 50 m/s.");
  if (mesh === null || !Number.isInteger(mesh) || mesh < 0 || mesh > MAX_MESH_LEVEL) errors.push(`Mesh level must be a whole number from 0 to ${MAX_MESH_LEVEL}.`);
  if (form.regime === "turbulent_rans" && !wallTreatmentsFor(form.regime, form.turbulenceModel).includes(form.wallTreatment))
    errors.push("kEpsilon is valid only with wall functions.");
  let reynolds: number | null = null;
  if (domain && density !== null && density > 0 && viscosity !== null && viscosity > 0 && velocity !== null && velocity > 0) {
    reynolds = velocity * domain.inner_diameter_m * density / viscosity;
    if (form.regime === "laminar" && reynolds > LAMINAR_RE_MAX) errors.push(`Laminar regime requested at Re ${Math.round(reynolds)}, above ${LAMINAR_RE_MAX}.`);
    if (form.regime === "turbulent_rans" && reynolds < TURBULENT_RE_MIN) errors.push(`Turbulent regime requested at Re ${Math.round(reynolds)}, below ${TURBULENT_RE_MIN}.`);
    const entry = form.regime === "laminar" ? LAMINAR_ENTRY_COEFF * reynolds : TURBULENT_ENTRY_COEFF * reynolds ** (1 / 6);
    const required = DEVELOPMENT_MARGIN * entry * domain.inner_diameter_m;
    if (UPSTREAM_STATION_FRACTION * domain.length_m < required)
      errors.push(`Tube length ${Number(domain.length_m.toPrecision(4))} m is too short for developed flow; about ${Number(required.toPrecision(4))} m is needed before the upstream pressure station.`);
  }
  if (errors.length || !domain) return { contract: null, errors, reynolds };
  const turbulent = form.regime === "turbulent_rans";
  return {
    errors, reynolds,
    contract: {
      contract_version: "cfd_pipe_study_v1", domain,
      fluid: { name: density === DEFAULT_DENSITY_KG_M3 && viscosity === DEFAULT_VISCOSITY_PA_S ? "water_20C" : "user_defined", density_kg_m3: density as number, dynamic_viscosity_pa_s: viscosity as number },
      inlet: { mean_velocity_m_s: velocity as number },
      outlet_pressure_pa: 0, regime: form.regime,
      turbulence_model: turbulent ? form.turbulenceModel : null,
      wall_treatment: turbulent ? form.wallTreatment : "resolved",
      mesh_level: mesh as number
    }
  };
}

/** Short display form of a template digest ("sha256:" prefix dropped). */
export const shortDigest = (digest: string): string => digest.replace(/^sha256:/, "").slice(0, 12);

/** Only load contracts this simple form can write back without dropping advanced inputs. */
export function formFromStudyContract(contract: PipeStudyContract): StudyTemplateFormState | null {
  const inlet = contract.inlet as PipeStudyContract["inlet"] & {
    flow_rate_m3_s?: number | null; turbulence_intensity?: number; mixing_length_over_d?: number;
  };
  const solver = (contract as PipeStudyContract & { solver?: {
    max_iterations: number; residual_target: number; wall_time_limit_s: number;
  } }).solver;
  const formFluidName = contract.fluid.density_kg_m3 === DEFAULT_DENSITY_KG_M3 &&
    contract.fluid.dynamic_viscosity_pa_s === DEFAULT_VISCOSITY_PA_S ? "water_20C" : "user_defined";
  if (contract.contract_version !== "cfd_pipe_study_v1" || inlet.mean_velocity_m_s == null ||
      inlet.flow_rate_m3_s != null || contract.outlet_pressure_pa !== 0 ||
      (inlet.turbulence_intensity ?? 0.05) !== 0.05 || (inlet.mixing_length_over_d ?? 0.07) !== 0.07 ||
      (solver && (solver.max_iterations !== 20000 || solver.residual_target !== 1e-6 ||
        solver.wall_time_limit_s !== 1800)) ||
      contract.fluid.name !== formFluidName) return null;
  return normalizeStudyTemplateForm({
    regime: contract.regime, turbulenceModel: contract.turbulence_model ?? "kOmegaSST",
    wallTreatment: contract.wall_treatment,
    density: String(contract.fluid.density_kg_m3), viscosity: String(contract.fluid.dynamic_viscosity_pa_s),
    velocity: String(inlet.mean_velocity_m_s), meshLevel: String(contract.mesh_level)
  });
}
