/**
 * `FilterState` lives in the URL, not in component state (design.md,
 * "Domain shapes") — that is what makes a drill-down shareable and a
 * reload lossless. Every page reads and writes through this hook rather
 * than touching `useSearchParams` directly, so the shape stays one thing.
 */

import { useSearchParams } from "react-router-dom";
import { useMemo } from "react";

export interface FilterState {
  start: string;
  end: string;
  topicId?: string;
  source?: string;
  entity?: string;
  metric?: string;
}

function defaultRange(): { start: string; end: string } {
  const end = new Date();
  const start = new Date(end);
  start.setDate(start.getDate() - 6);
  return { start: toIsoDate(start), end: toIsoDate(end) };
}

function toIsoDate(date: Date): string {
  return date.toISOString().slice(0, 10);
}

export function useFilterState(): [FilterState, (next: Partial<FilterState>) => void] {
  const [searchParams, setSearchParams] = useSearchParams();

  const filters = useMemo<FilterState>(() => {
    const fallback = defaultRange();
    return {
      start: searchParams.get("start") ?? fallback.start,
      end: searchParams.get("end") ?? fallback.end,
      topicId: searchParams.get("topic_id") ?? undefined,
      source: searchParams.get("source") ?? undefined,
      entity: searchParams.get("entity") ?? undefined,
      metric: searchParams.get("metric") ?? undefined,
    };
  }, [searchParams]);

  const setFilters = (next: Partial<FilterState>) => {
    const merged = { ...filters, ...next };
    const params = new URLSearchParams();
    params.set("start", merged.start);
    params.set("end", merged.end);
    if (merged.topicId) params.set("topic_id", merged.topicId);
    if (merged.source) params.set("source", merged.source);
    if (merged.entity) params.set("entity", merged.entity);
    if (merged.metric) params.set("metric", merged.metric);
    setSearchParams(params);
  };

  return [filters, setFilters];
}

/** The filter set a figure hands to the document explorer — a link, so the
 * drill-down survives a reload (design.md, "Domain shapes": `DrillTarget`). */
export interface DrillTarget {
  topicId?: string;
  source?: string;
  start?: string;
  end?: string;
}

export function drillTargetToSearchParams(target: DrillTarget): URLSearchParams {
  const params = new URLSearchParams();
  if (target.topicId) params.set("topic_id", target.topicId);
  if (target.source) params.set("source", target.source);
  if (target.start) params.set("start", target.start);
  if (target.end) params.set("end", target.end);
  return params;
}
