import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { usageApi } from "../api/domain";
import { ApiError } from "../api/errors";
import { queryKeys } from "../app/queryKeys";
import { LatencyChart, VolumeChart } from "../features/usage/UsageCharts";
import styles from "../styles/app.module.css";

function seconds(value: number | null): string {
  return value === null ? "—" : `${(value / 1000).toFixed(2)} s`;
}

function usageTime(value: string): string {
  return new Intl.DateTimeFormat("en", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function SectionError({ error, retry }: { error: Error; retry: () => void }) {
  return (
    <div className={styles.sectionError} role="alert">
      <span>
        {error instanceof ApiError ? error.message : "Usage data is temporarily unavailable."}
      </span>
      <button type="button" onClick={retry}>
        Retry
      </button>
    </div>
  );
}

export function UsagePage() {
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState("");
  const summary = useQuery({
    queryKey: queryKeys.usageSummary,
    queryFn: usageApi.summary,
    staleTime: 60_000,
    retry: false,
  });
  const volume = useQuery({
    queryKey: queryKeys.usageVolume,
    queryFn: usageApi.volume,
    staleTime: 60_000,
    retry: false,
  });
  const latency = useQuery({
    queryKey: queryKeys.usageLatency,
    queryFn: usageApi.latency,
    staleTime: 60_000,
    retry: false,
  });
  const records = useInfiniteQuery({
    queryKey: queryKeys.usageRecords,
    queryFn: ({ pageParam }) => usageApi.records(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    staleTime: 60_000,
    retry: false,
  });
  const items = records.data?.pages.flatMap((page) => page.items) ?? [];

  const downloadCsv = async () => {
    setExporting(true);
    setExportError("");
    try {
      const blob = await usageApi.exportCsv();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "scholight-usage.csv";
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      setExportError(error instanceof ApiError ? error.message : "Unable to export usage data.");
    } finally {
      setExporting(false);
    }
  };

  return (
    <main className={styles.ledgerPage}>
      <header className={styles.ledgerHeading}>
        <h1>Usage &amp; quota</h1>
        <p>Understand how your research activity uses today’s allowance.</p>
      </header>

      <section className={styles.quotaSection} aria-labelledby="today-usage">
        <div className={styles.sectionHeading}>
          <h2 id="today-usage">Today</h2>
          <span>
            {summary.data
              ? `Resets at ${new Intl.DateTimeFormat("en", { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: "UTC", timeZoneName: "short" }).format(new Date(summary.data.reset_at))}`
              : "UTC daily allowance"}
          </span>
        </div>
        {summary.error ? (
          <SectionError error={summary.error} retry={() => void summary.refetch()} />
        ) : summary.data ? (
          <div className={styles.quotaMetrics}>
            {(["standard", "thorough"] as const).map((strength) => {
              const quota = summary.data.today[strength];
              const percent =
                quota.daily_limit > 0 ? Math.min(100, (quota.used / quota.daily_limit) * 100) : 0;
              return (
                <div className={styles.quotaMetric} key={strength}>
                  <span className={strength === "standard" ? styles.brandLabel : styles.mutedLabel}>
                    {strength.toUpperCase()} SEARCH
                  </span>
                  <strong>
                    {quota.daily_limit > 0 ? `${quota.used} / ${quota.daily_limit}` : "Unavailable"}
                  </strong>
                  <p>
                    {quota.daily_limit > 0
                      ? `${quota.remaining} searches remaining today`
                      : "No daily allowance is configured."}
                  </p>
                  <div className={styles.quotaProgress}>
                    <span style={{ width: `${percent}%` }} />
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <div className={styles.sectionLoading}>Loading today’s usage…</div>
        )}
      </section>

      <section className={styles.performanceGrid} aria-label="Usage summary">
        {summary.data ? (
          <>
            <div>
              <span>SEARCHES TODAY</span>
              <strong>{summary.data.searches_today}</strong>
              <p>
                {summary.data.today.standard.used} Standard · {summary.data.today.thorough.used}{" "}
                Thorough
              </p>
            </div>
            <div>
              <span>THIS MONTH</span>
              <strong>{summary.data.searches_this_month}</strong>
              <p>Across web and access keys</p>
            </div>
            <div>
              <span>TYPICAL RESPONSE TIME</span>
              <strong>{seconds(summary.data.typical_response_ms)}</strong>
              <p>Median · p95 {seconds(summary.data.p95_response_ms)}</p>
            </div>
            <div>
              <span>SUCCESS RATE</span>
              <strong>
                {summary.data.success_rate === null
                  ? "—"
                  : `${(summary.data.success_rate * 100).toFixed(1)}%`}
              </strong>
              <p>
                {summary.data.degraded_count} degraded · {summary.data.failed_count} failed
              </p>
            </div>
          </>
        ) : (
          <p className={styles.sectionLoading}>
            Summary metrics will appear when usage is available.
          </p>
        )}
      </section>

      <section className={styles.analyticsGrid} aria-label="Usage analytics">
        <div className={styles.chartFigure}>
          <h2>Search volume</h2>
          <p>Daily searches · last 30 days</p>
          {volume.error ? (
            <SectionError error={volume.error} retry={() => void volume.refetch()} />
          ) : volume.data ? (
            <VolumeChart points={volume.data.points} />
          ) : (
            <div className={styles.sectionLoading}>Loading search volume…</div>
          )}
        </div>
        <div className={styles.chartFigure}>
          <h2>Response time</h2>
          <p>Server search time · median and p95</p>
          {latency.error ? (
            <SectionError error={latency.error} retry={() => void latency.refetch()} />
          ) : latency.data ? (
            <LatencyChart points={latency.data.points} />
          ) : (
            <div className={styles.sectionLoading}>Loading response time…</div>
          )}
        </div>
      </section>

      <section className={styles.recentUsage} aria-labelledby="recent-usage">
        <div className={styles.recentUsageHeading}>
          <h2 id="recent-usage">Recent usage</h2>
          <button type="button" disabled={exporting} onClick={() => void downloadCsv()}>
            {exporting ? "Exporting…" : "Export CSV"}
          </button>
        </div>
        {exportError && (
          <p className={styles.formMessageError} role="alert">
            {exportError}
          </p>
        )}
        {records.error ? (
          <SectionError error={records.error} retry={() => void records.refetch()} />
        ) : records.isPending ? (
          <div className={styles.sectionLoading}>Loading recent usage…</div>
        ) : items.length === 0 ? (
          <div className={styles.ledgerEmpty}>
            <h3>No usage yet</h3>
            <p>Signed-in web and access-key searches will appear here.</p>
          </div>
        ) : (
          <div className={styles.usageTableWrap}>
            <table className={styles.usageTable}>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Source</th>
                  <th>Strength</th>
                  <th>Response</th>
                  <th>Results</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <time dateTime={item.created_at}>{usageTime(item.created_at)}</time>
                    </td>
                    <td>
                      {item.actor_type === "access_key"
                        ? `Access key · ${item.access_key?.name ?? `••••${item.access_key?.last4 ?? ""}`}`
                        : "Web · signed in"}
                    </td>
                    <td>{item.strength === "thorough" ? "Thorough" : "Standard"}</td>
                    <td>{seconds(item.search_duration_ms)}</td>
                    <td>{item.result_count ?? "—"}</td>
                    <td>
                      <span
                        className={
                          styles[
                            `usageStatus${item.outcome[0]?.toUpperCase()}${item.outcome.slice(1)}`
                          ]
                        }
                      >
                        {item.outcome === "success"
                          ? "Succeeded"
                          : item.outcome[0]?.toUpperCase() + item.outcome.slice(1)}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {records.hasNextPage && (
          <button
            className={styles.loadMoreButton}
            type="button"
            disabled={records.isFetchingNextPage}
            onClick={() => void records.fetchNextPage()}
          >
            {records.isFetchingNextPage ? "Loading…" : "Load more"}
          </button>
        )}
      </section>
    </main>
  );
}
