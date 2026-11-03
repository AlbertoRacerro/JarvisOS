import type { ComponentType, ReactNode } from "react";

import type { Navigate } from "../app/AppLink";
import type { StageSelection } from "../app/selection";
import type { StageKind } from "../app/routes";
import ModelStage from "./ModelStage";
import ProcessStage from "./ProcessStage";
import ReviewStage from "./ReviewStage";
import EngineeringStudiesStage from "./EngineeringStudiesStage";

export type ShellRegion = "navigator" | "sidecar" | "dock";

export type ShellRegionContributions = Readonly<{
  navigator?: ReactNode;
  sidecar?: ReactNode;
  dock?: ReactNode;
}>;

export type PrimaryStageProps = Readonly<{
  workspaceId: string | null;
  onWorkspaceChange(next: string | null): void;
  selection: StageSelection | null;
  onSelectionChange(next: StageSelection | null): void;
  onShellRegionsChange(next: ShellRegionContributions): void;
  requestShellRegionOpen(region: ShellRegion): void;
  navigate: Navigate;
}>;

export type StageDefinition = Readonly<{
  kind: StageKind;
  label: string;
  render: ComponentType<PrimaryStageProps>;
}>;

export const PRIMARY_STAGES: Readonly<Record<StageKind, StageDefinition>> = {
  model: { kind: "model", label: "Model", render: ModelStage },
  process: { kind: "process", label: "Process", render: ProcessStage },
  review: { kind: "review", label: "Review", render: ReviewStage },
  studies: { kind: "studies", label: "Studies", render: EngineeringStudiesStage }
};
