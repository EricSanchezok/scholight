import { useReducedMotion } from "motion/react";
import * as m from "motion/react-m";

import styles from "../styles/app.module.css";
import { SkeletonPulse } from "./EditorialSkeleton";

export function SearchResultsSkeleton() {
  const reduceMotion = useReducedMotion();
  return (
    <div className={styles.searchLoading} data-testid="search-results-skeleton">
      <div className={styles.searchLoadingHeading} role="status" aria-live="polite">
        <span>Searching the literature…</span>
        <div className={styles.searchActivityTrack} aria-hidden="true">
          <m.span
            animate={
              reduceMotion
                ? { opacity: 0.7, scaleX: 0.32 }
                : { opacity: [0.45, 1, 0.45], scaleX: [0.12, 0.82, 0.12] }
            }
            transition={
              reduceMotion
                ? { duration: 0 }
                : { duration: 1.8, repeat: Infinity, ease: "easeInOut" }
            }
          />
        </div>
      </div>
      <SkeletonPulse label="Loading search results" className={styles.searchSkeletonPulse}>
        <div className={styles.searchSkeletonList}>
          {Array.from({ length: 4 }, (_, index) => (
            <div className={styles.searchSkeletonRow} key={index}>
              <span className={styles.searchSkeletonTitle} />
              <span className={styles.searchSkeletonMeta} />
              <span className={styles.searchSkeletonAbstract} />
              <span className={styles.searchSkeletonAbstractShort} />
            </div>
          ))}
        </div>
      </SkeletonPulse>
    </div>
  );
}
