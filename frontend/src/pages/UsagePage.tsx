import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import { useState } from "react";

import { usageApi } from "../api/domain";
import { ApiError } from "../api/errors";
import { queryKeys } from "../app/queryKeys";
import { EditorialRowsSkeleton, SkeletonPulse } from "../components/EditorialSkeleton";
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

function Reveal({ children, name }: { children: React.ReactNode; name: string }) {
  return (
    <m.div
      key={name}
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0, transition: { duration: 0.16 } }}
      exit={{ opacity: 0, transition: { duration: 0.08 } }}
    >
      {children}
    </m.div>
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
        <AnimatePresence initial={false} mode="popLayout">
          {summary.error && !summary.data ? (
            <SectionError error={summary.error} retry={() => void summary.refetch()} />
          ) : summary.data ? (
            <Reveal name="quota-content">
              <div className={styles.quotaMetrics}>
                {(["standard", "thorough"] as const).map((strength) => {
                  const quota = summary.data.today[strength];
                  const percent =
                    quota.daily_limit > 0
                      ? Math.min(100, (quota.used / quota.daily_limit) * 100)
                      : 0;
                  return (
                    <div className={styles.quotaMetric} key={strength}>
                      <span
                        className={strength === "standard" ? styles.brandLabel : styles.mutedLabel}
                      >
                        {strength.toUpperCase()} SEARCH
                      </span>
                      <strong>
                        {quota.daily_limit > 0
                          ? `${quota.used} / ${quota.daily_limit}`
                          : "Unavailable"}
                      </strong>
                      <p>
                        {quota.daily_limit > 0
                          ? `${quota.remaining} searches remaining today`
                          : "No daily allowance is configured."}
                      </p>
                      <div
                        className={styles.quotaProgress}
                        role="progressbar"
                        aria-valuenow={quota.used}
                        aria-valuemin={0}
                        aria-valuemax={quota.daily_limit}
                        aria-label={`${strength} quota used`}
                      >
                        <m.span
                          initial={{ width: 0 }}
                          animate={{ width: `${percent}%` }}
                          transition={{ duration: 0.28 }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            </Reveal>
          ) : (
            <SkeletonPulse label="Loading today’s usage">
              <div className={styles.skeletonQuota}>
                <span />
                <span />
              </div>
            </SkeletonPulse>
          )}
        </AnimatePresence>
        {summary.error && summary.data && (
          <SectionError error={summary.error} retry={() => void summary.refetch()} />
        )}
      </section>

      <section className={styles.performanceGrid} aria-label="Usage summary">
        {summary.data ? (
          <>
            <m.div
              initial={{ opacity: 0, y: 3 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.16 }}
            >
              <span>SEARCHES TODAY</span>
              <strong>{summary.data.searches_today}</strong>
              <p>
                {summary.data.today.standard.used} Standard · {summary.data.today.thorough.used}{" "}
                Thorough
              </p>
            </m.div>
            <m.div
              initial={{ opacity: 0, y: 3 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.16, delay: 0.02 }}
            >
              <span>THIS MONTH</span>
              <strong>{summary.data.searches_this_month}</strong>
              <p>Across web and access keys</p>
            </m.div>
            <m.div
              initial={{ opacity: 0, y: 3 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.16, delay: 0.04 }}
            >
              <span>TYPICAL RESPONSE TIME</span>
              <strong>{seconds(summary.data.typical_response_ms)}</strong>
              <p>Median · p95 {seconds(summary.data.p95_response_ms)}</p>
            </m.div>
            <m.div
              initial={{ opacity: 0, y: 3 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.16, delay: 0.06 }}
            >
              <span>SUCCESS RATE</span>
              <strong>
                {summary.data.success_rate === null
                  ? "—"
                  : `${(summary.data.success_rate * 100).toFixed(1)}%`}
              </strong>
              <p>
                {summary.data.degraded_count} degraded · {summary.data.failed_count} failed
              </p>
            </m.div>
          </>
        ) : (
          <SkeletonPulse label="Loading usage summary" className={styles.performanceSkeleton}>
            <div className={styles.skeletonMetricGrid}>
              {Array.from({ length: 4 }, (_, index) => (
                <span key={index} />
              ))}
            </div>
          </SkeletonPulse>
        )}
      </section>

      <section className={styles.analyticsGrid} aria-label="Usage analytics">
        <div className={styles.chartFigure}>
          <h2>Search volume</h2>
          <p>Daily searches · last 30 days</p>
          <AnimatePresence initial={false} mode="popLayout">
            {volume.error && !volume.data ? (
              <SectionError error={volume.error} retry={() => void volume.refetch()} />
            ) : volume.data ? (
              <Reveal name="volume-chart">
                <VolumeChart points={volume.data.points} />
              </Reveal>
            ) : (
              <SkeletonPulse label="Loading search volume" className={styles.chartSkeleton}>
                <span />
              </SkeletonPulse>
            )}
          </AnimatePresence>
        </div>
        <div className={styles.chartFigure}>
          <h2>Response time</h2>
          <p>Server search time · median and p95</p>
          <AnimatePresence initial={false} mode="popLayout">
            {latency.error && !latency.data ? (
              <SectionError error={latency.error} retry={() => void latency.refetch()} />
            ) : latency.data ? (
              <Reveal name="latency-chart">
                <LatencyChart points={latency.data.points} />
              </Reveal>
            ) : (
              <SkeletonPulse label="Loading response time" className={styles.chartSkeleton}>
                <span />
              </SkeletonPulse>
            )}
          </AnimatePresence>
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
        {records.error && !records.data ? (
          <SectionError error={records.error} retry={() => void records.refetch()} />
        ) : records.isPending ? (
          <EditorialRowsSkeleton label="Loading recent usage" rows={3} />
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
                {items.map((item, index) => (
                  <m.tr
                    key={item.id}
                    initial={{ opacity: 0, y: 4 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.16, delay: Math.min(index * 0.02, 0.16) }}
                  >
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
                  </m.tr>
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
