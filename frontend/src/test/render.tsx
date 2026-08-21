import type { ReactElement } from "react";
import { render } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { vi } from "vitest";

/** Renders a page at a route with filters already in the URL, matching
 * how every real page reads `FilterState` (design.md, "Domain shapes"). */
export function renderAtRoute(
  element: ReactElement,
  { path = "/monitors/:monitorId/overview", initialPath }: { path?: string; initialPath: string }
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route path={path} element={element} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function toResponse(body: unknown, options?: { ok?: boolean; status?: number }) {
  const ok = options?.ok ?? true;
  const status = options?.status ?? (ok ? 200 : 500);
  return {
    ok,
    status,
    statusText: ok ? "OK" : "Internal Server Error",
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

/** Installs a fetch mock returning `response` for every call — enough
 * when a page makes exactly one distinct request. */
export function mockFetchOnce(response: unknown, options?: { ok?: boolean; status?: number }) {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(toResponse(response, options)));
}

/** Installs a fetch mock that routes by which path segment the request
 * URL contains — for a page (like the overview) that calls several
 * distinct endpoints in one render. */
export function mockFetchByPath(routes: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (url: string | URL) => {
      const path = url.toString();
      for (const [match, body] of Object.entries(routes)) {
        if (path.includes(match)) return toResponse(body);
      }
      return toResponse({ detail: `no fixture registered for ${path}` }, { ok: false, status: 404 });
    })
  );
}

export function mockFetchError(message: string) {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error(message)));
}
