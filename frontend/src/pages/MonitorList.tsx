import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { listMonitors, type Monitor, type MonitorCreate, type TargetType } from "@/api/resources";
import { useCreateMonitor, useUpdateMonitor } from "@/api/queries";
import { load } from "@/api/client";
import { LoadStateView } from "@/components/LoadStateView";

const LOCAL_TIMEZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;

export function MonitorList() {
  const query = useQuery({
    queryKey: ["monitors"],
    queryFn: () => load(listMonitors),
  });

  return (
    <div className="p-4">
      <h1 className="mb-4 text-lg font-semibold">Monitors</h1>
      <CreateMonitorForm />
      <LoadStateView
        state={query.data ?? { status: "loading" }}
        render={(monitors) => (
          <>
            <ul className="space-y-2">
              {monitors.map((monitor) => (
                <MonitorRow key={monitor.id} monitor={monitor} />
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

/**
 * The seed's shape is decided by the target type, and the API rejects the
 * wrong one (mlsc/schemas/monitors.py, `validate_seed`): a product carries a
 * non-empty identifier list, a theme carries a description.
 */
function buildSeed(targetType: TargetType, seedInput: string): MonitorCreate["seed"] {
  if (targetType === "product") {
    return {
      identifiers: seedInput
        .split(",")
        .map((identifier) => identifier.trim())
        .filter(Boolean),
    };
  }
  return { description: seedInput.trim() };
}

function CreateMonitorForm() {
  const [name, setName] = useState("");
  const [targetType, setTargetType] = useState<TargetType>("product");
  const [seedInput, setSeedInput] = useState("");
  const [cronExpression, setCronExpression] = useState("0 3 * * *");
  const [timezone, setTimezone] = useState(LOCAL_TIMEZONE);
  const [retentionDays, setRetentionDays] = useState(90);
  const create = useCreateMonitor();

  return (
    <form
      className="mb-6 grid max-w-xl gap-2 border p-4"
      onSubmit={(event) => {
        event.preventDefault();
        create.mutate(
          {
            name,
            target_type: targetType,
            seed: buildSeed(targetType, seedInput),
            cron_expression: cronExpression,
            timezone,
            retention_days: retentionDays,
          },
          {
            onSuccess: () => {
              setName("");
              setSeedInput("");
            },
          }
        );
      }}
    >
      <h2 className="font-semibold">New monitor</h2>

      <label htmlFor="monitor-name">Name</label>
      <input
        id="monitor-name"
        className="border px-2 py-1"
        value={name}
        onChange={(event) => setName(event.target.value)}
        required
      />

      <label htmlFor="monitor-target-type">Target type</label>
      <select
        id="monitor-target-type"
        className="border px-2 py-1"
        value={targetType}
        onChange={(event) => setTargetType(event.target.value as TargetType)}
      >
        <option value="product">product</option>
        <option value="theme">theme</option>
      </select>

      <label htmlFor="monitor-seed">
        {targetType === "product" ? "Identifiers (comma separated)" : "Description"}
      </label>
      <input
        id="monitor-seed"
        className="border px-2 py-1"
        value={seedInput}
        onChange={(event) => setSeedInput(event.target.value)}
        placeholder={targetType === "product" ? "com.roblox.client" : "cloud gaming latency"}
        required
      />

      <label htmlFor="monitor-cron">Schedule (five-field cron)</label>
      <input
        id="monitor-cron"
        className="border px-2 py-1"
        value={cronExpression}
        onChange={(event) => setCronExpression(event.target.value)}
        required
      />

      <label htmlFor="monitor-timezone">Timezone</label>
      <input
        id="monitor-timezone"
        className="border px-2 py-1"
        value={timezone}
        onChange={(event) => setTimezone(event.target.value)}
        required
      />

      <label htmlFor="monitor-retention">Retention days</label>
      <input
        id="monitor-retention"
        type="number"
        min={1}
        className="border px-2 py-1"
        value={retentionDays}
        onChange={(event) => setRetentionDays(Number(event.target.value))}
        required
      />

      <button className="mt-2 border px-3 py-1" type="submit" disabled={create.isPending}>
        {create.isPending ? "Creating…" : "Create monitor"}
      </button>

      {create.isError && (
        <p role="alert" className="text-sm text-red-600">
          {create.error.message}
        </p>
      )}
    </form>
  );
}

function MonitorRow({ monitor }: { monitor: Monitor }) {
  const update = useUpdateMonitor();

  return (
    <li className="flex items-center gap-3">
      <Link className="underline" to={`/monitors/${monitor.id}`}>
        {monitor.name}
      </Link>
      <span className="text-muted-foreground text-sm">{monitor.status}</span>
      {monitor.status !== "archived" && (
        <>
          <button
            className="border px-2 text-sm"
            disabled={update.isPending}
            onClick={() =>
              update.mutate({
                monitorId: monitor.id,
                body: { status: monitor.status === "active" ? "paused" : "active" },
              })
            }
          >
            {monitor.status === "active" ? "Pause" : "Resume"}
          </button>
          <button
            className="border px-2 text-sm"
            disabled={update.isPending}
            onClick={() => update.mutate({ monitorId: monitor.id, body: { status: "archived" } })}
          >
            Archive
          </button>
        </>
      )}
      {update.isError && (
        <span role="alert" className="text-sm text-red-600">
          {update.error.message}
        </span>
      )}
    </li>
  );
}
