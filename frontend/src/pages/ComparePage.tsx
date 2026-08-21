import { useParams } from "react-router-dom";
import { useFilterState } from "@/lib/filter-state";
import { useEntityComparison } from "@/api/queries";
import { LoadStateView } from "@/components/LoadStateView";

/** Requirement 8: share of voice and sentiment difference across the
 * monitor's own entities (learn.md, "Share of voice": "a composition
 * measure, so it always sums to one"). */
export function ComparePage() {
  const { monitorId = "" } = useParams<{ monitorId: string }>();
  const [filters] = useFilterState();

  const comparison = useEntityComparison(monitorId, { start: filters.start, end: filters.end });

  return (
    <LoadStateView
      state={comparison.data}
      render={(data) => (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1">Entity</th>
              <th>Documents</th>
              <th>Share of voice</th>
              <th>Sentiment</th>
            </tr>
          </thead>
          <tbody>
            {data.entries.map((entry) => (
              <tr key={entry.entity_id} className="border-t">
                <td className="py-1">{entry.entity_id}</td>
                <td>{entry.doc_count}</td>
                <td>{(entry.share_of_voice * 100).toFixed(0)}%</td>
                <td>{entry.sentiment_mean !== null ? entry.sentiment_mean.toFixed(2) : "—"}</td>
              </tr>
            ))}
            {data.entries.length === 0 && (
              <tr>
                <td colSpan={4} className="text-muted-foreground py-2">
                  No entities to compare in this range.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    />
  );
}
