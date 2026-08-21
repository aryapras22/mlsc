import { Link, useParams } from "react-router-dom";
import { useFilterState } from "@/lib/filter-state";
import { useTopicRanking } from "@/api/queries";
import { LoadStateView } from "@/components/LoadStateView";

/** Requirement 3: topics ranked by movement, with sentiment and a
 * corroboration badge from the topic's own breadth ratio. */
export function TopicsPage() {
  const { monitorId = "" } = useParams<{ monitorId: string }>();
  const [filters] = useFilterState();

  const ranking = useTopicRanking(monitorId, { start: filters.start, end: filters.end });

  return (
    <LoadStateView
      state={ranking.data}
      render={(data) => (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1">Topic</th>
              <th>Docs</th>
              <th>Share</th>
              <th>Sentiment</th>
              <th>Trend score</th>
              <th>Corroboration</th>
            </tr>
          </thead>
          <tbody>
            {data.entries.map((entry) => (
              <tr key={entry.topic_id} className="border-t">
                <td className="py-1">
                  <Link className="underline" to={`../topics/${entry.topic_id}${location.search}`}>
                    {entry.label}
                  </Link>
                </td>
                <td>{entry.doc_count}</td>
                <td>{(entry.doc_count_share * 100).toFixed(0)}%</td>
                <td>{entry.sentiment_mean !== null ? entry.sentiment_mean.toFixed(2) : "—"}</td>
                <td>{entry.trend_score !== null ? entry.trend_score.toFixed(2) : "—"}</td>
                <td>{entry.breadth_ratio !== null ? `${(entry.breadth_ratio * 100).toFixed(0)}%` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    />
  );
}
