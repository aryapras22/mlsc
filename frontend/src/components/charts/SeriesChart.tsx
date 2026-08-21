/**
 * Wraps `QualitySeriesChart` so a too-short range renders a message rather
 * than throwing into the page (design.md, "Failure strategy": "RangeTooShort
 * — fall back to a message instead of a chart"). `QualityMissing` is left
 * to propagate — it is a programming error, not a user condition, and must
 * fail loudly (design.md: "QualityMissing — crash in development").
 */

import { QualitySeriesChart, RangeTooShort, assertRangeLongEnough } from "./QualitySeriesChart";
import type { components } from "@/api/generated-types";

type SeriesPoint = components["schemas"]["SeriesPoint"];

export function SeriesChart({ points, label }: { points: SeriesPoint[]; label: string }) {
  // Checked here, before the child renders, rather than caught around the
  // JSX return below: React does not call a child component during that
  // synchronous return, only later during its own render phase, so a
  // try/catch wrapped around `<QualitySeriesChart />` never sees the
  // exception it throws.
  try {
    assertRangeLongEnough(points);
  } catch (error) {
    if (error instanceof RangeTooShort) {
      return <div className="text-muted-foreground text-sm">{error.message}</div>;
    }
    throw error;
  }

  return <QualitySeriesChart points={points} label={label} />;
}
