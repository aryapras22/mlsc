/**
 * The one chart primitive every time-series surface builds on. Takes a
 * `Series` whose points carry their own quality (design.md, "Domain
 * shapes": `ChartSeries`) — there is no code path here that draws a point
 * without knowing whether it was clean, truncated, or partial.
 *
 * `QualityMissing` fails loudly rather than silently rendering an
 * unqualified line: a chart that could ship without quality is exactly
 * the omission requirement 2 exists to make impossible (design.md,
 * "Failure strategy": "QualityMissing — crash in development").
 */

import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { components } from "@/api/generated-types";

type SeriesPoint = components["schemas"]["SeriesPoint"];

export class QualityMissing extends Error {}

const MIN_POINTS_FOR_HONEST_CHART = 5;

export class RangeTooShort extends Error {}

function assertQualityPresent(points: SeriesPoint[]): void {
  for (const point of points) {
    if (point.quality === undefined || point.quality === null) {
      throw new QualityMissing(`point at ${point.bucket} has no quality — refusing to render`);
    }
  }
}

/** Requirement 10: a too-short range says so instead of drawing a chart
 * from a handful of points, which invites a conclusion the data cannot
 * support (design.md, "Failure strategy"). */
export function assertRangeLongEnough(points: SeriesPoint[]): void {
  if (points.length < MIN_POINTS_FOR_HONEST_CHART) {
    throw new RangeTooShort(
      `${points.length} points is too few to chart honestly (minimum ${MIN_POINTS_FOR_HONEST_CHART})`
    );
  }
}

const QUALITY_COLOR: Record<string, string> = {
  clean: "#16a34a",
  truncated: "#d97706",
  partial: "#dc2626",
};

interface QualitySeriesChartProps {
  points: SeriesPoint[];
  label: string;
}

export function QualitySeriesChart({ points, label }: QualitySeriesChartProps) {
  assertQualityPresent(points);
  assertRangeLongEnough(points);

  const data = points.map((point) => ({
    bucket: point.bucket,
    value: point.value,
    quality: point.quality,
  }));

  return (
    <div>
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="bucket" />
          <YAxis />
          <Tooltip
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null;
              const point = payload[0].payload as { bucket: string; value: number | null; quality: string };
              return (
                <div className="rounded border bg-white p-2 text-xs shadow">
                  <div>{point.bucket}</div>
                  <div>value: {point.value ?? "—"}</div>
                  <div>quality: {point.quality}</div>
                  {point.quality !== "clean" && (
                    <div className="text-amber-600">
                      {point.quality === "truncated"
                        ? "This day's sample hit its collection quota — the count is a floor, not a measurement."
                        : "This day's data is partial — a source failed validation that day."}
                    </div>
                  )}
                </div>
              );
            }}
          />
          <Line
            type="monotone"
            dataKey="value"
            stroke="#2563eb"
            dot={(props: { cx?: number; cy?: number; payload?: { quality?: string } }) => {
              const quality = props.payload?.quality ?? "clean";
              const color = QUALITY_COLOR[quality] ?? QUALITY_COLOR.clean;
              return (
                <circle
                  key={`${props.cx}-${props.cy}`}
                  cx={props.cx}
                  cy={props.cy}
                  r={quality === "clean" ? 3 : 5}
                  fill={color}
                  aria-label={`quality: ${quality}`}
                />
              );
            }}
          />
        </LineChart>
      </ResponsiveContainer>
      <p className="text-muted-foreground text-xs">{label}</p>
    </div>
  );
}
