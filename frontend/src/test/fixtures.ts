/**
 * Recorded API response fixtures — one per surface, including a typed
 * empty case, a truncated-day case, and a too-short-range case (design.md,
 * "Dependencies, injected": "the API client is a fixture returning
 * recorded responses").
 */

import type { components } from "@/api/generated-types";

type DataQuality = components["schemas"]["DataQuality"];

export const CLEAN_QUALITY: DataQuality = {
  sample_size: 100,
  truncated_days: [],
  sources_ok: ["play"],
  sources_failed: [],
  topics_absent: [],
};

export const TRUNCATED_QUALITY: DataQuality = {
  ...CLEAN_QUALITY,
  truncated_days: ["2026-08-15"],
};

export const seriesWithTruncatedDay: components["schemas"]["Series"] = {
  metric: "volume",
  topic_id: null,
  source: null,
  absence: null,
  data_quality: TRUNCATED_QUALITY,
  points: [
    { bucket: "2026-08-11", value: 10, quality: "clean" },
    { bucket: "2026-08-12", value: 12, quality: "clean" },
    { bucket: "2026-08-13", value: 11, quality: "clean" },
    { bucket: "2026-08-14", value: 9, quality: "clean" },
    { bucket: "2026-08-15", value: 40, quality: "truncated" },
    { bucket: "2026-08-16", value: 13, quality: "clean" },
  ],
};

export const seriesTooShort: components["schemas"]["Series"] = {
  metric: "volume",
  topic_id: null,
  source: null,
  absence: null,
  data_quality: CLEAN_QUALITY,
  points: [
    { bucket: "2026-08-15", value: 10, quality: "clean" },
    { bucket: "2026-08-16", value: 12, quality: "clean" },
  ],
};

export const seriesEmptyNoData: components["schemas"]["Series"] = {
  metric: "volume",
  topic_id: "11111111-1111-1111-1111-111111111111",
  source: null,
  absence: "no_data",
  data_quality: CLEAN_QUALITY,
  points: [],
};

export const insightFixture: components["schemas"]["InsightView"] = {
  id: "22222222-2222-2222-2222-222222222222",
  monitor_id: "33333333-3333-3333-3333-333333333333",
  topic_id: "11111111-1111-1111-1111-111111111111",
  period: { start: "2026-08-10", end: "2026-08-17" },
  kind: "opportunity",
  title: "Stabilize login",
  body: "Users report crashes on login after the recent update.",
  who: "Users attempting to log in",
  what: "Fix login crashes",
  why: "Multiple crash reports since the update",
  score: 0.7,
  score_components: { severity: 0.8, frequency: 0.6 },
  evidence_ids: ["44444444-4444-4444-4444-444444444444"],
  llm_provider: "openai_compatible",
  llm_model: "qwen3:8b",
  prompt_version: "v1",
};

export const eventFixture: components["schemas"]["EventView"] = {
  id: "55555555-5555-5555-5555-555555555555",
  topic_id: "11111111-1111-1111-1111-111111111111",
  detected_on: "2026-08-15",
  kind: "burst",
  method: "robust_z",
  severity: 4.2,
  statistics: { z: 4.2 },
  evidence_ids: ["44444444-4444-4444-4444-444444444444"],
};

export const documentPageFixture: components["schemas"]["DocumentPage"] = {
  items: [
    {
      id: "44444444-4444-4444-4444-444444444444",
      source_name: "play",
      url: null,
      body: "The app crashes every time I try to log in.",
      published_at: "2026-08-15T10:00:00Z",
      rating: 2,
      topic_id: "11111111-1111-1111-1111-111111111111",
    },
  ],
  cursor: null,
  total_known: null,
};
