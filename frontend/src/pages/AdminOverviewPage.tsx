import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";

import { adminApi } from "../api/domain";
import { queryKeys } from "../app/queryKeys";
import { PageRefreshButton } from "../components/PageRefreshButton";
import { AdminGroupedBarChart } from "../features/admin/AdminGroupedBarChart";
import { useI18n } from "../i18n/I18nProvider";
import { styles } from "../styles/classes";

const RANGE_DAYS = 30;

function count(value: number, locale: string): string {
  return value.toLocaleString(locale);
}

export function AdminOverviewPage() {
  const { locale, messages } = useI18n();
  const analytics = useQuery({
    queryKey: queryKeys.adminAnalytics(RANGE_DAYS),
    queryFn: () => adminApi.analyticsOverview(RANGE_DAYS),
  });

  useEffect(() => {
    document.title = messages.titles.adminOverview;
  }, [messages.titles.adminOverview]);

  const data = analytics.data;
  return (
    <main className={`${styles.ledgerPage} ${styles.adminPage}`}>
      <header className={`${styles.ledgerHeading} ${styles.pageHeadingAction}`}>
        <div>
          <span className={styles.eyebrow}>Administration</span>
          <h1>Overview</h1>
          <p>A product-level view of Scholight users, search activity, and access keys.</p>
        </div>
        <PageRefreshButton
          label="administration overview"
          refreshing={analytics.isFetching}
          onRefresh={() => analytics.refetch()}
        />
      </header>

      {analytics.error && !data ? (
        <div className={styles.sectionError} role="alert">
          <span>Product analytics are temporarily unavailable.</span>
          <button type="button" onClick={() => void analytics.refetch()}>
            Retry
          </button>
        </div>
      ) : analytics.isPending || !data ? (
        <div className={styles.adminOverviewSkeleton} aria-label="Loading administration overview">
          <span />
          <span />
          <span />
          <span />
        </div>
      ) : (
        <>
          <section className={styles.adminSummaryLedger} aria-label="Product summary">
            <div>
              <span>Total product users</span>
              <strong>{count(data.profiles.total, locale)}</strong>
              <small>
                {count(data.profiles.created_in_period, locale)} added in the last 30 days
              </small>
            </div>
            <div>
              <span>Active profiles</span>
              <strong>{count(data.profiles.active, locale)}</strong>
              <small>{count(data.profiles.blocked, locale)} blocked</small>
            </div>
            <div>
              <span>Product admins</span>
              <strong>{count(data.profiles.admins, locale)}</strong>
              <small>Scholight administrators</small>
            </div>
            <div>
              <span>Searches · 30 days</span>
              <strong>{count(data.searches.total, locale)}</strong>
              <small>
                {count(data.searches.authenticated, locale)} signed in ·{" "}
                {count(data.searches.anonymous, locale)} anonymous
              </small>
            </div>
          </section>

          <section className={styles.adminChartSection} aria-labelledby="admin-search-activity">
            <div>
              <h2 id="admin-search-activity">Search activity</h2>
              <p>Signed-in and anonymous searches by UTC day.</p>
            </div>
            <AdminGroupedBarChart
              title="Daily Scholight search activity"
              description="Signed-in and anonymous searches for the selected 30-day period."
              primaryLabel="Signed in"
              secondaryLabel="Anonymous"
              points={data.daily.map((point) => ({
                day: point.day,
                primary: point.authenticated,
                secondary: point.anonymous,
              }))}
            />
          </section>

          <section className={styles.adminBreakdown} aria-labelledby="admin-search-breakdown">
            <h2 id="admin-search-breakdown">Search and access breakdown</h2>
            <div className={styles.adminBreakdownGrid}>
              <div>
                <span>Standard</span>
                <strong>{count(data.searches.standard, locale)} searches</strong>
              </div>
              <div>
                <span>Thorough</span>
                <strong>{count(data.searches.thorough, locale)} searches</strong>
              </div>
              <div>
                <span>Signed-in REST</span>
                <strong>{count(data.searches.authenticated_rest, locale)} searches</strong>
              </div>
              <div>
                <span>Signed-in MCP</span>
                <strong>{count(data.searches.authenticated_mcp, locale)} searches</strong>
              </div>
              <div>
                <span>Access keys</span>
                <strong>
                  {count(data.access_keys.active, locale)} active of{" "}
                  {count(data.access_keys.total, locale)}
                </strong>
              </div>
              <div>
                <span>Used in period</span>
                <strong>{count(data.access_keys.used_in_period, locale)} keys</strong>
              </div>
            </div>
          </section>
        </>
      )}
    </main>
  );
}
