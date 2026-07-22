import type { UsageLatencyPoint, UsageVolumePoint } from "../../api/types";
import styles from "../../styles/app.module.css";

const WIDTH = 528;
const HEIGHT = 280;
const PLOT = { left: 38, right: 14, top: 54, bottom: 38 };

function tickIndexes(length: number): number[] {
  if (length <= 1) return [0];
  return [...new Set([0, Math.floor((length - 1) / 2), length - 1])];
}

function dateLabel(value: string): string {
  return new Intl.DateTimeFormat("en", { day: "numeric", month: "short", timeZone: "UTC" }).format(
    new Date(value),
  );
}

function niceMaximum(value: number): number {
  if (value <= 1) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  return Math.ceil(value / magnitude) * magnitude;
}

export function VolumeChart({ points }: { points: UsageVolumePoint[] }) {
  const plotWidth = WIDTH - PLOT.left - PLOT.right;
  const plotHeight = HEIGHT - PLOT.top - PLOT.bottom;
  const maximum = niceMaximum(
    Math.max(1, ...points.flatMap((point) => [point.standard, point.thorough])),
  );
  const groupWidth = plotWidth / Math.max(points.length, 1);
  const barWidth = Math.max(2, Math.min(7, groupWidth * 0.3));
  const labels = tickIndexes(points.length);

  return (
    <div className={styles.chartShell}>
      {points.length === 0 ? (
        <p className={styles.chartEmpty}>No search activity in this period.</p>
      ) : (
        <>
          <svg
            viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
            role="img"
            aria-labelledby="volume-title volume-desc"
          >
            <title id="volume-title">Daily search volume</title>
            <desc id="volume-desc">Standard and Thorough searches for the last 30 days.</desc>
            <g className={styles.chartLegend}>
              <circle cx="40" cy="21" r="4" className={styles.chartBrandFill} />
              <text x="50" y="25">
                Standard
              </text>
              <circle cx="126" cy="21" r="4" className={styles.chartMutedFill} />
              <text x="136" y="25">
                Thorough
              </text>
            </g>
            {[0, 0.5, 1].map((ratio) => {
              const y = PLOT.top + plotHeight * ratio;
              const value = Math.round(maximum * (1 - ratio));
              return (
                <g key={ratio}>
                  <line
                    x1={PLOT.left}
                    x2={WIDTH - PLOT.right}
                    y1={y}
                    y2={y}
                    className={styles.chartGrid}
                  />
                  <text
                    x={PLOT.left - 8}
                    y={y + 4}
                    textAnchor="end"
                    className={styles.chartAxisLabel}
                  >
                    {value}
                  </text>
                </g>
              );
            })}
            {points.map((point, index) => {
              const center = PLOT.left + index * groupWidth + groupWidth / 2;
              const standardHeight = (point.standard / maximum) * plotHeight;
              const thoroughHeight = (point.thorough / maximum) * plotHeight;
              return (
                <g key={point.bucket_start}>
                  <rect
                    x={center - barWidth - 1}
                    y={PLOT.top + plotHeight - standardHeight}
                    width={barWidth}
                    height={standardHeight}
                    className={styles.chartBrandFill}
                  />
                  <rect
                    x={center + 1}
                    y={PLOT.top + plotHeight - thoroughHeight}
                    width={barWidth}
                    height={thoroughHeight}
                    className={styles.chartMutedFill}
                  />
                </g>
              );
            })}
            {labels.map((index) => {
              const point = points[index];
              if (!point) return null;
              const x = PLOT.left + index * groupWidth + groupWidth / 2;
              return (
                <text
                  key={point.bucket_start}
                  x={x}
                  y={HEIGHT - 8}
                  textAnchor="middle"
                  className={styles.chartAxisLabel}
                >
                  {dateLabel(point.bucket_start)}
                </text>
              );
            })}
          </svg>
          <table className="sr-only">
            <caption>Daily search volume data</caption>
            <thead>
              <tr>
                <th>Date</th>
                <th>Standard</th>
                <th>Thorough</th>
              </tr>
            </thead>
            <tbody>
              {points.map((point) => (
                <tr key={point.bucket_start}>
                  <td>{dateLabel(point.bucket_start)}</td>
                  <td>{point.standard}</td>
                  <td>{point.thorough}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

type LatencyKey = "standard_p50_ms" | "thorough_p50_ms" | "overall_p95_ms";

function lineSegments(points: UsageLatencyPoint[], key: LatencyKey, maximum: number): string[] {
  const plotWidth = WIDTH - PLOT.left - PLOT.right;
  const plotHeight = HEIGHT - PLOT.top - PLOT.bottom;
  const segments: string[] = [];
  let current = "";
  points.forEach((point, index) => {
    const value = point[key];
    if (value === null) {
      if (current) segments.push(current);
      current = "";
      return;
    }
    const x = PLOT.left + (index / Math.max(points.length - 1, 1)) * plotWidth;
    const y = PLOT.top + plotHeight - (value / maximum) * plotHeight;
    current += `${current ? " L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`;
  });
  if (current) segments.push(current);
  return segments;
}

function latencyLabel(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(value % 1000 === 0 ? 0 : 1)} s` : `${value} ms`;
}

export function LatencyChart({ points }: { points: UsageLatencyPoint[] }) {
  const values = points
    .flatMap((point) => [point.standard_p50_ms, point.thorough_p50_ms, point.overall_p95_ms])
    .filter((value): value is number => value !== null);
  const maximum = niceMaximum(Math.max(1000, ...values));
  const plotHeight = HEIGHT - PLOT.top - PLOT.bottom;
  const labels = tickIndexes(points.length);
  const series: Array<{ key: LatencyKey; className: string | undefined }> = [
    { key: "standard_p50_ms", className: styles.chartStandardLine },
    { key: "thorough_p50_ms", className: styles.chartThoroughLine },
    { key: "overall_p95_ms", className: styles.chartP95Line },
  ];

  return (
    <div className={styles.chartShell}>
      {values.length === 0 ? (
        <p className={styles.chartEmpty}>No response-time samples in this period.</p>
      ) : (
        <>
          <svg
            viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
            role="img"
            aria-labelledby="latency-title latency-desc"
          >
            <title id="latency-title">Daily response time</title>
            <desc id="latency-desc">
              Median Standard and Thorough response time with overall 95th percentile.
            </desc>
            <g className={styles.chartLegend}>
              <circle cx="40" cy="21" r="4" className={styles.chartBrandFill} />
              <text x="50" y="25">
                Standard
              </text>
              <circle cx="126" cy="21" r="4" className={styles.chartInkFill} />
              <text x="136" y="25">
                Thorough
              </text>
              <line x1="215" x2="232" y1="21" y2="21" className={styles.chartP95Line} />
              <text x="238" y="25">
                P95
              </text>
            </g>
            {[0, 0.5, 1].map((ratio) => {
              const y = PLOT.top + plotHeight * ratio;
              return (
                <g key={ratio}>
                  <line
                    x1={PLOT.left}
                    x2={WIDTH - PLOT.right}
                    y1={y}
                    y2={y}
                    className={styles.chartGrid}
                  />
                  <text
                    x={PLOT.left - 8}
                    y={y + 4}
                    textAnchor="end"
                    className={styles.chartAxisLabel}
                  >
                    {latencyLabel(maximum * (1 - ratio))}
                  </text>
                </g>
              );
            })}
            {series.flatMap((item) =>
              lineSegments(points, item.key, maximum).map((path, index) => (
                <path key={`${item.key}-${index}`} d={path} className={item.className} />
              )),
            )}
            {labels.map((index) => {
              const point = points[index];
              if (!point) return null;
              const x =
                PLOT.left +
                (index / Math.max(points.length - 1, 1)) * (WIDTH - PLOT.left - PLOT.right);
              return (
                <text
                  key={point.bucket_start}
                  x={x}
                  y={HEIGHT - 8}
                  textAnchor="middle"
                  className={styles.chartAxisLabel}
                >
                  {dateLabel(point.bucket_start)}
                </text>
              );
            })}
          </svg>
          <table className="sr-only">
            <caption>Daily response-time data</caption>
            <thead>
              <tr>
                <th>Date</th>
                <th>Standard median</th>
                <th>Thorough median</th>
                <th>P95</th>
                <th>Samples</th>
              </tr>
            </thead>
            <tbody>
              {points.map((point) => (
                <tr key={point.bucket_start}>
                  <td>{dateLabel(point.bucket_start)}</td>
                  <td>{point.standard_p50_ms ?? "No data"}</td>
                  <td>{point.thorough_p50_ms ?? "No data"}</td>
                  <td>{point.overall_p95_ms ?? "No data"}</td>
                  <td>{point.sample_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
