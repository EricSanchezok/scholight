import { formatUtcDay } from "../../i18n/format";
import { useI18n } from "../../i18n/I18nProvider";
import { styles } from "../../styles/classes";

const WIDTH = 980;
const HEIGHT = 270;
const PLOT = { left: 18, right: 18, top: 48, bottom: 38 };

interface AdminChartPoint {
  day: string;
  primary: number;
  secondary: number;
}

function niceMaximum(value: number): number {
  if (value <= 1) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  return Math.ceil(value / magnitude) * magnitude;
}

function labelIndexes(length: number): Set<number> {
  if (length <= 7) return new Set(Array.from({ length }, (_, index) => index));
  return new Set([0, Math.floor((length - 1) / 2), length - 1]);
}

export function AdminGroupedBarChart({
  title,
  description,
  primaryLabel,
  secondaryLabel,
  points,
}: {
  title: string;
  description: string;
  primaryLabel: string;
  secondaryLabel: string;
  points: AdminChartPoint[];
}) {
  const { locale } = useI18n();
  const plotWidth = WIDTH - PLOT.left - PLOT.right;
  const plotHeight = HEIGHT - PLOT.top - PLOT.bottom;
  const maximum = niceMaximum(
    Math.max(1, ...points.flatMap((point) => [point.primary, point.secondary])),
  );
  const groupWidth = plotWidth / Math.max(points.length, 1);
  const barWidth = Math.max(3, Math.min(18, groupWidth * 0.28));
  const labels = labelIndexes(points.length);
  const chartId = title.toLowerCase().replaceAll(/[^a-z0-9]+/g, "-");

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
          >
            <title id={`${chartId}-title`}>{title}</title>
            <desc id={`${chartId}-description`}>{description}</desc>
            <g className={styles.chartLegend}>
              <circle cx="4" cy="18" r="4" className={styles.chartBrandFill} />
              <text x="14" y="22">
                {primaryLabel}
              </text>
              <circle cx="116" cy="18" r="4" className={styles.chartMutedFill} />
              <text x="126" y="22">
                {secondaryLabel}
              </text>
            </g>
            <line
              x1={PLOT.left}
              x2={WIDTH - PLOT.right}
              y1={PLOT.top + plotHeight}
              y2={PLOT.top + plotHeight}
              className={styles.chartGrid}
            />
            {points.map((point, index) => {
              const center = PLOT.left + index * groupWidth + groupWidth / 2;
              const primaryHeight = (point.primary / maximum) * plotHeight;
              const secondaryHeight = (point.secondary / maximum) * plotHeight;
              return (
                <g key={point.day}>
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
                  {labels.has(index) && (
                    <text
                      x={center}
                      y={HEIGHT - 8}
                      textAnchor="middle"
                      className={styles.chartAxisLabel}
                    >
                      {formatUtcDay(point.day, locale)}
                    </text>
                  )}
                </g>
              );
            })}
          </svg>
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
