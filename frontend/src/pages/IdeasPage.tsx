import { useState } from "react";
import { useParams } from "react-router-dom";
import { useFilterState } from "@/lib/filter-state";
import { useInsights, useRecordJudgement } from "@/api/queries";
import { LoadStateView } from "@/components/LoadStateView";
import type { InsightView } from "@/api/resources";

/** Requirement 6: opportunity cards, expanding to evidence, with the
 * useful control posting back. A failed submission keeps the card and
 * shows the failure rather than looking recorded when it was not
 * (design.md, "Failure strategy": "a card that looks recorded and was not
 * is a small lie"). */
export function IdeasPage() {
  const { monitorId = "" } = useParams<{ monitorId: string }>();
  const [filters] = useFilterState();

  const insights = useInsights(monitorId, { start: filters.start, end: filters.end, kind: "opportunity" });

  return (
    <LoadStateView
      state={insights.data}
      render={(data) => (
        <div className="space-y-4">
          {data.map((insight) => (
            <OpportunityCard key={insight.id} insight={insight} />
          ))}
          {data.length === 0 && <p className="text-muted-foreground text-sm">No opportunities generated in this range.</p>}
        </div>
      )}
    />
  );
}

function OpportunityCard({ insight }: { insight: InsightView }) {
  const [expanded, setExpanded] = useState(false);
  const judgement = useRecordJudgement();

  return (
    <div className="rounded border p-4">
      <h3 className="font-medium">{insight.title}</h3>
      <p className="text-sm">{insight.body}</p>
      <button type="button" className="mt-2 text-sm underline" onClick={() => setExpanded((value) => !value)}>
        {expanded ? "Hide evidence" : "Show evidence"}
      </button>
      {expanded && (
        <ul className="mt-2 space-y-1 text-xs">
          {insight.evidence_ids.map((id) => (
            <li key={id}>{id}</li>
          ))}
        </ul>
      )}
      <div className="mt-3 flex gap-2 text-sm">
        <button
          type="button"
          className="rounded border px-2 py-1"
          disabled={judgement.isPending}
          onClick={() => judgement.mutate({ insightId: insight.id, useful: true })}
        >
          Useful
        </button>
        <button
          type="button"
          className="rounded border px-2 py-1"
          disabled={judgement.isPending}
          onClick={() => judgement.mutate({ insightId: insight.id, useful: false })}
        >
          Not useful
        </button>
        {judgement.isSuccess && <span className="text-green-700">Recorded.</span>}
        {judgement.isError && (
          <span className="text-red-600">Failed to record — try again.</span>
        )}
      </div>
    </div>
  );
}
