import { useEffect, useMemo, useState } from "react";

import type { Appointment, CalendarEvent } from "@/api/types";
import { path } from "@/lib/api";
import { scheduleEvents } from "@/lib/scheduleEvents";
import { dayTitle, localTime, statusLabel } from "@/lib/time";
import { useOwner } from "@/owner/OwnerContext";

type Detail = { clientId: string } | { error: true };

export function ScheduleList({ date, days, events, zone, selectedId, onSelect, onLoadMore }: {
  date: string; days: number; events: CalendarEvent[]; zone: string; selectedId: string | null;
  onSelect: (id: string, element: HTMLElement) => void;
  onLoadMore: () => void;
}) {
  const { api, data, stamp } = useOwner();
  const pending = useMemo(() => new Map(data.requests.map((request) =>
    [request.appointment_id, request.client_id])), [data.requests]);
  const groups = useMemo(() => scheduleEvents(events, date, days, zone, new Set(pending.keys())),
    [events, date, days, zone, pending]);
  const ids = groups.flatMap((group) => group.events.map((event) => event.event_id));
  const signature = ids.join("\0");
  const detailsKey = `${stamp.version}:${signature}`;
  const [details, setDetails] = useState<{ key: string; values: Map<string, Detail> }>({
    key: "", values: new Map(),
  });

  // Appointment details are owner-only. Fetch just the visible range; old responses cannot
  // replace a newer date/range or refreshed snapshot.
  useEffect(() => {
    let current = true;
    const needsRead = ids.filter((id) => !pending.has(id));
    const load = async () => {
      // Bound concurrent owner reads while names are progressively revealed.
      for (let offset = 0; offset < needsRead.length && current; offset += 8) {
        const batch = await Promise.all(needsRead.slice(offset, offset + 8).map(async (id): Promise<[string, Detail]> => {
          try {
            const appointment = await api.get<Appointment>(path`/appointments/${id}`);
            return [id, { clientId: appointment.client_id }];
          } catch {
            return [id, { error: true }];
          }
        }));
        if (current) setDetails((previous) => ({ key: detailsKey,
          values: new Map([...(previous.key === detailsKey ? previous.values : new Map()), ...batch]),
        }));
      }
    };
    void load();
    return () => { current = false; };
    // The signature is a stable summary of visible events; details refresh with the snapshot.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, signature, stamp.version, pending]);

  const names = new Map(data.clients.map((client) => [client.client_id, client.name]));
  const clientLabel = (event: CalendarEvent) => {
    const clientId = pending.get(event.event_id);
    if (clientId) return names.get(clientId) ?? "Client unavailable";
    const detail = details.key === detailsKey ? details.values.get(event.event_id) : undefined;
    if (!detail) return "Loading client…";
    if ("error" in detail) return "Client unavailable";
    return names.get(detail.clientId) ?? "Client unavailable";
  };

  return (
    <div className="schedule-list" role="region" tabIndex={0} data-calendar-scroll="" data-popover-clip=""
      aria-label="Schedule agenda">
      {groups.length === 0 && <p className="empty" role="status">No confirmed visits or pending requests in this range.</p>}
      {groups.map((group) => (
        <section className="schedule-day" key={group.date} aria-label={dayTitle(group.date)}>
          <h3>{dayTitle(group.date)}</h3>
          <div className="schedule-day-items">
            {group.events.map((event) => {
              const client = clientLabel(event);
              const status = statusLabel(event.status);
              const time = localTime(event.start_at, zone);
              return (
                <button type="button" key={event.event_id} data-event-id={event.event_id} data-popover-anchor=""
                  aria-haspopup="dialog" aria-current={event.event_id === selectedId ? "true" : undefined}
                  aria-label={`${dayTitle(group.date)}, ${time}, ${status}, ${client}`}
                  className={`schedule-item ${event.status === "CONFIRMED" ? "confirmed" : "pending"}${event.event_id === selectedId ? " selected" : ""}`}
                  onClick={(click) => onSelect(event.event_id, click.currentTarget)}>
                  <span className="schedule-item-time">{time}</span>
                  <span className="schedule-item-client">{client}</span>
                  <span className="schedule-item-status">{status}</span>
                </button>
              );
            })}
          </div>
        </section>
      ))}
      <button type="button" className="schedule-load-more" onClick={onLoadMore}>Load more</button>
    </div>
  );
}
