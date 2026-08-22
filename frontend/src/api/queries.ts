/**
 * One `useQuery` per surface, keyed on the filters that determine the
 * response — the mechanism behind the caching design.md's "Dependencies,
 * injected" section asks for. A component never fetches directly; it asks
 * for a `LoadState` and renders one of its four cases.
 */

import { useQuery } from "@tanstack/react-query";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import * as api from "./resources";
import { loadWithAbsence, load, type LoadState } from "./client";
import type { DateRangeParams, TimeseriesParams, InsightParams, DocumentParams } from "./resources";

export function useMonitor(monitorId: string) {
  return useQuery({
    queryKey: ["monitor", monitorId],
    queryFn: () => api.getMonitor(monitorId),
  });
}

export function useCreateMonitor() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: api.MonitorCreate) => api.createMonitor(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["monitors"] });
    },
  });
}

/** Also how a monitor is paused, resumed and archived — status is one more field. */
export function useUpdateMonitor() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ monitorId, body }: { monitorId: string; body: api.MonitorUpdate }) =>
      api.updateMonitor(monitorId, body),
    onSuccess: (monitor) => {
      queryClient.invalidateQueries({ queryKey: ["monitors"] });
      queryClient.invalidateQueries({ queryKey: ["monitor", monitor.id] });
    },
  });
}

export function useOverview(monitorId: string, range: DateRangeParams): { data: LoadState<api.Overview> } {
  const query = useQuery({
    queryKey: ["overview", monitorId, range.start, range.end],
    queryFn: () => load(() => api.getOverview(monitorId, range)),
  });
  return { data: query.data ?? { status: "loading" } };
}

export function useTimeseries(monitorId: string, params: TimeseriesParams): { data: LoadState<api.Series> } {
  const query = useQuery({
    queryKey: ["timeseries", monitorId, params.metric, params.start, params.end, params.topic_id, params.source],
    queryFn: () => loadWithAbsence(() => api.getTimeseries(monitorId, params)),
  });
  return { data: query.data ?? { status: "loading" } };
}

export function useTopicRanking(monitorId: string, range: DateRangeParams): { data: LoadState<api.TopicRanking> } {
  const query = useQuery({
    queryKey: ["topic-ranking", monitorId, range.start, range.end],
    queryFn: () => load(() => api.getTopicRanking(monitorId, range)),
  });
  return { data: query.data ?? { status: "loading" } };
}

export function useEvents(monitorId: string, range: DateRangeParams): { data: LoadState<api.EventView[]> } {
  const query = useQuery({
    queryKey: ["events", monitorId, range.start, range.end],
    queryFn: () => load(() => api.listEvents(monitorId, range)),
  });
  return { data: query.data ?? { status: "loading" } };
}

export function useInsights(monitorId: string, params: InsightParams): { data: LoadState<api.InsightView[]> } {
  const query = useQuery({
    queryKey: ["insights", monitorId, params.start, params.end, params.kind],
    queryFn: () => load(() => api.listInsights(monitorId, params)),
  });
  return { data: query.data ?? { status: "loading" } };
}

export function useEntityComparison(monitorId: string, range: DateRangeParams): { data: LoadState<api.EntityComparison> } {
  const query = useQuery({
    queryKey: ["compare", monitorId, range.start, range.end],
    queryFn: () => load(() => api.getEntityComparison(monitorId, range)),
  });
  return { data: query.data ?? { status: "loading" } };
}

export function useDocuments(monitorId: string, params: DocumentParams): { data: LoadState<api.DocumentPage> } {
  const query = useQuery({
    queryKey: ["documents", monitorId, params.topic_id, params.source, params.start, params.end, params.cursor],
    queryFn: () => load(() => api.browseDocuments(monitorId, params)),
  });
  return { data: query.data ?? { status: "loading" } };
}

export function useTriggerRun(monitorId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (runDate?: string) => api.triggerRun(monitorId, runDate),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["overview", monitorId] });
      queryClient.invalidateQueries({ queryKey: ["run-history", monitorId] });
    },
  });
}

const DEFAULT_RUN_POLL_INTERVAL_MS = 2000;

/** Polls a run until `finished_at` is set (requirement 5), then stops. */
export function useRun(
  runId: string,
  { pollIntervalMs = DEFAULT_RUN_POLL_INTERVAL_MS }: { pollIntervalMs?: number } = {}
): { data: LoadState<api.RunView> } {
  const query = useQuery({
    queryKey: ["run", runId],
    queryFn: () => load(() => api.getRun(runId)),
    refetchInterval: (q) => {
      const state = q.state.data;
      if (state?.status === "ready" && state.data.finished_at !== null) return false;
      return pollIntervalMs;
    },
  });
  return { data: query.data ?? { status: "loading" } };
}

/** The other half of `RunProgress` (design.md, "Domain shapes"): each
 * source's outcome, polled on the same cadence as the run itself. */
export function useRunStats(
  runId: string,
  { pollIntervalMs = DEFAULT_RUN_POLL_INTERVAL_MS, enabled = true }: { pollIntervalMs?: number; enabled?: boolean } = {}
): { data: LoadState<api.RunSourceStats[]> } {
  const query = useQuery({
    queryKey: ["run-stats", runId],
    queryFn: () => load(() => api.getRunStats(runId)),
    enabled,
    refetchInterval: enabled ? pollIntervalMs : false,
  });
  return { data: query.data ?? { status: "loading" } };
}

export function useRunHistory(monitorId: string): { data: LoadState<api.RunSummary[]> } {
  const query = useQuery({
    queryKey: ["run-history", monitorId],
    queryFn: () => load(() => api.listRuns(monitorId)),
  });
  return { data: query.data ?? { status: "loading" } };
}

export function useSources(monitorId: string): { data: LoadState<api.Source[]> } {
  const query = useQuery({
    queryKey: ["sources", monitorId],
    queryFn: () => load(() => api.listSources(monitorId)),
  });
  return { data: query.data ?? { status: "loading" } };
}

export function useAttachSource(monitorId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: api.SourceCreate) => api.attachSource(monitorId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sources", monitorId] });
    },
  });
}

export function useUpdateSource(monitorId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ sourceId, body }: { sourceId: string; body: api.SourceUpdate }) =>
      api.updateSource(sourceId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sources", monitorId] });
    },
  });
}

export function useRecordJudgement() {
  return useMutation({
    mutationFn: ({ insightId, useful }: { insightId: string; useful: boolean }) =>
      api.recordJudgement(insightId, useful),
  });
}
