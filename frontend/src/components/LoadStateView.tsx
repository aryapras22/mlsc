/**
 * Renders the four `LoadState` cases distinctly (design.md, "Failure
 * strategy"): a failed request never looks like an empty chart, and each
 * typed absence says which kind of nothing it is.
 */

import type { ReactNode } from "react";
import type { LoadState, Absence } from "@/api/client";

const ABSENCE_MESSAGES: Record<Absence, string> = {
  no_data: "No discussion in this range and filters.",
  unknown_topic: "This topic filter does not exist for this monitor.",
  unknown_source: "This source is not attached to this monitor.",
  out_of_retention: "This range predates the monitor's retention window.",
};

export function LoadStateView<T>({
  state,
  render,
}: {
  state: LoadState<T>;
  render: (data: T) => ReactNode;
}) {
  switch (state.status) {
    case "loading":
      return <div className="text-muted-foreground text-sm">Loading…</div>;
    case "failed":
      return (
        <div role="alert" className="text-sm text-red-600">
          Request failed: {state.reason}
        </div>
      );
    case "empty":
      return <div className="text-muted-foreground text-sm">{ABSENCE_MESSAGES[state.absence]}</div>;
    case "ready":
      return <>{render(state.data)}</>;
  }
}
