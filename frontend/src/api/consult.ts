import { API_BASE_URL } from "./client";

export type ConsultActionOutcome = {
  status: string;
  code?: string | null;
  consult_id?: string | null;
  reason?: string | null;
  offending?: string[];
  answer_id?: string | null;
};

export type ConsultAnswerRead = {
  status: string;
  consult_id: string;
  untrusted?: boolean;
  model?: string | null;
  answer_sha256?: string | null;
  text?: string;
  next_offset?: number | null;
  note?: string;
};

export async function approveConsult(consultId: string, packetSha256: string): Promise<ConsultActionOutcome> {
  const response = await fetch(`${API_BASE_URL}/ai/consult/${consultId}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ packet_sha256: packetSha256 }),
  });
  return response.json() as Promise<ConsultActionOutcome>;
}

export async function editConsult(consultId: string, question: string, context: string = ""): Promise<ConsultActionOutcome> {
  const response = await fetch(`${API_BASE_URL}/ai/consult/${consultId}/edit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, context }),
  });
  return response.json() as Promise<ConsultActionOutcome>;
}

export async function rejectConsult(consultId: string): Promise<ConsultActionOutcome> {
  const response = await fetch(`${API_BASE_URL}/ai/consult/${consultId}/reject`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  return response.json() as Promise<ConsultActionOutcome>;
}

export async function fetchConsultAnswer(consultId: string, offset: number = 0): Promise<ConsultAnswerRead> {
  const response = await fetch(`${API_BASE_URL}/ai/consult/${consultId}/answer?offset=${offset}`);
  return response.json() as Promise<ConsultAnswerRead>;
}
