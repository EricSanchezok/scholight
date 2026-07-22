import { useReducedMotion } from "motion/react";
import * as m from "motion/react-m";

import { skeletonPulseMotion } from "../app/motion";
import { styles } from "../styles/classes";
import { accountRouteFor, routes } from "../app/routes";

export function SkeletonPulse({
  children,
  label,
  className = "",
}: {
  children: React.ReactNode;
  label: string;
  className?: string;
}) {
  const reduceMotion = useReducedMotion();
  return (
    <m.div
      className={`${styles.skeletonPulse} ${className}`}
      aria-busy="true"
      aria-label={label}
      role="status"
      {...skeletonPulseMotion(reduceMotion)}
    >
      <span className="sr-only">{label}</span>
      <div aria-hidden="true">{children}</div>
    </m.div>
  );
}

function Lines({ rows = 3 }: { rows?: number }) {
  return (
    <div className={styles.skeletonLines}>
      {Array.from({ length: rows }, (_, index) => (
        <span key={index} />
      ))}
    </div>
  );
}

export function EditorialRowsSkeleton({
  label,
  rows = 3,
  className = "",
}: {
  label: string;
  rows?: number;
  className?: string;
}) {
  return (
    <SkeletonPulse label={label} className={className}>
      <Lines rows={rows} />
    </SkeletonPulse>
  );
}

function UsageSkeleton() {
  return (
    <main className={styles.ledgerPage} data-testid="usage-route-skeleton">
      <header className={styles.ledgerHeading}>
        <h1>Usage &amp; quota</h1>
        <p>Understand how your research activity uses today’s allowance.</p>
      </header>
      <SkeletonPulse label="Loading usage and quota">
        <div className={styles.skeletonQuota}>
          <span />
          <span />
        </div>
        <div className={styles.skeletonMetricGrid}>
          {Array.from({ length: 4 }, (_, index) => (
            <span key={index} />
          ))}
        </div>
        <div className={styles.skeletonCharts}>
          <span />
          <span />
        </div>
        <Lines rows={3} />
      </SkeletonPulse>
    </main>
  );
}

function LedgerSkeleton({ pathname }: { pathname: string }) {
  const route = accountRouteFor(pathname);
  const isHistory = route?.id === "history";
  const title = isHistory
    ? "Search history"
    : route?.id === "account"
      ? "Account settings"
      : "Access keys";
  const intro = isHistory
    ? "Revisit your previous research questions or remove the searches you no longer need."
    : route?.id === "account"
      ? "Manage your profile, password, sessions, and account."
      : "Create keys for tools and agents that search Scholight on your behalf.";
  return (
    <main
      className={
        isHistory
          ? styles.historyPage
          : route?.id === "account"
            ? styles.accountPage
            : styles.ledgerPage
      }
      data-testid="private-route-skeleton"
    >
      <header className={isHistory ? styles.historyHeading : styles.ledgerHeading}>
        <h1>{title}</h1>
        <p>{intro}</p>
      </header>
      <SkeletonPulse label={`Loading ${title.toLowerCase()}`}>
        <div className={isHistory ? styles.skeletonToolbar : styles.skeletonLedgerHeading} />
        <Lines rows={route?.id === "account" ? 6 : 4} />
      </SkeletonPulse>
    </main>
  );
}

export function RouteSkeleton({ pathname }: { pathname: string }) {
  return pathname === routes.usage.path ? (
    <UsageSkeleton />
  ) : (
    <LedgerSkeleton pathname={pathname} />
  );
}
