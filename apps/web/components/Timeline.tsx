import type { ApplicationEventView } from "@/lib/service/types";
import { formatClock } from "@/lib/format";

export function Timeline({ events }: { events: ApplicationEventView[] }) {
  if (events.length === 0) {
    return <p className="timeline__empty">No events yet.</p>;
  }
  return (
    <ol className="timeline">
      {events.map((event) => (
        <li key={event.id} className={`timeline__item tone-${event.tone}`}>
          <time className="timeline__time" dateTime={event.at}>
            {formatClock(event.at)}
          </time>
          <span className="timeline__mark" aria-hidden="true" />
          <span className="timeline__message">{event.message}</span>
        </li>
      ))}
    </ol>
  );
}
