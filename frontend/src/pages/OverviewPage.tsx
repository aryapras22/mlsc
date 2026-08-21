import { useParams } from "react-router-dom";
import { useFilterState } from "@/lib/filter-state";
import { useOverview, useTimeseries, useTopicRanking, useEvents } from "@/api/queries";
import { LoadStateView } from "@/components/LoadStateView";
import { SeriesChart } from "@/components/charts/SeriesChart";

/** Requirement 1, 2: recent volume, sentiment, rising topics, open alerts,
 * and the source strip on one screen — the sixty-second test's home page. */
export function OverviewPage() {
  const { monitorId = "" } = useParams<{ monitorId: string }>();
  const [filters] = useFilterState();
  const range = { start: filters.start, end: filters.end };

  const overview = useOverview(monitorId, range);
  const volume = useTimeseries(monitorId, { ...range, metric: "volume" });
  const ranking = useTopicRanking(monitorId, range);
  const events = useEvents(monitorId, range);

  return (
    <div className="space-y-6">
      <section>
        <h2 className="mb-2 font-medium">Volume</h2>
        <LoadStateView state={volume.data} render={(series) => <SeriesChart points={series.points} label="volume" />} />
      </section>

      <section>
        <h2 className="mb-2 font-medium">Headline figures</h2>
        <LoadStateView
          state={overview.data}
          render={(data) => (
            <dl className="grid grid-cols-2 gap-4 text-sm">
              {Object.entries(data.headline_figures).map(([key, value]) => (
                <div key={key}>
                  <dt className="text-muted-foreground">{key}</dt>
                  <dd className="text-lg">{value ?? "—"}</dd>
                </div>
              ))}
            </dl>
          )}
        />
      </section>

      <section>
        <h2 className="mb-2 font-medium">Rising topics</h2>
        <LoadStateView
          state={ranking.data}
          render={(data) => (
            <ol className="list-decimal space-y-1 pl-5 text-sm">
              {data.entries.slice(0, 5).map((entry) => (
                <li key={entry.topic_id}>
                  {entry.label} — {entry.doc_count} documents
                  {entry.trend_score !== null && ` — trend score ${entry.trend_score.toFixed(2)}`}
                </li>
              ))}
            </ol>
          )}
        />
      </section>

      <section>
        <h2 className="mb-2 font-medium">Open changes</h2>
        <LoadStateView
          state={events.data}
          render={(data) => (
            <ul className="space-y-1 text-sm">
              {data.slice(0, 5).map((event) => (
                <li key={event.id}>
                  {event.kind} via {event.method} — severity {event.severity.toFixed(2)}
                </li>
              ))}
              {data.length === 0 && <li className="text-muted-foreground">No changes detected this range.</li>}
            </ul>
          )}
        />
      </section>

      <section>
        <h2 className="mb-2 font-medium">Sources</h2>
        <LoadStateView
          state={overview.data}
          render={(data) => (
            <div className="flex gap-4 text-sm">
              <span className="text-green-700">ok: {data.data_quality.sources_ok.join(", ") || "—"}</span>
              <span className="text-red-700">failed: {data.data_quality.sources_failed.join(", ") || "none"}</span>
              {data.data_quality.truncated_days.length > 0 && (
                <span className="text-amber-700">truncated days: {data.data_quality.truncated_days.join(", ")}</span>
              )}
            </div>
          )}
        />
      </section>
    </div>
  );
}
