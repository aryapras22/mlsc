import { useState } from "react";
import { useParams } from "react-router-dom";
import {
  useAttachSource,
  useOverrides,
  useRetentionPreview,
  useRun,
  useRunHistory,
  useRunStats,
  useSources,
  useSubmitOverride,
  useTriggerRun,
  useUpdateSource,
} from "@/api/queries";
import { LoadStateView } from "@/components/LoadStateView";
import type { OverrideKind, OverrideRequest, Source, SourceCreate, SourceName, Stage } from "@/api/resources";

const SOURCE_KINDS: SourceName[] = ["play", "appstore", "discourse", "news", "rss", "hackernews"];

const OVERRIDE_KINDS: OverrideKind[] = ["stage_rerun", "backfill_window", "retention_purge"];

const STAGES: Stage[] = ["clean", "language", "relevance", "duplicate", "embed", "sentiment", "intent"];

const CONFIG_FIELD: Record<SourceName, { label: string; placeholder: string }> = {
  play: { label: "Package id", placeholder: "com.roblox.client" },
  appstore: { label: "App Store numeric id", placeholder: "431946152" },
  discourse: { label: "Forum base URL", placeholder: "https://devforum.roblox.com" },
  rss: { label: "Feed URL", placeholder: "https://blog.example.com/feed.xml" },
  news: { label: "Queries (comma separated)", placeholder: "roblox, voice chat" },
  hackernews: { label: "Queries (comma separated)", placeholder: "roblox" },
};

/** The shape each kind's config needs (mlsc/schemas/sources.py, the
 * per-kind validators `instance_key_for` dispatches to). */
function buildSourceConfig(sourceName: SourceName, configInput: string): SourceCreate["config"] {
  switch (sourceName) {
    case "play":
      return { package_id: configInput.trim() };
    case "appstore":
      return { app_id: configInput.trim() };
    case "discourse":
      return { base_url: configInput.trim() };
    case "rss":
      return { feed_url: configInput.trim() };
    case "news":
    case "hackernews":
      return {
        queries: configInput
          .split(",")
          .map((query) => query.trim())
          .filter(Boolean),
      };
  }
}

/** Requirement 1, 2, 7: attach a source of a chosen kind, see it listed
 * with its allowance and state, enable or disable it. */
export function SettingsPage() {
  const { monitorId = "" } = useParams<{ monitorId: string }>();
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  return (
    <div className="space-y-6">
      <SourcesPanel monitorId={monitorId} />

      <section>
        <h2 className="mb-2 font-medium">Start a run</h2>
        <RunNowButton monitorId={monitorId} onRunStarted={setActiveRunId} />
        {activeRunId && <RunProgressPanel monitorId={monitorId} runId={activeRunId} />}
      </section>

      <RecentRuns monitorId={monitorId} />

      <OverridesPanel monitorId={monitorId} />
    </div>
  );
}

function AttachSourceForm({ monitorId }: { monitorId: string }) {
  const [sourceName, setSourceName] = useState<SourceName>("play");
  const [configInput, setConfigInput] = useState("");
  const [dailyQuota, setDailyQuota] = useState(300);
  const attach = useAttachSource(monitorId);
  const field = CONFIG_FIELD[sourceName];

  return (
    <form
      className="mb-4 grid max-w-md gap-2 border p-4"
      onSubmit={(event) => {
        event.preventDefault();
        attach.mutate(
          {
            source_name: sourceName,
            config: buildSourceConfig(sourceName, configInput),
            daily_quota: dailyQuota,
            enabled: true,
          },
          { onSuccess: () => setConfigInput("") }
        );
      }}
    >
      <h3 className="font-semibold">Attach a source</h3>

      <label htmlFor="source-kind">Kind</label>
      <select
        id="source-kind"
        className="border px-2 py-1"
        value={sourceName}
        onChange={(event) => setSourceName(event.target.value as SourceName)}
      >
        {SOURCE_KINDS.map((kind) => (
          <option key={kind} value={kind}>
            {kind}
          </option>
        ))}
      </select>

      <label htmlFor="source-config">{field.label}</label>
      <input
        id="source-config"
        className="border px-2 py-1"
        value={configInput}
        onChange={(event) => setConfigInput(event.target.value)}
        placeholder={field.placeholder}
        required
      />

      <label htmlFor="source-quota">Daily quota</label>
      <input
        id="source-quota"
        type="number"
        min={1}
        className="border px-2 py-1"
        value={dailyQuota}
        onChange={(event) => setDailyQuota(Number(event.target.value))}
        required
      />

      <button className="mt-2 border px-3 py-1" type="submit" disabled={attach.isPending}>
        {attach.isPending ? "Attaching…" : "Attach source"}
      </button>

      {attach.isError && (
        <p role="alert" className="text-sm text-red-600">
          {attach.error.message}
        </p>
      )}
    </form>
  );
}

function SourceRow({ monitorId, source }: { monitorId: string; source: Source }) {
  const update = useUpdateSource(monitorId);

  return (
    <li className="flex items-center gap-3 text-sm">
      <span className="font-medium">{source.source_name}</span>
      <span className="text-muted-foreground">{source.instance_key}</span>
      <span className="text-muted-foreground">quota {source.daily_quota}/day</span>
      <span>{source.enabled ? "enabled" : "disabled"}</span>
      <button
        className="border px-2 text-sm"
        disabled={update.isPending}
        onClick={() => update.mutate({ sourceId: source.id, body: { enabled: !source.enabled } })}
      >
        {source.enabled ? "Disable" : "Enable"}
      </button>
      {update.isError && (
        <span role="alert" className="text-red-600">
          {update.error.message}
        </span>
      )}
    </li>
  );
}

function SourcesPanel({ monitorId }: { monitorId: string }) {
  const sources = useSources(monitorId);

  return (
    <section>
      <h2 className="mb-2 font-medium">Sources</h2>
      <AttachSourceForm monitorId={monitorId} />
      <LoadStateView
        state={sources.data}
        render={(items) => (
          <>
            <ul className="space-y-1">
              {items.map((source) => (
                <SourceRow key={source.id} monitorId={monitorId} source={source} />
              ))}
            </ul>
            {items.length === 0 && <p className="text-muted-foreground text-sm">No sources attached yet.</p>}
          </>
        )}
      />
    </section>
  );
}

/** Requirement 3, 6: enqueue and return a run id without collecting in the
 * request; a refusal (no enabled source, monitor not active) shows as text
 * rather than a run that silently never appears. */
function RunNowButton({
  monitorId,
  onRunStarted,
}: {
  monitorId: string;
  onRunStarted: (runId: string) => void;
}) {
  const trigger = useTriggerRun(monitorId);

  return (
    <div>
      <button
        className="border px-3 py-1"
        disabled={trigger.isPending}
        onClick={() => trigger.mutate(undefined, { onSuccess: (result) => onRunStarted(result.run_id) })}
      >
        {trigger.isPending ? "Starting…" : "Run now"}
      </button>
      {trigger.isError && (
        <p role="alert" className="mt-2 text-sm text-red-600">
          {trigger.error.message}
        </p>
      )}
    </div>
  );
}

function describeOutcome(row: { kept: number; error: string | null; validation_failed: boolean }): string {
  if (row.validation_failed) return "validation failed";
  if (row.error) return `failed — ${row.error}`;
  return `collected, ${row.kept} kept`;
}

/** Requirement 5: while a run is in flight, the stage it has reached and
 * what each source produced — stops polling once the run reaches a
 * terminal status (design.md, "Domain shapes": `RunProgress`).
 *
 * `pollIntervalMs` is exposed only so a test can poll faster than
 * production's default; `SettingsPage` never sets it. */
export function RunProgressPanel({
  monitorId,
  runId,
  pollIntervalMs,
}: {
  monitorId: string;
  runId: string;
  pollIntervalMs?: number;
}) {
  const run = useRun(runId, { pollIntervalMs });
  const runIsTerminal = run.data.status === "ready" && run.data.data.finished_at !== null;
  const stats = useRunStats(runId, { enabled: !runIsTerminal, pollIntervalMs });
  const sources = useSources(monitorId);
  const sourceNames =
    sources.data.status === "ready"
      ? Object.fromEntries(sources.data.data.map((source) => [source.id, source.source_name]))
      : {};

  return (
    <div className="mt-4 border p-4">
      <LoadStateView
        state={run.data}
        render={(data) => (
          <div className="text-sm">
            <p>
              Run {data.id} — status: <strong>{data.status}</strong>
            </p>
            <ul className="mt-1 ml-4 list-disc">
              {Object.entries(data.stage_status).map(([stage, state]) => (
                <li key={stage}>
                  {stage}: {state}
                </li>
              ))}
            </ul>
          </div>
        )}
      />
      <LoadStateView
        state={stats.data}
        render={(rows) => (
          <ul className="mt-2 space-y-1 text-sm">
            {rows.map((row) => (
              <li key={row.monitor_source_id}>
                {sourceNames[row.monitor_source_id] ?? row.monitor_source_id}: {describeOutcome(row)}
              </li>
            ))}
            {rows.length === 0 && <li className="text-muted-foreground">No source has reported yet.</li>}
          </ul>
        )}
      />
    </div>
  );
}

/** Requirement 5: a failed run is visible after the fact, not only while
 * a `RunProgressPanel` happens to be open for it. */
function RecentRuns({ monitorId }: { monitorId: string }) {
  const history = useRunHistory(monitorId);

  return (
    <section>
      <h2 className="mb-2 font-medium">Recent runs</h2>
      <LoadStateView
        state={history.data}
        render={(runs) => (
          <ul className="space-y-1 text-sm">
            {runs.map((run) => (
              <li key={run.id}>
                {run.run_date} — {run.status}
                {run.is_backfill ? " (backfill)" : ""}
              </li>
            ))}
            {runs.length === 0 && <li className="text-muted-foreground">No runs yet.</li>}
          </ul>
        )}
      />
    </section>
  );
}

/** Requirement 1, 2, 3, 4: one form covering all three repair kinds — a
 * stage re-run needs only a stage, a backfill only a window, and a purge
 * shows its preview count and submits with the token that count was
 * issued for, never one the operator typed by hand. */
function OverridesPanel({ monitorId }: { monitorId: string }) {
  const [kind, setKind] = useState<OverrideKind>("stage_rerun");
  const [stage, setStage] = useState<Stage>("clean");
  const [windowStart, setWindowStart] = useState("");
  const [windowEnd, setWindowEnd] = useState("");
  const submit = useSubmitOverride(monitorId);
  const preview = useRetentionPreview(monitorId, { enabled: kind === "retention_purge" });

  const canSubmit = kind !== "retention_purge" || preview.data !== undefined;

  return (
    <section>
      <h2 className="mb-2 font-medium">Repair overrides</h2>
      <form
        className="mb-4 grid max-w-md gap-2 border p-4"
        onSubmit={(event) => {
          event.preventDefault();
          const body: OverrideRequest =
            kind === "stage_rerun"
              ? { kind, stage }
              : kind === "backfill_window"
                ? { kind, window_start: windowStart, window_end: windowEnd }
                : { kind, purge_token: preview.data?.token };
          submit.mutate(body);
        }}
      >
        <h3 className="font-semibold">Submit an override</h3>

        <label htmlFor="override-kind">Kind</label>
        <select
          id="override-kind"
          className="border px-2 py-1"
          value={kind}
          onChange={(event) => setKind(event.target.value as OverrideKind)}
        >
          {OVERRIDE_KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>

        {kind === "stage_rerun" && (
          <>
            <label htmlFor="override-stage">Stage</label>
            <select
              id="override-stage"
              className="border px-2 py-1"
              value={stage}
              onChange={(event) => setStage(event.target.value as Stage)}
            >
              {STAGES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </>
        )}

        {kind === "backfill_window" && (
          <>
            <label htmlFor="override-window-start">Window start</label>
            <input
              id="override-window-start"
              type="date"
              className="border px-2 py-1"
              value={windowStart}
              onChange={(event) => setWindowStart(event.target.value)}
              required
            />
            <label htmlFor="override-window-end">Window end</label>
            <input
              id="override-window-end"
              type="date"
              className="border px-2 py-1"
              value={windowEnd}
              onChange={(event) => setWindowEnd(event.target.value)}
              required
            />
          </>
        )}

        {kind === "retention_purge" && (
          <p className="text-sm">
            {preview.isPending && "Loading preview…"}
            {preview.data && `${preview.data.count} documents past retention will be removed.`}
          </p>
        )}

        <button className="mt-2 border px-3 py-1" type="submit" disabled={submit.isPending || !canSubmit}>
          {submit.isPending ? "Submitting…" : "Submit override"}
        </button>

        {submit.isError && (
          <p role="alert" className="text-sm text-red-600">
            {submit.error.message}
          </p>
        )}
      </form>

      <OverrideJobList monitorId={monitorId} />
    </section>
  );
}

function OverrideJobList({ monitorId }: { monitorId: string }) {
  const overrides = useOverrides(monitorId);

  return (
    <LoadStateView
      state={overrides.data}
      render={(jobs) => (
        <ul className="space-y-1 text-sm">
          {jobs.map((job) => (
            <li key={job.id}>
              {job.kind} — {job.status} — submitted {job.submitted_at}
              {job.outcome && (
                <span className="text-muted-foreground"> — {JSON.stringify(job.outcome)}</span>
              )}
            </li>
          ))}
          {jobs.length === 0 && <li className="text-muted-foreground">No overrides submitted yet.</li>}
        </ul>
      )}
    />
  );
}
