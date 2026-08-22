/**
 * The three write paths this page adds: an invalid source config comes
 * back as a field-naming rejection, a trigger refusal (no enabled source)
 * shows as text rather than a silently-lost run, and the progress panel
 * polls a run to a terminal status while surfacing each source's outcome.
 *
 * Requirements: 5, 6, 7.
 */

import { describe, expect, it, afterEach, vi } from "vitest";
import { screen, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderAtRoute } from "@/test/render";
import { SettingsPage, RunProgressPanel } from "@/pages/SettingsPage";
import type { Source, RunView, RunSourceStats } from "@/api/resources";

const ROUTE = { path: "/monitors/:monitorId/settings", initialPath: "/monitors/m1/settings" };

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function toResponse(body: unknown, options?: { ok?: boolean; status?: number }) {
  const ok = options?.ok ?? true;
  const status = options?.status ?? (ok ? 200 : 500);
  return {
    ok,
    status,
    statusText: ok ? "OK" : "Error",
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

/** Routes by method and path substring, and by call count within a route
 * — the poll test needs a run's second fetch to differ from its first. */
function mockFetchSequenced(
  routes: Record<string, ((call: number) => unknown) | unknown>
) {
  const callCounts: Record<string, number> = {};
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (url: string | URL, init?: RequestInit) => {
      const path = url.toString();
      const method = init?.method ?? "GET";
      for (const [key, value] of Object.entries(routes)) {
        const [routeMethod, routeMatch] = key.split(" ");
        if (method !== routeMethod || !path.includes(routeMatch)) continue;
        const call = (callCounts[key] ?? 0) + 1;
        callCounts[key] = call;
        const body = typeof value === "function" ? (value as (call: number) => unknown)(call) : value;
        return toResponse(body);
      }
      return toResponse({ detail: `no fixture for ${method} ${path}` }, { ok: false, status: 404 });
    })
  );
}

describe("attaching a source", () => {
  it("shows the API's field-naming rejection instead of clearing the form", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (url: string | URL, init?: RequestInit) => {
        const path = url.toString();
        const method = init?.method ?? "GET";
        if (method === "POST" && path.includes("/sources")) {
          return toResponse(
            { detail: "config.package_id 'not-a-package' is not a well-formed Play package identifier" },
            { ok: false, status: 422 }
          );
        }
        if (path.includes("/sources")) return toResponse([]);
        if (path.includes("/runs")) return toResponse([]);
        return toResponse({ detail: `no fixture for ${method} ${path}` }, { ok: false, status: 404 });
      })
    );

    renderAtRoute(<SettingsPage />, ROUTE);

    await userEvent.type(screen.getByLabelText("Package id"), "not-a-package");
    await userEvent.click(screen.getByRole("button", { name: "Attach source" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("is not a well-formed Play package identifier");
    expect(screen.getByLabelText("Package id")).toHaveValue("not-a-package");
  });
});

describe("starting a run with no enabled source", () => {
  it("shows the refusal as text rather than losing the click", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (url: string | URL, init?: RequestInit) => {
        const path = url.toString();
        const method = init?.method ?? "GET";
        if (method === "POST" && path.includes("/runs")) {
          return toResponse({ detail: "monitor m1 has no enabled source" }, { ok: false, status: 409 });
        }
        if (path.includes("/sources")) return toResponse([]);
        if (path.includes("/runs")) return toResponse([]);
        return toResponse({ detail: `no fixture for ${method} ${path}` }, { ok: false, status: 404 });
      })
    );

    renderAtRoute(<SettingsPage />, ROUTE);

    await userEvent.click(await screen.findByRole("button", { name: "Run now" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("has no enabled source");
  });
});

describe("polling a run to completion", () => {
  it("reaches partial and shows the one failed source once polling stops", async () => {
    const playSource: Source = {
      id: "s1",
      monitor_id: "m1",
      source_name: "play",
      instance_key: "com.roblox.client",
      config: { package_id: "com.roblox.client" },
      daily_quota: 300,
      enabled: true,
      last_external_id: null,
      last_published_at: null,
      created_at: "2026-08-20T00:00:00Z",
    };
    const discourseSource: Source = { ...playSource, id: "s2", source_name: "discourse", instance_key: "https://forum.example.com" };

    const runningRun: RunView = {
      id: "r1",
      monitor_id: "m1",
      run_date: "2026-08-22",
      status: "running",
      stage_status: { ingest: "running" },
      started_at: "2026-08-22T03:00:00Z",
      finished_at: null,
    };
    const partialRun: RunView = { ...runningRun, status: "partial", stage_status: { ingest: "complete" }, finished_at: "2026-08-22T03:05:00Z" };

    const inFlightStats: RunSourceStats[] = [];
    const finalStats: RunSourceStats[] = [
      { monitor_source_id: "s1", attempted: 10, fetched: 8, kept: 8, quota: 300, quota_outcome: "within_allowance", validation_failed: false, error: null },
      { monitor_source_id: "s2", attempted: 0, fetched: 0, kept: 0, quota: 300, quota_outcome: "within_allowance", validation_failed: false, error: "connection refused" },
    ];

    mockFetchSequenced({
      "GET /monitors/m1/sources": [playSource, discourseSource],
      "GET /runs/r1/stats": (call: number) => (call === 1 ? inFlightStats : finalStats),
      "GET /runs/r1": (call: number) => (call === 1 ? runningRun : partialRun),
    });

    renderAtRoute(<RunProgressPanel monitorId="m1" runId="r1" pollIntervalMs={20} />, {
      path: "/monitors/:monitorId/settings",
      initialPath: "/monitors/m1/settings",
    });

    await screen.findByText("partial");
    await screen.findByText(/discourse: failed — connection refused/i);
    expect(screen.getByText(/play: collected, 8 kept/i)).toBeInTheDocument();
  });
});
