import { useEffect, useMemo, useState } from "react";

import type { Appointment, CalendarEvent } from "@/api/types";
import type { CalendarItem } from "@/calendar/item";
import { toCalendarItem } from "@/owner/calendarItem";
import { path } from "@/lib/api";
import { scheduleEvents } from "@/lib/scheduleEvents";
import { useOwner } from "@/owner/OwnerContext";

import { ScheduleList } from "./ScheduleList";

type Detail = { clientId: string } | { error: true };

/** The owner's Schedule view: the shared list, with client names resolved from owner-only reads. */
export function OwnerScheduleList({ date, days, events, zone, selectedId, onSelect, onLoadMore }: {
  date: string; days: number; events: CalendarEvent[]; zone: string; selectedId: string | null;
  onSelect: (id: string, element: HTMLElement) => void;
  onLoadMore: () => void;
}) {
  const { api, data, stamp } = useOwner();
  const pending = useMemo(() => new Map(data.requests.map((request) =>
    [request.appointment_id, request.client_id])), [data.requests]);
  const items = useMemo(() => events.map(toCalendarItem), [events]);
  const groups = useMemo(() => scheduleEvents(items, date, days, zone, new Set(pending.keys())),
    [items, date, days, zone, pending]);
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
  const clientLabel = (event: CalendarItem) => {
    const clientId = pending.get(event.event_id);
    if (clientId) return names.get(clientId) ?? "Client unavailable";
    const detail = details.key === detailsKey ? details.values.get(event.event_id) : undefined;
    if (!detail) return "Loading client…";
    if ("error" in detail) return "Client unavailable";
    return names.get(detail.clientId) ?? "Client unavailable";
  };

  return (
    <ScheduleList groups={groups} zone={zone} selectedId={selectedId} onSelect={onSelect}
      onLoadMore={onLoadMore} itemTitle={clientLabel} />
  );
}
