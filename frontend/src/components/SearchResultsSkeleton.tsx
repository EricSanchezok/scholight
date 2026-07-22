import { useReducedMotion } from "motion/react";
import * as m from "motion/react-m";

import { searchActivityMotion } from "../app/motion";
import { styles } from "../styles/classes";
import { SkeletonPulse } from "./EditorialSkeleton";

export function SearchResultsSkeleton() {
  const reduceMotion = useReducedMotion();
  return (
    <div className={styles.searchLoading} data-testid="search-results-skeleton">
      <div className={styles.searchLoadingHeading} role="status" aria-live="polite">
        <span>Searching the literature…</span>
        <div className={styles.searchActivityTrack} aria-hidden="true">
          <m.span {...searchActivityMotion(reduceMotion)} />
        </div>
      </div>
      <SkeletonPulse label="Loading search results">
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
