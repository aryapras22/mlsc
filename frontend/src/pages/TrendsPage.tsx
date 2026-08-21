import { useParams } from "react-router-dom";
import { useFilterState } from "@/lib/filter-state";
import { useEvents } from "@/api/queries";
import { LoadStateView } from "@/components/LoadStateView";

/** Requirement 5: each entry names what fired, its method and strength,
 * and reaches its evidence in one click — evidence document ids link
 * straight into the document explorer with that document pre-filtered. */
export function TrendsPage() {
  const { monitorId = "" } = useParams<{ monitorId: string }>();
  const [filters] = useFilterState();

  const events = useEvents(monitorId, { start: filters.start, end: filters.end });

  return (
    <LoadStateView
      state={events.data}
      render={(data) => (
        <ul className="space-y-3">
          {data.map((event) => (
            <li key={event.id} className="border-b pb-2">
              <div className="text-sm font-medium">
                {event.kind} — {event.detected_on}
              </div>
              <div className="text-muted-foreground text-xs">
                method: {event.method} · severity: {event.severity.toFixed(2)}
              </div>
              <div className="mt-1 flex gap-2 text-xs">
                {event.evidence_ids.map((documentId) => (
                  <a
                    key={documentId}
                    className="underline"
                    href={`../explorer?topic_id=${event.topic_id}`}
                  >
                    evidence
                  </a>
                ))}
              </div>
            </li>
          ))}
          {data.length === 0 && <li className="text-muted-foreground text-sm">No changes detected in this range.</li>}
        </ul>
      )}
    />
  );
}
