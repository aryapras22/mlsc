/**
 * The API client and the load state every surface receives instead of a
 * promise (design.md, "Domain shapes": `LoadState`).
 *
 * A component never sees a raw fetch. It gets `loading`, `ready`, an
 * `empty` case carrying the typed absence the API returned, or `failed`
 * carrying a reason — three of those four are commonly collapsed into
 * "no data", and that collapse is exactly what makes a broken page look
 * like a quiet week (design.md, "Domain shapes": "the important one").
 */

import type { components } from "./generated-types";

export type Absence = components["schemas"]["Absence"];

export type LoadState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "empty"; absence: Absence }
  | { status: "failed"; reason: string };

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export class RequestFailed extends Error {}

/**
 * Fetches one JSON resource and returns it, or throws `RequestFailed`.
 * Never returns an empty result for a 4xx/5xx — a caller distinguishes a
 * real absence (present in the payload as an `absence` field) from a
 * request that failed outright.
 */
export async function apiGet<T>(path: string, params?: object): Promise<T> {
  const url = new URL(path, API_BASE_URL);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
  }

  let response: Response;
  try {
    response = await fetch(url);
  } catch (error) {
    throw new RequestFailed(error instanceof Error ? error.message : "network error");
  }

  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new RequestFailed(`${response.status} ${response.statusText}: ${body}`.trim());
  }

  return (await response.json()) as T;
}

async function sendJson<T>(method: "POST" | "PATCH", path: string, body: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(new URL(path, API_BASE_URL), {
      method,
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (error) {
    throw new RequestFailed(error instanceof Error ? error.message : "network error");
  }

  if (!response.ok) {
    const responseBody = await response.text().catch(() => "");
    throw new RequestFailed(`${response.status} ${response.statusText}: ${responseBody}`.trim());
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export function apiPost<T>(path: string, body: unknown): Promise<T> {
  return sendJson<T>("POST", path, body);
}

export function apiPatch<T>(path: string, body: unknown): Promise<T> {
  return sendJson<T>("PATCH", path, body);
}

/** A response shape that carries a typed absence alongside its data. */
export interface WithAbsence {
  absence: Absence | null;
}

/**
 * Wraps a fetch that may report a typed absence into a `LoadState` — the
 * one place `RequestFailed` and a typed `empty` are told apart, so every
 * surface built on top of this never has to make that distinction itself.
 */
export async function loadWithAbsence<T extends WithAbsence>(
  fetcher: () => Promise<T>
): Promise<LoadState<T>> {
  try {
    const data = await fetcher();
    if (data.absence) {
      return { status: "empty", absence: data.absence };
    }
    return { status: "ready", data };
  } catch (error) {
    return { status: "failed", reason: error instanceof Error ? error.message : "unknown error" };
  }
}

/** Wraps a fetch with no absence concept — a monitor, a run, a list. */
export async function load<T>(fetcher: () => Promise<T>): Promise<LoadState<T>> {
  try {
    const data = await fetcher();
    return { status: "ready", data };
  } catch (error) {
    return { status: "failed", reason: error instanceof Error ? error.message : "unknown error" };
  }
}
