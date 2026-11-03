import { API_BASE_URL } from "./client";

export type RoadmapItem = {
  id: string;
  workspace_id: string;
  title: string;
  description?: string | null;
  item_type: string;
  status: string;
  priority: string;
  window_start_date?: string | null;
  window_end_date?: string | null;
  done_when?: string | null;
  done_when_satisfied?: boolean | null;
  revision: number;
};

export type CalendarAllocation = {
  id: string;
  workspace_id: string;
  roadmap_item_id?: string | null;
  title: string;
  event_type: string;
  start_instant: string;
  end_instant: string;
  timezone: string;
  priority?: string | null;
  revision: number;
};

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, init);
  if (!response.ok) {
    let message = `Request failed with ${response.status}`;
    try {
      const payload = await response.json() as { detail?: { message?: string } | string };
      if (typeof payload.detail === "string") message = payload.detail;
      else if (payload.detail?.message) message = payload.detail.message;
    } catch {
      // Preserve the status-based fallback when a non-JSON error reaches the UI.
    }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function listRoadmapItems(workspaceId: string): Promise<RoadmapItem[]> {
  return requestJson(`/development/roadmap/items?workspace_id=${encodeURIComponent(workspaceId)}`);
}

export function createRoadmapItem(workspaceId: string, title: string): Promise<RoadmapItem> {
  return requestJson("/development/roadmap/items", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      workspace_id: workspaceId,
      title,
      item_type: "Task",
      status: "Planned",
      priority: "Normal",
      created_by: "operator"
    })
  });
}

export function updateRoadmapItem(item: RoadmapItem, patch: Record<string, unknown>): Promise<RoadmapItem> {
  return requestJson(`/development/roadmap/items/${encodeURIComponent(item.id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace_id: item.workspace_id, expected_revision: item.revision, actor: "operator", ...patch })
  });
}

export function deleteRoadmapItem(item: RoadmapItem): Promise<void> {
  return requestJson(`/development/roadmap/items/${encodeURIComponent(item.id)}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace_id: item.workspace_id, expected_revision: item.revision, actor: "operator" })
  });
}

export function listCalendarAllocations(workspaceId: string): Promise<CalendarAllocation[]> {
  return requestJson(`/development/calendar/allocations?workspace_id=${encodeURIComponent(workspaceId)}`);
}

export function createCalendarAllocation(
  workspaceId: string,
  roadmapItemId: string | null,
  title: string,
  startLocal: string,
  endLocal: string,
  timezone: string
): Promise<CalendarAllocation> {
  return requestJson("/development/calendar/allocations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      workspace_id: workspaceId,
      roadmap_item_id: roadmapItemId,
      title,
      event_type: "work session",
      start_local: startLocal,
      end_local: endLocal,
      timezone,
      created_by: "operator"
    })
  });
}

export function updateCalendarAllocation(allocation: CalendarAllocation, patch: Record<string, unknown>): Promise<CalendarAllocation> {
  return requestJson(`/development/calendar/allocations/${encodeURIComponent(allocation.id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace_id: allocation.workspace_id, expected_revision: allocation.revision, actor: "operator", ...patch })
  });
}

export function deleteCalendarAllocation(allocation: CalendarAllocation): Promise<void> {
  return requestJson(`/development/calendar/allocations/${encodeURIComponent(allocation.id)}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace_id: allocation.workspace_id, expected_revision: allocation.revision, actor: "operator" })
  });
}
