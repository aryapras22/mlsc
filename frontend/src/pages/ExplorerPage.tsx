import { useParams, useSearchParams } from "react-router-dom";
import { useDocuments } from "@/api/queries";
import { LoadStateView } from "@/components/LoadStateView";

/** Requirement 7: search and filter collected documents, arriving here
 * pre-filtered from any figure elsewhere via the URL's own query params
 * (design.md, "Domain shapes": `DrillTarget`). */
export function ExplorerPage() {
  const { monitorId = "" } = useParams<{ monitorId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();

  const topicId = searchParams.get("topic_id") ?? undefined;
  const source = searchParams.get("source") ?? undefined;
  const cursor = searchParams.get("cursor") ?? undefined;

  const documents = useDocuments(monitorId, { topic_id: topicId, source: source as never, cursor });

  return (
    <div>
      <div className="mb-4 flex gap-2 text-sm">
        {topicId && (
          <span className="rounded bg-gray-100 px-2 py-1">
            topic: {topicId}{" "}
            <button
              type="button"
              onClick={() => {
                const next = new URLSearchParams(searchParams);
                next.delete("topic_id");
                setSearchParams(next);
              }}
            >
              ×
            </button>
          </span>
        )}
        {source && <span className="rounded bg-gray-100 px-2 py-1">source: {source}</span>}
      </div>

      <LoadStateView
        state={documents.data}
        render={(page) => (
          <div className="space-y-2">
            {page.items.map((document) => (
              <div key={document.id} className="border-b pb-2 text-sm">
                <div className="text-muted-foreground text-xs">
                  {document.source_name} · {document.published_at}
                </div>
                <div>{document.body}</div>
              </div>
            ))}
            {page.items.length === 0 && <p className="text-muted-foreground text-sm">No documents match this filter.</p>}
            {page.cursor && (
              <button
                type="button"
                className="text-sm underline"
                onClick={() => {
                  const next = new URLSearchParams(searchParams);
                  next.set("cursor", page.cursor!);
                  setSearchParams(next);
                }}
              >
                Load more
              </button>
            )}
          </div>
        )}
      />
    </div>
  );
}
