import { useParams } from "react-router-dom";
import { useFilterState } from "@/lib/filter-state";
import { useTimeseries, useEvents } from "@/api/queries";
import { LoadStateView } from "@/components/LoadStateView";
import { SeriesChart } from "@/components/charts/SeriesChart";

/** Requirement 4: one topic's timeline with detected changes marked on
 * it. App-version annotations are deferred — no endpoint currently
 * exposes released app versions, so this renders only the events the API
 * actually serves rather than inventing a second data source. */
export function TopicDetailPage() {
  const { monitorId = "", topicId = "" } = useParams<{ monitorId: string; topicId: string }>();
  const [filters] = useFilterState();
  const range = { start: filters.start, end: filters.end };

  const volume = useTimeseries(monitorId, { ...range, metric: "volume", topic_id: topicId });
  const events = useEvents(monitorId, range);

  return (
    <div className="space-y-6">
      <section>
        <h2 className="mb-2 font-medium">Volume</h2>
        <LoadStateView state={volume.data} render={(series) => <SeriesChart points={series.points} label="volume" />} />
      </section>

      <section>
        <h2 className="mb-2 font-medium">Detected changes</h2>
        <LoadStateView
          state={events.data}
          render={(allEvents) => {
            const topicEvents = allEvents.filter((event) => event.topic_id === topicId);
            return (
              <ul className="space-y-1 text-sm">
                {topicEvents.map((event) => (
                  <li key={event.id}>
                    {event.detected_on} — {event.kind} via {event.method} (severity {event.severity.toFixed(2)})
                  </li>
                ))}
                {topicEvents.length === 0 && (
                  <li className="text-muted-foreground">No changes detected for this topic in this range.</li>
                )}
              </ul>
            );
          }}
        />
      </section>
    </div>
  );
}
