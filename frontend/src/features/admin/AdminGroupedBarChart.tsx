import { useState } from "react";

import { formatUtcDay } from "../../i18n/format";
import { useI18n } from "../../i18n/I18nProvider";
import { styles } from "../../styles/classes";

const WIDTH = 980;
const HEIGHT = 330;
const PLOT = { left: 64, right: 18, top: 72, bottom: 44 };

interface AdminChartPoint {
  day: string;
  primary: number;
  secondary: number;
}

interface ChartScale {
  maximum: number;
  ticks: number[];
}

function niceScale(value: number): ChartScale {
  const maximumValue = Math.max(1, value);
  const roughStep = maximumValue / 4;
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const normalized = roughStep / magnitude;
  const multiplier = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  const step = Math.max(1, multiplier * magnitude);
  const maximum = Math.ceil(maximumValue / step) * step;
  const ticks = Array.from({ length: Math.round(maximum / step) + 1 }, (_, index) => {
    return index * step;
  });
  return { maximum, ticks };
}

function dateTickIndexes(length: number): Set<number> {
  if (length <= 7) return new Set(Array.from({ length }, (_, index) => index));
  const tickCount = 5;
  return new Set(
    Array.from({ length: tickCount }, (_, index) => {
      return Math.round((index * (length - 1)) / (tickCount - 1));
    }),
  );
}

function lastActiveIndex(points: AdminChartPoint[]): number {
  for (let index = points.length - 1; index >= 0; index -= 1) {
    const point = points[index];
    if (point && (point.primary > 0 || point.secondary > 0)) return index;
  }
  return Math.max(points.length - 1, 0);
}

function compactCount(value: number, locale: string): string {
  if (Math.abs(value) < 1000) return value.toLocaleString(locale);
  return new Intl.NumberFormat(locale, {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

export function AdminGroupedBarChart({
  title,
  description,
  primaryLabel,
  secondaryLabel,
  valueLabel,
  points,
}: {
  title: string;
  description: string;
  primaryLabel: string;
  secondaryLabel: string;
  valueLabel: string;
  points: AdminChartPoint[];
}) {
  const { locale } = useI18n();
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const plotWidth = WIDTH - PLOT.left - PLOT.right;
  const plotHeight = HEIGHT - PLOT.top - PLOT.bottom;
  const scale = niceScale(
    Math.max(1, ...points.flatMap((point) => [point.primary, point.secondary])),
  );
  const groupWidth = plotWidth / Math.max(points.length, 1);
  const barWidth = Math.max(3, Math.min(18, groupWidth * 0.28));
  const labels = dateTickIndexes(points.length);
  const chartId = title.toLowerCase().replaceAll(/[^a-z0-9]+/g, "-");
  const inspectedIndex = selectedIndex ?? lastActiveIndex(points);
  const inspectedPoint = points[inspectedIndex];
  const showDirectValues = points.length <= 7;
  const exactCount = (value: number) => value.toLocaleString(locale);
  const inspectedLabel = inspectedPoint
    ? `${formatUtcDay(inspectedPoint.day, locale)} · ${primaryLabel} ${exactCount(
        inspectedPoint.primary,
      )} · ${secondaryLabel} ${exactCount(inspectedPoint.secondary)}`
    : "";

  return (
    <div className={styles.adminChart}>
      {points.length === 0 ? (
        <p className={styles.chartEmpty}>No activity in this period.</p>
      ) : (
        <>
          <svg
            viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
            role="img"
            aria-labelledby={`${chartId}-title ${chartId}-description`}
            onMouseLeave={() => setSelectedIndex(null)}
          >
            <title id={`${chartId}-title`}>{title}</title>
            <desc id={`${chartId}-description`}>{description}</desc>
            <g className={styles.chartLegend}>
              <circle cx={PLOT.left} cy="24" r="4" className={styles.chartBrandFill} />
              <text x={PLOT.left + 10} y="28">
                {primaryLabel}
              </text>
              <circle cx={PLOT.left + 116} cy="24" r="4" className={styles.chartMutedFill} />
              <text x={PLOT.left + 126} y="28">
                {secondaryLabel}
              </text>
            </g>
            <text x={PLOT.left} y={PLOT.top - 14} className={styles.adminChartUnit}>
              {valueLabel}
            </text>
            {scale.ticks.map((value) => {
              const y = PLOT.top + plotHeight - (value / scale.maximum) * plotHeight;
              return (
                <g key={value}>
                  <line
                    x1={PLOT.left}
                    x2={WIDTH - PLOT.right}
                    y1={y}
                    y2={y}
                    className={styles.chartGrid}
                  />
                  <text
                    data-axis-tick={value}
                    x={PLOT.left - 10}
                    y={y + 4}
                    textAnchor="end"
                    className={styles.chartAxisLabel}
                  >
                    {compactCount(value, locale)}
                  </text>
                </g>
              );
            })}
            {points.map((point, index) => {
              const center = PLOT.left + index * groupWidth + groupWidth / 2;
              const primaryHeight = (point.primary / scale.maximum) * plotHeight;
              const secondaryHeight = (point.secondary / scale.maximum) * plotHeight;
              const date = formatUtcDay(point.day, locale);
              const ariaLabel = `${date}: ${primaryLabel} ${exactCount(
                point.primary,
              )}; ${secondaryLabel} ${exactCount(point.secondary)}`;
              return (
                <g key={point.day}>
                  {index === inspectedIndex && (
                    <rect
                      x={PLOT.left + index * groupWidth}
                      y={PLOT.top}
                      width={groupWidth}
                      height={plotHeight}
                      className={styles.adminChartActiveBand}
                    />
                  )}
                  <rect
                    x={center - barWidth - 2}
                    y={PLOT.top + plotHeight - primaryHeight}
                    width={barWidth}
                    height={primaryHeight}
                    className={styles.chartBrandFill}
                  />
                  <rect
                    x={center + 2}
                    y={PLOT.top + plotHeight - secondaryHeight}
                    width={barWidth}
                    height={secondaryHeight}
                    className={styles.chartMutedFill}
                  />
                  {showDirectValues && point.primary > 0 && (
                    <text
                      x={center - barWidth / 2 - 2}
                      y={Math.max(PLOT.top + 11, PLOT.top + plotHeight - primaryHeight - 7)}
                      textAnchor="middle"
                      className={styles.adminChartValue}
                    >
                      {compactCount(point.primary, locale)}
                    </text>
                  )}
                  {showDirectValues && point.secondary > 0 && (
                    <text
                      x={center + barWidth / 2 + 2}
                      y={Math.max(PLOT.top + 11, PLOT.top + plotHeight - secondaryHeight - 7)}
                      textAnchor="middle"
                      className={styles.adminChartValue}
                    >
                      {compactCount(point.secondary, locale)}
                    </text>
                  )}
                  {labels.has(index) && (
                    <text
                      x={center}
                      y={HEIGHT - 12}
                      textAnchor="middle"
                      className={styles.chartAxisLabel}
                      data-date-tick
                    >
                      {date}
                    </text>
                  )}
                  <rect
                    x={PLOT.left + index * groupWidth}
                    y={PLOT.top}
                    width={groupWidth}
                    height={plotHeight}
                    className={styles.adminChartHitArea}
                    role="button"
                    tabIndex={0}
                    aria-label={ariaLabel}
                    onMouseEnter={() => setSelectedIndex(index)}
                    onFocus={() => setSelectedIndex(index)}
                    onBlur={() => setSelectedIndex(null)}
                  >
                    <title>{ariaLabel}</title>
                  </rect>
                </g>
              );
            })}
          </svg>
          <p className={styles.adminChartReadout} aria-live="polite">
            {inspectedLabel}
          </p>
          <table className="sr-only">
            <caption>{title}</caption>
            <thead>
              <tr>
                <th>Date</th>
                <th>{primaryLabel}</th>
                <th>{secondaryLabel}</th>
              </tr>
            </thead>
            <tbody>
              {points.map((point) => (
                <tr key={point.day}>
                  <td>{formatUtcDay(point.day, locale)}</td>
                  <td>{point.primary}</td>
                  <td>{point.secondary}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
