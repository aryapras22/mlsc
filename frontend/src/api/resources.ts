/**
 * One typed function per endpoint. Nothing above this layer constructs a
 * fetch call directly (design.md, "Dependencies, injected").
 */

import { apiGet, apiPost } from "./client";
import type { components } from "./generated-types";

export type Overview = components["schemas"]["Overview"];
export type Series = components["schemas"]["Series"];
export type TopicRanking = components["schemas"]["TopicRanking"];
export type EventView = components["schemas"]["EventView"];
export type InsightView = components["schemas"]["InsightView"];
export type DocumentPage = components["schemas"]["DocumentPage"];
export type RunView = components["schemas"]["RunView"];
export type Monitor = components["schemas"]["MonitorResponse"];
export type EntityComparison = components["schemas"]["EntityComparison"];
export type Metric = components["schemas"]["Metric"];
export type SourceName = components["schemas"]["SourceName"];
export type InsightKind = components["schemas"]["InsightKind"];

export interface DateRangeParams {
  start: string;
  end: string;
}

export function getMonitor(monitorId: string): Promise<Monitor> {
  return apiGet<Monitor>(`/monitors/${monitorId}`);
}

export function listMonitors(): Promise<Monitor[]> {
  return apiGet<Monitor[]>("/monitors");
}

export function getOverview(monitorId: string, range: DateRangeParams): Promise<Overview> {
  return apiGet<Overview>(`/monitors/${monitorId}/overview`, range);
}

export interface TimeseriesParams extends DateRangeParams {
  metric: Metric;
  topic_id?: string;
  source?: SourceName;
}

export function getTimeseries(monitorId: string, params: TimeseriesParams): Promise<Series> {
  return apiGet<Series>(`/monitors/${monitorId}/timeseries`, params);
}

export function getTopicRanking(monitorId: string, range: DateRangeParams): Promise<TopicRanking> {
  return apiGet<TopicRanking>(`/monitors/${monitorId}/topics/ranking`, range);
}

export function listEvents(monitorId: string, range: DateRangeParams): Promise<EventView[]> {
  return apiGet<EventView[]>(`/monitors/${monitorId}/events`, range);
}

export interface InsightParams extends DateRangeParams {
  kind?: InsightKind;
}

export function listInsights(monitorId: string, params: InsightParams): Promise<InsightView[]> {
  return apiGet<InsightView[]>(`/monitors/${monitorId}/insights`, params);
}

export interface DocumentParams {
  topic_id?: string;
  source?: SourceName;
  start?: string;
  end?: string;
  cursor?: string;
  limit?: string;
}

export function browseDocuments(monitorId: string, params: DocumentParams): Promise<DocumentPage> {
  return apiGet<DocumentPage>(`/monitors/${monitorId}/documents`, params);
}

export function getEntityComparison(monitorId: string, range: DateRangeParams): Promise<EntityComparison> {
  return apiGet<EntityComparison>(`/monitors/${monitorId}/compare`, range);
}

export function getRun(runId: string): Promise<RunView> {
  return apiGet<RunView>(`/runs/${runId}`);
}

export function triggerRun(monitorId: string, runDate?: string): Promise<{ run_id: string }> {
  return apiPost<{ run_id: string }>(`/monitors/${monitorId}/runs`, { run_date: runDate ?? null });
}

export function recordJudgement(insightId: string, useful: boolean): Promise<void> {
  return apiPost<void>(`/insights/${insightId}/judgement`, { useful });
}
