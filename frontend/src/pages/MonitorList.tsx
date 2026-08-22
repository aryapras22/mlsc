import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { listMonitors } from "@/api/resources";
import { load } from "@/api/client";
import { LoadStateView } from "@/components/LoadStateView";

export function MonitorList() {
  const query = useQuery({
    queryKey: ["monitors"],
    queryFn: () => load(listMonitors),
  });

  return (
    <div className="p-4">
      <h1 className="mb-4 text-lg font-semibold">Monitors</h1>
      <LoadStateView
        state={query.data ?? { status: "loading" }}
        render={(monitors) => (
          <>
            <ul className="space-y-2">
              {monitors.map((monitor) => (
                <li key={monitor.id}>
                  <Link className="underline" to={`/monitors/${monitor.id}`}>
                    {monitor.name}
                  </Link>
                </li>
              ))}
            </ul>
            {monitors.length === 0 && (
              <p className="text-muted-foreground text-sm">No monitors yet.</p>
            )}
          </>
        )}
      />
    </div>
  );
}
