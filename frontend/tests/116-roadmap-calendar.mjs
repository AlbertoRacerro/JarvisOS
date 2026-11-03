import fs from "node:fs";

const page = fs.readFileSync(new URL("../src/pages/DevelopmentRoadmap.tsx", import.meta.url), "utf8");
const api = fs.readFileSync(new URL("../src/api/development.ts", import.meta.url), "utf8");

function check(condition, message) {
  if (!condition) throw new Error(message);
}

for (const token of [
  "+ Add work item",
  "Edit work item",
  "Save work item",
  "Delete work item",
  "Lifecycle status",
  "Mark Done",
  "Project window:",
  "Scheduled blocks:",
  "Roadmap project-window timeline",
  "Execution status",
  "roadmap-timeline-window",
  "+ Add event",
  "Edit event",
  "Edit Roadmap item",
  "Edit start",
  "Edit end",
  "Edit time zone",
  "Save event",
  "Delete event",
  "Open roadmap item",
  "Opened roadmap item:",
  "View date",
  'aria-label="Calendar projection"',
  '"Day", "Week", "Month", "Agenda"'
]) {
  check(page.includes(token), `Development Roadmap must expose ${token}`);
}

check(page.includes('useState<CalendarView>("Week")'), "Calendar must default to Week projection");
check(page.includes("projectedAllocations"), "Calendar views must project canonical allocations instead of relabeling one unbounded list");
check(page.includes("allocationOverlapsDateRange"), "Calendar projections must include allocations overlapping the selected range");
check(page.includes("allocationDateKey"), "Calendar projections must use saved-zone allocation dates");
check(page.includes("formatAllocationInstant"), "Calendar times must render in the saved IANA zone");
check(page.includes("timelineRange"), "Timeline must project Roadmap project windows over a shared date range");
check(page.includes("await refresh(workspaceId)"), "Mutation paths must reconcile against fresh server-owned state");
check(page.includes("updateRoadmapItem(item"), "Roadmap edits must use server-owned CAS mutation");
check(page.includes("deleteRoadmapItem(item)"), "Roadmap deletes must use server-owned CAS mutation");
check(page.includes("updateCalendarAllocation(allocation"), "Calendar edits must use server-owned CAS mutation");
check(page.includes("deleteCalendarAllocation(allocation)"), "Calendar deletes must use server-owned CAS mutation");
check(api.includes('event_type: "work session"'), "Calendar creation must use the bounded event vocabulary");
check(api.includes('method: "DELETE"'), "Development API client must expose DELETE mutations");
check(!page.toLowerCase().includes("board"), "116 must not introduce Board state or affordances");

console.log("116 roadmap/calendar frontend contract: OK");
