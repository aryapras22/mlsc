/**
 * The monitor list's two write paths: the seed shape the API demands differs by
 * target type, and pause, resume and archive are all one status field.
 */

import { describe, expect, it, afterEach, vi } from "vitest";
import { screen, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderAtRoute, mockFetchByPath } from "@/test/render";
import { MonitorList } from "@/pages/MonitorList";
import type { Monitor } from "@/api/resources";

const ROUTE = { path: "/", initialPath: "/" };

const activeMonitor: Monitor = {
  id: "m1",
  name: "roblox",
  target_type: "product",
  seed: { identifiers: ["com.roblox.client"] },
  cron_expression: "0 3 * * *",
  timezone: "UTC",
  status: "active",
  retention_days: 90,
  created_at: "2026-08-22T00:00:00Z",
  projected: true,
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function requestsOfMethod(method: string) {
  return vi
    .mocked(fetch)
    .mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === method);
}

describe("creating a monitor", () => {
  it("sends a product seed as an identifier list", async () => {
    mockFetchByPath({ monitors: [] });
    renderAtRoute(<MonitorList />, ROUTE);

    await userEvent.type(screen.getByLabelText("Name"), "roblox");
    await userEvent.type(
      screen.getByLabelText("Identifiers (comma separated)"),
      "com.roblox.client, 431946152"
    );
    await userEvent.click(screen.getByRole("button", { name: "Create monitor" }));

    const [, init] = requestsOfMethod("POST")[0];
    expect(JSON.parse((init as RequestInit).body as string)).toMatchObject({
      name: "roblox",
      target_type: "product",
      seed: { identifiers: ["com.roblox.client", "431946152"] },
      retention_days: 90,
    });
  });

  it("sends a theme seed as a description", async () => {
    mockFetchByPath({ monitors: [] });
    renderAtRoute(<MonitorList />, ROUTE);

    await userEvent.type(screen.getByLabelText("Name"), "cloud gaming");
    await userEvent.selectOptions(screen.getByLabelText("Target type"), "theme");
    await userEvent.type(screen.getByLabelText("Description"), "cloud gaming latency");
    await userEvent.click(screen.getByRole("button", { name: "Create monitor" }));

    const [, init] = requestsOfMethod("POST")[0];
    expect(JSON.parse((init as RequestInit).body as string)).toMatchObject({
      target_type: "theme",
      seed: { description: "cloud gaming latency" },
    });
  });

  it("shows the API's rejection instead of clearing the form", async () => {
    // The list must succeed while the create fails, and mockFetchByPath routes on the
    // path alone — both requests are /monitors.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (_url: string | URL, init?: RequestInit) =>
        init?.method === "POST"
          ? {
              ok: false,
              status: 422,
              statusText: "Unprocessable Entity",
              text: async () => '{"detail":"cron_expression is not valid"}',
            }
          : { ok: true, status: 200, statusText: "OK", json: async () => [] }
      )
    );
    renderAtRoute(<MonitorList />, ROUTE);

    await userEvent.type(screen.getByLabelText("Name"), "roblox");
    await userEvent.type(screen.getByLabelText("Identifiers (comma separated)"), "com.roblox.client");
    await userEvent.click(screen.getByRole("button", { name: "Create monitor" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("cron_expression is not valid");
    expect(screen.getByLabelText("Name")).toHaveValue("roblox");
  });
});

describe("monitor lifecycle", () => {
  it("pauses an active monitor by patching its status", async () => {
    mockFetchByPath({ monitors: [activeMonitor] });
    renderAtRoute(<MonitorList />, ROUTE);

    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));

    const [url, init] = requestsOfMethod("PATCH")[0];
    expect(url.toString()).toContain("/monitors/m1");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ status: "paused" });
  });

  it("offers no lifecycle actions on an archived monitor", async () => {
    mockFetchByPath({ monitors: [{ ...activeMonitor, status: "archived" }] });
    renderAtRoute(<MonitorList />, ROUTE);

    await screen.findByText("archived");
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Archive" })).toBeNull();
  });
});
