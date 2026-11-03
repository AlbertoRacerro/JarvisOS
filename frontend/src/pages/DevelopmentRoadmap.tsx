import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { listWorkspaces, type Workspace } from "../api/client";
import {
  createCalendarAllocation,
  createRoadmapItem,
  deleteCalendarAllocation,
  deleteRoadmapItem,
  listCalendarAllocations,
  listRoadmapItems,
  updateCalendarAllocation,
  updateRoadmapItem,
  type CalendarAllocation,
  type RoadmapItem
} from "../api/development";

import "../styles/development-roadmap.css";

type Props = {
  jarvis?: ReactNode;
  mode: "timeline" | "calendar";
  workspaceId: string | null;
  onWorkspaceChange(next: string | null): void;
};

type CalendarView = "Day" | "Week" | "Month" | "Agenda";
const ROADMAP_STATUSES = ["Planned", "Ready", "In progress", "Blocked", "Done", "Cancelled"] as const;
const DAY_MS = 24 * 60 * 60 * 1000;

function formatAllocationInstant(instant: string, timeZone: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone
  }).format(new Date(instant));
}

function zonedParts(instant: string | Date, timeZone: string): Record<string, string> {
  return Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23"
    }).formatToParts(typeof instant === "string" ? new Date(instant) : instant).filter((part) => part.type !== "literal").map((part) => [part.type, part.value])
  );
}

function dateKeyForInstant(instant: string | Date, timeZone: string): string {
  const parts = zonedParts(instant, timeZone);
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function allocationDateKey(allocation: CalendarAllocation, timeZone = allocation.timezone): string {
  return dateKeyForInstant(allocation.start_instant, timeZone);
}

function allocationEndDateKey(allocation: CalendarAllocation, timeZone = allocation.timezone): string {
  const exclusiveEnd = new Date(Math.max(
    new Date(allocation.start_instant).getTime(),
    new Date(allocation.end_instant).getTime() - 1
  ));
  return dateKeyForInstant(exclusiveEnd, timeZone);
}

function allocationOverlapsDateRange(allocation: CalendarAllocation, firstDay: string, lastDay: string, timeZone = allocation.timezone): boolean {
  return allocationDateKey(allocation, timeZone) <= lastDay && allocationEndDateKey(allocation, timeZone) >= firstDay;
}

function allocationLocalInput(instant: string, timeZone: string): string {
  const parts = zonedParts(instant, timeZone);
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

function shiftDate(dateKey: string, days: number): string {
  const date = new Date(`${dateKey}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function weekStart(dateKey: string): string {
  const date = new Date(`${dateKey}T00:00:00Z`);
  const mondayOffset = (date.getUTCDay() + 6) % 7;
  return shiftDate(dateKey, -mondayOffset);
}

function monthRange(dateKey: string): [string, string] {
  const [year, month] = dateKey.split("-").map(Number);
  const first = `${year.toString().padStart(4, "0")}-${month.toString().padStart(2, "0")}-01`;
  const nextMonth = new Date(Date.UTC(year, month, 1));
  nextMonth.setUTCDate(nextMonth.getUTCDate() - 1);
  return [first, nextMonth.toISOString().slice(0, 10)];
}

function dayOffset(dateKey: string, origin: string): number {
  return Math.round((new Date(`${dateKey}T00:00:00Z`).getTime() - new Date(`${origin}T00:00:00Z`).getTime()) / DAY_MS);
}

function renderedStartMinute(allocation: CalendarAllocation, day: string, timeZone: string): number {
  // An event that began before this column is rendered from midnight. This
  // keeps overnight blocks visible instead of placing them at the previous
  // day's wall-clock start.
  if (allocationDateKey(allocation, timeZone) < day) return 0;
  const start = zonedParts(allocation.start_instant, timeZone);
  return Number(start.hour) * 60 + Number(start.minute);
}

export default function DevelopmentRoadmap({ mode, workspaceId, onWorkspaceChange, jarvis }: Props) {
  const activeWorkspace = useRef(workspaceId);
  activeWorkspace.current = workspaceId;
  const [selectedItemId, setSelectedItemId] = useState<string | null>(null);
  const [selectedAllocationId, setSelectedAllocationId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const displayTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const calendarScroller = useRef<HTMLDivElement>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [items, setItems] = useState<RoadmapItem[]>([]);
  const [allocations, setAllocations] = useState<CalendarAllocation[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newTitle, setNewTitle] = useState("");
  const [editingItemId, setEditingItemId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editWindowStart, setEditWindowStart] = useState("");
  const [editWindowEnd, setEditWindowEnd] = useState("");
  const [editDoneWhen, setEditDoneWhen] = useState("");
  const [calendarView, setCalendarView] = useState<CalendarView>("Week");
  const [calendarAnchor, setCalendarAnchor] = useState(() => new Date().toISOString().slice(0, 10));
  const [calendarAnchorTouched, setCalendarAnchorTouched] = useState(false);
  const [eventTitle, setEventTitle] = useState("");
  const [eventItemId, setEventItemId] = useState("");
  const [eventStart, setEventStart] = useState("");
  const [eventEnd, setEventEnd] = useState("");
  const [eventTimezone, setEventTimezone] = useState(Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  const [editingAllocationId, setEditingAllocationId] = useState<string | null>(null);
  const [editEventTitle, setEditEventTitle] = useState("");
  const [editEventItemId, setEditEventItemId] = useState("");
  const [editEventStart, setEditEventStart] = useState("");
  const [editEventEnd, setEditEventEnd] = useState("");
  const [editEventTimezone, setEditEventTimezone] = useState("");
  const requestedRoadmapItemId = new URLSearchParams(window.location.search).get("roadmap_item_id");

  useEffect(() => {
    let alive = true;
    listWorkspaces().then((rows) => {
      if (!alive) return;
      setWorkspaces(rows);
      if (!workspaceId && rows[0]) onWorkspaceChange(rows[0].id);
      else if (workspaceId && !rows.some((row) => row.id === workspaceId)) onWorkspaceChange(rows[0]?.id ?? null);
    }).catch((exc: unknown) => alive && setError(exc instanceof Error ? exc.message : "Workspace discovery failed."));
    return () => { alive = false; };
  }, [workspaceId, onWorkspaceChange]);

  async function refresh(selectedWorkspaceId: string) {
    const [nextItems, nextAllocations] = await Promise.all([
      listRoadmapItems(selectedWorkspaceId),
      listCalendarAllocations(selectedWorkspaceId)
    ]);
    if (activeWorkspace.current !== selectedWorkspaceId) return;
    setItems(nextItems);
    setAllocations(nextAllocations);
  }

  useEffect(() => {
    setItems([]); setAllocations([]); setSelectedItemId(null); setSelectedAllocationId(null);
    setEditingItemId(null); setEditingAllocationId(null); setShowCreate(false); setBusy(false); setNotice(null);
    setCalendarAnchorTouched(false); setEventItemId("");
    if (!workspaceId) {
      setItems([]);
      setAllocations([]);
      return;
    }
    let alive = true;
    setLoading(true);
    setError(null);
    Promise.all([listRoadmapItems(workspaceId), listCalendarAllocations(workspaceId)])
      .then(([nextItems, nextAllocations]) => {
        if (!alive) return;
        setItems(nextItems);
        setAllocations(nextAllocations);
      })
      .catch((exc: unknown) => alive && setError(exc instanceof Error ? exc.message : "Development data failed to load.")).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [workspaceId]);

  useEffect(() => {
    if (mode !== "calendar" || eventItemId || items.length === 0) return;
    if (requestedRoadmapItemId && items.some((item) => item.id === requestedRoadmapItemId)) setEventItemId(requestedRoadmapItemId);
  }, [mode, eventItemId, items, requestedRoadmapItemId]);

  useEffect(() => {
    if (mode !== "calendar" || calendarAnchorTouched || allocations.length === 0) return;
    setCalendarAnchor(allocationDateKey(allocations[0], displayTimezone));
  }, [mode, allocations, calendarAnchorTouched]);

  useEffect(() => {
    if (mode !== "timeline" || !requestedRoadmapItemId || items.length === 0) return;
    setSelectedItemId(requestedRoadmapItemId);
  }, [mode, requestedRoadmapItemId, items]);

  const allocationsByItem = useMemo(() => {
    const map = new Map<string, number>();
    for (const allocation of allocations) {
      if (allocation.roadmap_item_id) map.set(allocation.roadmap_item_id, (map.get(allocation.roadmap_item_id) ?? 0) + 1);
    }
    return map;
  }, [allocations]);

  const timelineRange = useMemo(() => {
    const starts = items.map((item) => item.window_start_date).filter((value): value is string => Boolean(value));
    const ends = items.map((item) => item.window_end_date).filter((value): value is string => Boolean(value));
    if (starts.length === 0 && ends.length === 0) return null;
    const ordered = [...starts, ...ends].sort();
    const first = ordered[0];
    const last = ordered[ordered.length - 1] ?? first;
    return { first, last, days: Math.max(1, dayOffset(last, first) + 1) };
  }, [items]);

  const projectedAllocations = useMemo(() => {
    const sorted = [...allocations].sort((left, right) => left.start_instant.localeCompare(right.start_instant));
    const matches = sorted.filter((allocation) => allocation.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
    if (calendarView === "Agenda") return matches;
    if (calendarView === "Day") return matches.filter((allocation) => allocationOverlapsDateRange(allocation, calendarAnchor, calendarAnchor, displayTimezone));
    if (calendarView === "Month") {
      const [firstDay, lastDay] = monthRange(calendarAnchor);
      return matches.filter((allocation) => allocationOverlapsDateRange(allocation, firstDay, lastDay, displayTimezone));
    }
    const firstDay = weekStart(calendarAnchor);
    const lastDay = shiftDate(firstDay, 6);
    return matches.filter((allocation) => allocationOverlapsDateRange(allocation, firstDay, lastDay, displayTimezone));
  }, [allocations, calendarView, calendarAnchor, query, displayTimezone]);

  const openedRoadmapItem = mode === "timeline" && requestedRoadmapItemId
    ? items.find((item) => item.id === requestedRoadmapItemId) ?? null
    : null;

  async function run(action: () => Promise<void>) {
    if (!workspaceId || busy) return;
    const originWorkspace = workspaceId;
    setBusy(true);
    setError(null); setNotice(null);
    try {
      await action();
      await refresh(workspaceId);
      if (activeWorkspace.current === originWorkspace) setNotice("Saved successfully.");
    } catch (exc) {
      if (activeWorkspace.current !== originWorkspace) return;
      setError(exc instanceof Error ? exc.message : "Development mutation failed.");
      try {
        await refresh(workspaceId);
      } catch {
        // Keep the original mutation error visible; the next explicit refresh remains available through navigation.
      }
    } finally {
      if (activeWorkspace.current === originWorkspace) setBusy(false);
    }
  }

  function beginRoadmapEdit(item: RoadmapItem) {
    setEditingItemId(item.id);
    setEditTitle(item.title);
    setEditWindowStart(item.window_start_date ?? "");
    setEditWindowEnd(item.window_end_date ?? "");
    setEditDoneWhen(item.done_when ?? "");
  }

  function beginAllocationEdit(allocation: CalendarAllocation) {
    setEditingAllocationId(allocation.id);
    setEditEventTitle(allocation.title);
    setEditEventItemId(allocation.roadmap_item_id ?? "");
    setEditEventStart(allocationLocalInput(allocation.start_instant, allocation.timezone));
    setEditEventEnd(allocationLocalInput(allocation.end_instant, allocation.timezone));
    setEditEventTimezone(allocation.timezone);
  }

  const visibleItems = items.filter((item) => (!statusFilter || item.status === statusFilter) && item.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  const calendarDays = useMemo(() => {
    if (!calendarAnchor) return [];
    if (calendarView === "Day") return [calendarAnchor];
    const [monthFirst, monthLast] = monthRange(calendarAnchor);
    const first = weekStart(calendarView === "Month" ? monthFirst : calendarAnchor);
    const count = calendarView === "Month" ? Math.ceil((dayOffset(monthLast, first) + 1) / 7) * 7 : 7;
    return Array.from({ length: count }, (_, index) => shiftDate(first, index));
  }, [calendarAnchor, calendarView]);
  useEffect(() => {
    if (calendarScroller.current && (calendarView === "Day" || calendarView === "Week")) {
      // Keep the first visible event in view with one hour of context. Use
      // display-timezone parts so an event authored in another zone lands on
      // the same day/hour the operator sees in the grid. Overnight events
      // clamp to midnight; an empty calendar opens on a useful workday hour.
      const earliest = projectedAllocations[0];
      if (!earliest) {
        calendarScroller.current.scrollTop = 8 * 56;
        return;
      }
      const visibleDays = calendarDays.filter((day) => projectedAllocations.some((allocation) => allocationOverlapsDateRange(allocation, day, day, displayTimezone)));
      const minutes = visibleDays.flatMap((day) => projectedAllocations
        .filter((allocation) => allocationOverlapsDateRange(allocation, day, day, displayTimezone))
        .map((allocation) => renderedStartMinute(allocation, day, displayTimezone)))
        .reduce((minimum, value) => Math.min(minimum, value), Number.POSITIVE_INFINITY);
      if (!Number.isFinite(minutes)) {
        calendarScroller.current.scrollTop = 8 * 56;
        return;
      }
      calendarScroller.current.scrollTop = Math.max(0, (minutes / 60 - 1) * 56);
    }
  }, [calendarView, mode, workspaceId, loading, projectedAllocations, calendarDays, displayTimezone]);
  useEffect(() => {
    if (selectedItemId || selectedAllocationId) document.querySelector(".roadmap-detail article")?.scrollIntoView({ block: "nearest" });
  }, [selectedItemId, selectedAllocationId, editingItemId, editingAllocationId]);
  function moveCalendar(direction: number) {
    if (calendarView === "Month") {
      const date = new Date(`${calendarAnchor}T12:00:00Z`); date.setUTCDate(1); date.setUTCMonth(date.getUTCMonth() + direction);
      setCalendarAnchor(date.toISOString().slice(0, 10));
    } else setCalendarAnchor(shiftDate(calendarAnchor, direction * (calendarView === "Day" ? 1 : 7)));
    setCalendarAnchorTouched(true);
  }
  const dateLabel = (key: string) => new Date(`${key}T12:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
  function eventButton(allocation: CalendarAllocation) {
    return <button type="button" className="calendar-event" aria-pressed={selectedAllocationId === allocation.id} onClick={() => { setSelectedAllocationId(allocation.id); setEditingAllocationId(null); }} title={`${allocation.title} · ${formatAllocationInstant(allocation.start_instant, allocation.timezone)} (${allocation.timezone})`}>
      <strong>{allocation.title}</strong><span>{new Date(allocation.start_instant).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", timeZone: displayTimezone })} – {new Date(allocation.end_instant).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", timeZone: displayTimezone })}</span>
    </button>;
  }
  return (
    <section className="operator-page roadmap-workspace" data-testid={`development-roadmap-${mode}`}>
      <div className="roadmap-navigation">
        <nav aria-label="Roadmap views"><a aria-current={mode === "timeline" ? "page" : undefined} href="/development/roadmap/timeline">Timeline</a><a aria-current={mode === "calendar" ? "page" : undefined} href="/development/roadmap/calendar">Calendar</a></nav>
      </div>
      {error ? <div role="alert" className="inline-notice inline-notice--error">{error}</div> : null}
      {notice ? <p role="status">{notice}</p> : null}
      {loading ? <p role="status">Loading your planning workspace…</p> : null}
      {!workspaceId ? <p>Select a workspace to start planning.</p> : <div className="roadmap-workbench">
        <div className="roadmap-primary">
          <div className="roadmap-toolbar">
            <strong>{mode === "timeline" ? "Project windows" : "Scheduled time"}</strong>
            <label className="roadmap-search">Search {mode === "timeline" ? "work items" : "events"}<input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={mode === "timeline" ? "Find work…" : "Find an event…"} /></label>
            <button type="button" onClick={() => setShowCreate(!showCreate)} aria-expanded={showCreate}>{showCreate ? "Close new entry" : mode === "timeline" ? "+ Add work item" : "+ Add event"}</button>
          </div>
          {showCreate ? <div className="roadmap-create">{mode === "timeline" ? <>        <form onSubmit={(event) => {
          event.preventDefault();
          const title = newTitle.trim();
          if (!title) return;
          void run(async () => { const created = await createRoadmapItem(workspaceId, title); if (activeWorkspace.current === workspaceId) { setNewTitle(""); setSelectedItemId(created.id); beginRoadmapEdit(created); setShowCreate(false); } });
        }}>
          <label>New work item <input value={newTitle} onChange={(event) => setNewTitle(event.target.value)} /></label>
          <button type="submit" disabled={busy || !newTitle.trim()}>+ Add work item</button>
        </form>
</> : <>        <form onSubmit={(event) => {
          event.preventDefault();
          if (!eventTitle.trim() || !eventStart || !eventEnd || !eventTimezone.trim()) return;
          void run(async () => {
            const created = await createCalendarAllocation(workspaceId, eventItemId || null, eventTitle.trim(), eventStart, eventEnd, eventTimezone.trim());
            if (activeWorkspace.current !== workspaceId) return;
            setSelectedAllocationId(created.id); setShowCreate(false);
            setCalendarAnchor(allocationDateKey(created, displayTimezone));
            setCalendarAnchorTouched(true);
            setEventTitle("");
            setEventStart("");
            setEventEnd("");
          });
        }}>
          <label>Title <input value={eventTitle} onChange={(event) => setEventTitle(event.target.value)} /></label>
          <label>Roadmap item <select value={eventItemId} onChange={(event) => setEventItemId(event.target.value)}><option value="">None</option>{items.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>
          <label>Start <input type="datetime-local" value={eventStart} onChange={(event) => setEventStart(event.target.value)} /></label>
          <label>End <input type="datetime-local" value={eventEnd} onChange={(event) => setEventEnd(event.target.value)} /></label>
          <label>Time zone <input value={eventTimezone} onChange={(event) => setEventTimezone(event.target.value)} /></label>
          <button type="submit" disabled={busy || !eventTitle.trim() || !eventStart || !eventEnd || !eventTimezone.trim()}>+ Add event</button>
        </form></>}</div> : null}
          {mode === "timeline" ? <>
            <div className="roadmap-subtoolbar"><label>Execution status <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="">All work items</option>{ROADMAP_STATUSES.map((status) => <option key={status}>{status}</option>)}</select></label><span>Project windows show intended dates. Schedule working time in Calendar.</span></div>
            {requestedRoadmapItemId ? <p>Opened roadmap item: {openedRoadmapItem?.title ?? "not found in this workspace"}</p> : null}
            <section className="roadmap-timeline-scroll" aria-label="Roadmap project-window timeline" data-testid="roadmap-timeline-projection">
              <div className="roadmap-timeline">
                <div className="roadmap-axis"><span>Work item</span><div>{timelineRange ? Array.from({ length: 5 }, (_, index) => <span key={index}>{dateLabel(shiftDate(timelineRange.first, Math.round(index * (timelineRange.days - 1) / 4)))}</span>) : <span>Choose a work item to set its project window</span>}</div></div>
                {visibleItems.map((item) => {
                  const start = item.window_start_date ?? item.window_end_date;
                  const end = item.window_end_date ?? item.window_start_date;
                  const left = timelineRange && start ? dayOffset(start, timelineRange.first) / timelineRange.days * 100 : 0;
                  const width = timelineRange && start && end ? (dayOffset(end, start) + 1) / timelineRange.days * 100 : 0;
                  return <div className="roadmap-lane" key={item.id} data-status={item.status}>
                    <button type="button" className="roadmap-lane-label" aria-pressed={selectedItemId === item.id} onClick={() => {setSelectedItemId(item.id); setEditingItemId(null);}}><strong>{item.title}</strong><small>{item.status} · {allocationsByItem.get(item.id) ?? 0} scheduled blocks</small></button>
                    <div className="roadmap-track">{start && end ? <button className="roadmap-window" type="button" data-testid="roadmap-timeline-window" style={{ left: `${left}%`, width: `${width}%` }} title={`${item.title}: ${start} to ${end}`} onClick={() => {setSelectedItemId(item.id); setEditingItemId(null);}}><span>{item.title}</span><small>{dateLabel(start)} → {dateLabel(end)}</small></button> : <button type="button" className="roadmap-unscheduled" onClick={() => {setSelectedItemId(item.id); beginRoadmapEdit(item);}}>Set project window</button>}</div>
                  </div>;
                })}
                {!visibleItems.length ? <p className="roadmap-empty">{items.length ? "No work items match your search and status." : "What would you like to make progress on? Add your first work item."}</p> : null}
              </div>
            </section>
            <details className="roadmap-execution" data-testid="roadmap-execution-status"><summary>Execution status · {items.length} work items</summary><div className="roadmap-status-columns">{ROADMAP_STATUSES.map((status) => <div key={status}><strong>{status} · {items.filter((item) => item.status === status).length}</strong>{items.filter((item) => item.status === status).map((item) => <button key={item.id} type="button" onClick={() => {setSelectedItemId(item.id); setEditingItemId(null);}}>{item.title}</button>)}</div>)}</div></details>
        <div className="roadmap-detail" aria-label="Selected work item">
          {items.filter((item) => item.id === selectedItemId).map((item) => <article key={item.id} id={`roadmap-item-${item.id}`} className="operator-card" data-testid="roadmap-item" data-opened={requestedRoadmapItemId === item.id ? "true" : undefined}>
            {editingItemId === item.id ? <form onSubmit={(event) => {
              event.preventDefault();
              void run(async () => {
                await updateRoadmapItem(item, {
                  title: editTitle.trim(),
                  window_start_date: editWindowStart || null,
                  window_end_date: editWindowEnd || null,
                  done_when: editDoneWhen.trim() || null
                });
                setEditingItemId(null);
              });
            }}>
              <label>Edit title <input value={editTitle} onChange={(event) => setEditTitle(event.target.value)} /></label>
              <label>Window start <input type="date" value={editWindowStart} onChange={(event) => setEditWindowStart(event.target.value)} /></label>
              <label>Window end <input type="date" value={editWindowEnd} onChange={(event) => setEditWindowEnd(event.target.value)} /></label>
              <label>Done when <input value={editDoneWhen} onChange={(event) => setEditDoneWhen(event.target.value)} /></label>
              <button type="submit" disabled={busy || !editTitle.trim()}>Save work item</button>
              <button type="button" disabled={busy} onClick={() => setEditingItemId(null)}>Cancel</button>
            </form> : <>
              <div><strong>{item.title}</strong><p>{item.item_type} · {item.priority}</p></div>
              <p>Status: <strong>{item.status}</strong></p>
              <p>Project window: {item.window_start_date ?? "unset"} → {item.window_end_date ?? "unset"}</p>
              <p>Scheduled blocks: {allocationsByItem.get(item.id) ?? 0}</p>
              {item.done_when ? <p>Done when: {item.done_when} · {item.done_when_satisfied ? "satisfied" : "not satisfied"}</p> : null}
              <label>Lifecycle status <select value={item.status} disabled={busy} onChange={(event) => void run(async () => { await updateRoadmapItem(item, { status: event.target.value }); })}>{ROADMAP_STATUSES.map((status) => <option key={status} value={status}>{status}</option>)}</select></label>
              <div>
                <button type="button" disabled={busy} onClick={() => beginRoadmapEdit(item)}>Edit work item</button>
                {item.status !== "Done" && item.status !== "Cancelled" ? <button type="button" disabled={busy} onClick={() => void run(async () => { await updateRoadmapItem(item, { status: "Done" }); })}>Mark Done</button> : null}
                <button type="button" disabled={busy} onClick={() => void run(async () => { if (window.confirm(`Delete “${item.title}”? Linked calendar events and dependencies must be removed first.`)) { await deleteRoadmapItem(item); if (activeWorkspace.current === workspaceId) setSelectedItemId(null); } })}>Delete work item</button>
                <a href={`/development/roadmap/calendar?roadmap_item_id=${encodeURIComponent(item.id)}`}>Schedule in Calendar</a>
              </div>
            </>}
          </article>)}

        </div>
          </> : <>
            <div className="roadmap-subtoolbar" role="region" aria-label="Calendar projection"><div className="calendar-modes">{(["Day", "Week", "Month", "Agenda"] as CalendarView[]).map((view) => <button key={view} type="button" aria-pressed={calendarView === view} onClick={() => setCalendarView(view)}>{view}</button>)}</div><div className="calendar-date-nav"><button type="button" aria-label="Previous period" onClick={() => moveCalendar(-1)}>‹</button><label>View date <input type="date" required value={calendarAnchor} onChange={(event) => { if (event.target.value) setCalendarAnchor(event.target.value); setCalendarAnchorTouched(true); }} /></label><button type="button" aria-label="Next period" onClick={() => moveCalendar(1)}>›</button><button type="button" onClick={() => {setCalendarAnchor(dateKeyForInstant(new Date(), displayTimezone)); setCalendarAnchorTouched(true);}}>Today</button></div></div>
            <p className="calendar-zone">All grid times: {displayTimezone}. Each event keeps its original time zone.</p>
            <div className={`calendar-projection calendar-projection--${calendarView.toLowerCase()}`} data-calendar-view={calendarView.toLowerCase()} ref={calendarScroller}>
              {!projectedAllocations.length ? <p className="roadmap-empty calendar-empty">No scheduled time in this {calendarView.toLowerCase()}. Add an event or choose another date.</p> : null}
              {calendarView === "Agenda" ? projectedAllocations.map((allocation) => <div className="calendar-agenda-row" key={allocation.id}><time>{dateLabel(allocationDateKey(allocation, displayTimezone))}</time>{eventButton(allocation)}</div>) : calendarView === "Month" ? <div className="calendar-month">{calendarDays.map((day) => <div className="calendar-month-day" key={day} data-outside-month={day.slice(0,7) !== calendarAnchor.slice(0,7)}><button className="calendar-day-heading" type="button" onClick={() => {setCalendarAnchor(day); setCalendarView("Day"); setCalendarAnchorTouched(true);}}>{new Date(`${day}T12:00:00Z`).toLocaleDateString(undefined, {weekday: "short", day: "numeric", timeZone: "UTC"})}</button>{projectedAllocations.filter((allocation) => allocationOverlapsDateRange(allocation, day, day, displayTimezone)).map((allocation) => <div key={allocation.id}>{eventButton(allocation)}</div>)}</div>)}</div> : <div className="calendar-time-grid" style={{ gridTemplateColumns: `48px repeat(${calendarDays.length}, minmax(105px, 1fr))` }}>
                <div className="calendar-hour-column"><div className="calendar-day-heading">Time</div>{Array.from({length: 24}, (_, hour) => <time key={hour}>{`${String(hour).padStart(2,"0")}:00`}</time>)}</div>
                {calendarDays.map((day) => {
                  const events = projectedAllocations.filter((allocation) => allocationOverlapsDateRange(allocation, day, day, displayTimezone));
                  const segments = events.map((allocation) => {
                    const end = zonedParts(allocation.end_instant, displayTimezone);
                    const from = renderedStartMinute(allocation, day, displayTimezone);
                    const until = dateKeyForInstant(allocation.end_instant, displayTimezone) > day ? 1440 : Number(end.hour)*60+Number(end.minute);
                    const duration = (new Date(allocation.end_instant).getTime() - new Date(allocation.start_instant).getTime()) / 60000;
                    return {allocation, from, until: Math.min(1440, Math.max(from + 15, until <= from ? from + duration : until)), lane: 0, lanes: 1};
                  }).sort((a,b) => a.from-b.from || a.until-b.until);
                  let group: typeof segments = [], laneEnds: number[] = [], groupEnd = -1;
                  const finishGroup = () => { for (const entry of group) entry.lanes = laneEnds.length; group = []; laneEnds = []; };
                  for (const segment of segments) {
                    if (segment.from >= groupEnd) finishGroup();
                    let lane = laneEnds.findIndex((end) => end <= segment.from); if (lane < 0) lane = laneEnds.length;
                    laneEnds[lane] = segment.until; segment.lane = lane; group.push(segment); groupEnd = Math.max(groupEnd, segment.until);
                  }
                  finishGroup();
                  return <div key={day} className="calendar-day-column"><div className="calendar-day-heading">{new Date(`${day}T12:00:00Z`).toLocaleDateString(undefined, {weekday:"short", day:"numeric", month:"short", timeZone:"UTC"})}</div><div className="calendar-day-hours">{segments.map(({allocation,from,until,lane,lanes}) => <div className="calendar-event-position" key={allocation.id} style={{top:`${from/60*56}px`,height:`${(until-from)/60*56}px`,left:`${lane/lanes*100}%`,width:`${100/lanes}%`}}>{eventButton(allocation)}</div>)}</div></div>;
                })}
              </div>}
            </div>

        <div className="roadmap-detail" aria-label="Selected calendar event">
          {allocations.filter((allocation) => allocation.id === selectedAllocationId).map((allocation) => <article key={allocation.id} className="operator-card" data-testid="calendar-allocation">
            {editingAllocationId === allocation.id ? <form onSubmit={(event) => {
              event.preventDefault();
              void run(async () => {
                await updateCalendarAllocation(allocation, {
                  title: editEventTitle.trim(),
                  roadmap_item_id: editEventItemId || null,
                  start_local: editEventStart,
                  end_local: editEventEnd,
                  timezone: editEventTimezone.trim()
                });
                setCalendarAnchor(editEventStart.slice(0, 10));
                setCalendarAnchorTouched(true);
                setEditingAllocationId(null);
              });
            }}>
              <label>Edit event title <input value={editEventTitle} onChange={(event) => setEditEventTitle(event.target.value)} /></label>
              <label>Edit Roadmap item <select value={editEventItemId} onChange={(event) => setEditEventItemId(event.target.value)}><option value="">None</option>{items.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>
              <label>Edit start <input type="datetime-local" value={editEventStart} onChange={(event) => setEditEventStart(event.target.value)} /></label>
              <label>Edit end <input type="datetime-local" value={editEventEnd} onChange={(event) => setEditEventEnd(event.target.value)} /></label>
              <label>Edit time zone <input value={editEventTimezone} onChange={(event) => setEditEventTimezone(event.target.value)} /></label>
              <button type="submit" disabled={busy || !editEventTitle.trim() || !editEventStart || !editEventEnd || !editEventTimezone.trim()}>Save event</button>
              <button type="button" disabled={busy} onClick={() => setEditingAllocationId(null)}>Cancel</button>
            </form> : <>
              <strong>{allocation.title}</strong>
              <p>{formatAllocationInstant(allocation.start_instant, allocation.timezone)} → {formatAllocationInstant(allocation.end_instant, allocation.timezone)}</p>
              <p>Time zone: {allocation.timezone}</p>
              <p>Roadmap item: {items.find((item) => item.id === allocation.roadmap_item_id)?.title ?? "None"}</p>
              <div>
                <button type="button" disabled={busy} onClick={() => beginAllocationEdit(allocation)}>Edit event</button>
                <button type="button" disabled={busy} onClick={() => void run(async () => { if (window.confirm(`Delete “${allocation.title}” from the calendar?`)) { await deleteCalendarAllocation(allocation); if (activeWorkspace.current === workspaceId) setSelectedAllocationId(null); } })}>Delete event</button>
                {allocation.roadmap_item_id ? <a href={`/development/roadmap/timeline?roadmap_item_id=${encodeURIComponent(allocation.roadmap_item_id)}`}>Open roadmap item</a> : null}
              </div>
            </>}
          </article>)}

        </div>
          </>}
        </div>
        {jarvis ? <aside className="roadmap-jarvis">{jarvis}</aside> : null}
      </div>}
    </section>
  );
}
