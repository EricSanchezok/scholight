import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";

import { adminApi } from "../api/domain";
import type { AdminOperations } from "../api/types";
import { queryKeys } from "../app/queryKeys";
import { EditorialRowsSkeleton } from "../components/EditorialSkeleton";
import { PageRefreshButton } from "../components/PageRefreshButton";
import { AdminGroupedBarChart } from "../features/admin/AdminGroupedBarChart";
import { formatFullDateTime, formatUtcDay } from "../i18n/format";
import { useI18n } from "../i18n/I18nProvider";
import { styles } from "../styles/classes";

const RANGE_DAYS = 7;
const ISSUE_LIMIT = 20;

function openQueue(queue: AdminOperations["queue"]): number {
  return queue.pending + queue.running + queue.retry + queue.dead;
}

export function AdminOperationsPage() {
  const { locale, messages } = useI18n();
  const operations = useQuery({
    queryKey: queryKeys.adminOperations(RANGE_DAYS, ISSUE_LIMIT),
    queryFn: () => adminApi.operationsOverview(RANGE_DAYS, ISSUE_LIMIT),
  });

  useEffect(() => {
    document.title = messages.titles.adminOperations;
  }, [messages.titles.adminOperations]);

  const data = operations.data;
  return (
    <main className={`${styles.ledgerPage} ${styles.adminPage}`}>
      <header className={`${styles.ledgerHeading} ${styles.pageHeadingAction}`}>
        <div>
          <span className={styles.eyebrow}>Administration</span>
          <h1>Operations</h1>
          <p>Monitor metadata sync, ingestion work, and recoverable failures.</p>
        </div>
        <PageRefreshButton
          label="operations"
          refreshing={operations.isFetching}
          onRefresh={() => operations.refetch()}
        />
      </header>

      {operations.error && !data ? (
        <div className={styles.sectionError} role="alert">
          <span>Operations metrics are temporarily unavailable.</span>
          <button type="button" onClick={() => void operations.refetch()}>
            Retry
          </button>
        </div>
      ) : operations.isPending || !data ? (
        <EditorialRowsSkeleton label="Loading operations" rows={6} />
      ) : (
        <>
          <section className={styles.adminOperationsSummary} aria-label="Operations summary">
            <div>
              <span>Last successful sync</span>
              <strong>
                {data.sync?.last_successful_date
                  ? `Through ${formatUtcDay(data.sync.last_successful_date, locale)}`
                  : "No completed sync"}
              </strong>
              <small>
                {data.sync?.last_succeeded_at
                  ? `Completed ${formatFullDateTime(data.sync.last_succeeded_at, locale)}`
                  : "Waiting for the first successful run"}
              </small>
            </div>
            <div>
              <span>Open queue</span>
              <strong>{openQueue(data.queue).toLocaleString(locale)} papers</strong>
              <small>
                {data.queue.pending.toLocaleString(locale)} pending ·{" "}
                {data.queue.running.toLocaleString(locale)} running ·{" "}
                {data.queue.retry.toLocaleString(locale)} retry
              </small>
            </div>
          </section>

          <section className={styles.adminChartSection} aria-labelledby="admin-intake">
            <div>
              <h2 id="admin-intake">Seven-day intake</h2>
              <p>Discovered papers and completed full-text ingestion by UTC day.</p>
            </div>
            <AdminGroupedBarChart
              title="Seven-day paper intake"
              description="Papers discovered and full-text ingestion completed by UTC day."
              primaryLabel="Discovered"
              secondaryLabel="Full text completed"
              points={data.intake.map((point) => ({
                day: point.day,
                primary: point.discovered,
                secondary: point.full_text_completed,
              }))}
            />
          </section>

          <section className={styles.adminQueue} aria-labelledby="processing-queue">
            <div>
              <h2 id="processing-queue">Processing queue</h2>
              <p>Current ingestion work grouped by state.</p>
            </div>
            <div className={styles.adminQueueLedger}>
              <div>
                <span>Running</span>
                <strong>Full-text ingestion</strong>
                <small>{data.queue.running.toLocaleString(locale)} papers</small>
              </div>
              <div>
                <span>Pending</span>
                <strong>Waiting to start</strong>
                <small>{data.queue.pending.toLocaleString(locale)} papers</small>
              </div>
              <div>
                <span>Retry</span>
                <strong>Scheduled retry</strong>
                <small>{data.queue.retry.toLocaleString(locale)} papers</small>
              </div>
              <div>
                <span>Dead</span>
                <strong>Needs manual review</strong>
                <small>{data.queue.dead.toLocaleString(locale)} papers</small>
              </div>
            </div>
          </section>

          <section className={styles.adminIssues} aria-labelledby="recent-ingestion-issues">
            <div>
              <h2 id="recent-ingestion-issues">Recent issues</h2>
              <p>Retryable and terminal ingestion failures, newest first.</p>
            </div>
            {data.recent_issues.length ? (
              <div className={styles.adminIssueLedger}>
                {data.recent_issues.map((issue) => (
                  <article
                    className={styles.adminIssueRow}
                    key={`${issue.arxiv_id}-${issue.updated_at}`}
                  >
                    <div>
                      <strong>{issue.arxiv_id}</strong>
                      <span>
                        {issue.last_error_message || issue.last_error_code || "Ingestion failed"}
                      </span>
                    </div>
                    <div>
                      <span className={issue.status === "dead" ? styles.adminDead : ""}>
                        {issue.status === "dead" ? "Needs review" : "Retry scheduled"}
                      </span>
                      <small>
                        Attempt {issue.attempt_count.toLocaleString(locale)} of{" "}
                        {issue.max_attempts.toLocaleString(locale)}
                      </small>
                    </div>
                    <time dateTime={issue.next_attempt_at}>
                      {issue.status === "retry"
                        ? formatFullDateTime(issue.next_attempt_at, locale)
                        : formatFullDateTime(issue.updated_at, locale)}
                    </time>
                  </article>
                ))}
              </div>
            ) : (
              <div className={styles.ledgerEmpty}>
                <h3>No recent issues</h3>
                <p>Retryable and terminal ingestion failures will appear here.</p>
              </div>
            )}
          </section>
        </>
      )}
    </main>
  );
}
