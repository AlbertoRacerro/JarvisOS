import { API_BASE_URL } from "./client";

export type StepState = "done" | "blocked" | "in_progress" | "not_started";
export type QuestionStatus = "open" | "closed";

export type GuidedStepDefinition = {
  step: string;
  order: number;
  label_it: string;
  route: string;
  description_it: string;
};

export type NextAction = {
  step: string;
  title_it: string;
  reason_it: string;
  action_label_it: string;
  route: string;
};

export type GuidedStepEvaluation = {
  step: string;
  order: number;
  label_it: string;
  route: string;
  description_it: string;
  state: StepState;
  blocked_items: string[];
};

export type DesignQuestion = {
  id: string;
  workspace_id: string;
  title: string;
  status: QuestionStatus;
  what_changes: string | null;
  sensitivity: string | null;
  explore_skip_reason: string | null;
  linked_decision_id: string | null;
  created_at: string;
  updated_at: string;
  steps: GuidedStepEvaluation[];
  next_action: NextAction | null;
};

export type GuidedQuestionsResponse = {
  steps_definition: GuidedStepDefinition[];
  questions: DesignQuestion[];
  active_question_id: string | null;
  steps: GuidedStepEvaluation[];
  next_action: NextAction | null;
};

export type CreateQuestionPayload = {
  title: string;
  what_changes?: string | null;
  sensitivity?: string | null;
  explore_skip_reason?: string | null;
  linked_decision_id?: string | null;
};

export type UpdateQuestionPayload = {
  title?: string | null;
  status?: QuestionStatus | null;
  what_changes?: string | null;
  sensitivity?: string | null;
  explore_skip_reason?: string | null;
  linked_decision_id?: string | null;
};

export async function getGuidedQuestions(workspaceId: string): Promise<GuidedQuestionsResponse> {
  const response = await fetch(`${API_BASE_URL}/workspaces/${encodeURIComponent(workspaceId)}/engineering/guided-questions`);
  if (!response.ok) {
    throw new Error(`Failed to load guided questions: ${response.status}`);
  }
  return response.json() as Promise<GuidedQuestionsResponse>;
}

export async function createGuidedQuestion(
  workspaceId: string,
  payload: CreateQuestionPayload,
): Promise<DesignQuestion> {
  const response = await fetch(
    `${API_BASE_URL}/workspaces/${encodeURIComponent(workspaceId)}/engineering/guided-questions`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  if (!response.ok) {
    throw new Error(`Failed to create question: ${response.status}`);
  }
  return response.json() as Promise<DesignQuestion>;
}

export async function updateGuidedQuestion(
  workspaceId: string,
  questionId: string,
  payload: UpdateQuestionPayload,
): Promise<DesignQuestion> {
  const response = await fetch(
    `${API_BASE_URL}/workspaces/${encodeURIComponent(workspaceId)}/engineering/guided-questions/${encodeURIComponent(questionId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  if (!response.ok) {
    throw new Error(`Failed to update question: ${response.status}`);
  }
  return response.json() as Promise<DesignQuestion>;
}
