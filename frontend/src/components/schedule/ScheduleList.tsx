import type { CalendarItem } from "@/calendar/item";
import type { ScheduleDay } from "@/lib/scheduleEvents";
import { dayTitle, localTime, statusLabel } from "@/lib/time";

/**
 * The agenda: items already grouped by day (see `scheduleEvents`). `itemTitle` supplies the
 * line shown beside the time, so the owner can show a client's name and a client can show
 * their own booking, without this list knowing either.
 */
export function ScheduleList({ groups, zone, selectedId, onSelect, onLoadMore, itemTitle }: {
  groups: ScheduleDay[]; zone: string; selectedId: string | null;
  onSelect: (id: string, element: HTMLElement) => void;
  onLoadMore: () => void;
  itemTitle: (item: CalendarItem) => string;
}) {
  return (
    <div className="schedule-list" role="region" tabIndex={0} data-calendar-scroll="" data-popover-clip=""
      aria-label="Schedule agenda">
      {groups.length === 0 && <p className="empty" role="status">No confirmed visits or pending requests in this range.</p>}
      {groups.map((group) => (
        <section className="schedule-day" key={group.date} aria-label={dayTitle(group.date)}>
          <h3>{dayTitle(group.date)}</h3>
          <div className="schedule-day-items">
            {group.events.map((event) => {
              const client = itemTitle(event);
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
