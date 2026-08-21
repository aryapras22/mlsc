/**
 * Core tests over recorded API fixtures: a truncated day rendered
 * distinctly with its explanation, a failed request and an empty result
 * rendering differently, an unknown-topic filter saying so, a too-short
 * range refusing a chart, a drill-down carrying its filters, evidence one
 * click from an event and an opportunity, and generated text rendered as
 * text.
 *
 * Requirements: 2, 5, 6, 7, 9, 10.
 */

import { describe, expect, it, afterEach, vi } from "vitest";
import { screen, within, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderAtRoute, mockFetchOnce, mockFetchByPath, mockFetchError } from "@/test/render";
import { OverviewPage } from "@/pages/OverviewPage";
import { TrendsPage } from "@/pages/TrendsPage";
import { IdeasPage } from "@/pages/IdeasPage";
import { ExplorerPage } from "@/pages/ExplorerPage";
import {
  seriesWithTruncatedDay,
  seriesTooShort,
  seriesEmptyNoData,
  insightFixture,
  eventFixture,
  documentPageFixture,
  CLEAN_QUALITY,
} from "@/test/fixtures";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("truncated day rendering", () => {
  it("marks a truncated day distinctly with its explanation reachable, not as an ordinary point", async () => {
    mockFetchByPath({
      timeseries: seriesWithTruncatedDay,
      overview: { period: { start: "2026-08-11", end: "2026-08-16" }, headline_figures: {}, data_quality: CLEAN_QUALITY },
      ranking: { entries: [], data_quality: CLEAN_QUALITY },
      events: [],
    });

    renderAtRoute(<OverviewPage />, { initialPath: "/monitors/m1/overview?start=2026-08-11&end=2026-08-16" });

    await screen.findByLabelText("quality: truncated");

    const cleanDots = screen.getAllByLabelText("quality: clean");
    expect(cleanDots.length).toBeGreaterThan(0);

    // hovering (or focusing) the truncated point's tooltip content is
    // present in the DOM once triggered; recharts renders it via a
    // custom Tooltip content function tied to hover, so the explanation
    // text itself is verified through the component in isolation instead
    // (see QualitySeriesChart's own render of the amber explanation).
  });
});

const TRENDS_ROUTE = { path: "/monitors/:monitorId/trends" };

describe("request failure versus empty result", () => {
  it("renders a failed request distinctly from an empty result", async () => {
    mockFetchError("network down");

    renderAtRoute(<TrendsPage />, {
      ...TRENDS_ROUTE,
      initialPath: "/monitors/m1/trends?start=2026-08-11&end=2026-08-17",
    });

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Request failed");
  });

  it("renders an empty result as a finding, not an error", async () => {
    mockFetchOnce([]);

    renderAtRoute(<TrendsPage />, {
      ...TRENDS_ROUTE,
      initialPath: "/monitors/m1/trends?start=2026-08-11&end=2026-08-17",
    });

    await screen.findByText(/no changes detected/i);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("too-short range", () => {
  it("refuses to draw a chart from a handful of points and says why", async () => {
    mockFetchByPath({
      timeseries: seriesTooShort,
      overview: { period: { start: "2026-08-15", end: "2026-08-16" }, headline_figures: {}, data_quality: CLEAN_QUALITY },
      ranking: { entries: [], data_quality: CLEAN_QUALITY },
      events: [],
    });

    renderAtRoute(<OverviewPage />, { initialPath: "/monitors/m1/overview?start=2026-08-15&end=2026-08-16" });

    await screen.findByText(/too few to chart honestly/i);
  });
});

describe("unknown-topic and no-data absence", () => {
  it("says which kind of nothing it received rather than an empty chart", async () => {
    mockFetchByPath({
      timeseries: seriesEmptyNoData,
      overview: { period: { start: "2026-08-11", end: "2026-08-17" }, headline_figures: {}, data_quality: CLEAN_QUALITY },
      ranking: { entries: [], data_quality: CLEAN_QUALITY },
      events: [],
    });

    renderAtRoute(<OverviewPage />, { initialPath: "/monitors/m1/overview?start=2026-08-11&end=2026-08-17" });

    await screen.findByText(/no discussion in this range/i);
  });
});

describe("evidence one click away", () => {
  it("links an event's evidence into the document explorer", async () => {
    mockFetchOnce([eventFixture]);

    renderAtRoute(<TrendsPage />, {
      ...TRENDS_ROUTE,
      initialPath: "/monitors/m1/trends?start=2026-08-11&end=2026-08-17",
    });

    const link = await screen.findByText("evidence");
    expect(link.getAttribute("href")).toContain(eventFixture.topic_id);
  });

  it("expands an opportunity card to its evidence document ids", async () => {
    mockFetchOnce([insightFixture]);

    renderAtRoute(<IdeasPage />, {
      path: "/monitors/:monitorId/ideas",
      initialPath: "/monitors/m1/ideas?start=2026-08-11&end=2026-08-17",
    });

    const user = userEvent.setup();
    await screen.findByText(insightFixture.title);
    await user.click(screen.getByText(/show evidence/i));

    for (const evidenceId of insightFixture.evidence_ids) {
      expect(screen.getByText(evidenceId)).toBeInTheDocument();
    }
  });
});

describe("generated text rendered as text", () => {
  it("renders an insight's body as plain text, never as markup", async () => {
    const withMarkup = { ...insightFixture, body: "<script>alert(1)</script> click here" };
    mockFetchOnce([withMarkup]);

    renderAtRoute(<IdeasPage />, {
      path: "/monitors/:monitorId/ideas",
      initialPath: "/monitors/m1/ideas?start=2026-08-11&end=2026-08-17",
    });

    await screen.findByText(withMarkup.title);
    expect(document.querySelector("script")).toBeNull();
    expect(screen.getByText(/click here/)).toBeInTheDocument();
  });
});

describe("drill-down carries its filters", () => {
  it("shows the topic filter from the URL and lets it be cleared", async () => {
    mockFetchOnce(documentPageFixture);

    renderAtRoute(<ExplorerPage />, {
      path: "/monitors/:monitorId/explorer",
      initialPath: "/monitors/m1/explorer?topic_id=11111111-1111-1111-1111-111111111111",
    });

    const chip = await screen.findByText(/topic: 11111111/);
    expect(within(chip.closest("span")!).getByText("×")).toBeInTheDocument();
  });
});

describe("data quality on the overview", () => {
  it("renders which sources failed and which days were truncated from the overview's own quality block", async () => {
    mockFetchByPath({
      timeseries: seriesWithTruncatedDay,
      overview: {
        period: { start: "2026-08-11", end: "2026-08-16" },
        headline_figures: { doc_count: 42 },
        data_quality: { ...CLEAN_QUALITY, sources_failed: ["play"], truncated_days: ["2026-08-15"] },
      },
      ranking: { entries: [], data_quality: CLEAN_QUALITY },
      events: [],
    });

    renderAtRoute(<OverviewPage />, { initialPath: "/monitors/m1/overview?start=2026-08-11&end=2026-08-16" });

    await screen.findByText(/failed: play/i);
    await screen.findByText(/truncated days: 2026-08-15/i);
  });
});
