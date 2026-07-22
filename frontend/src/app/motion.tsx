import { AnimatePresence, LazyMotion, MotionConfig } from "motion/react";
import * as m from "motion/react-m";
import { Suspense } from "react";
import { Outlet, useLocation } from "react-router-dom";

import { RouteSkeleton } from "../components/EditorialSkeleton";
import styles from "../styles/app.module.css";

export const motionEase = [0.22, 1, 0.36, 1] as const;
export const motionDuration = {
  quick: 0.1,
  standard: 0.18,
  slow: 0.28,
} as const;

export const popoverMotion = {
  initial: { opacity: 0, y: -4, scale: 0.985 },
  animate: { opacity: 1, y: 0, scale: 1, transition: { duration: 0.14, ease: motionEase } },
  exit: { opacity: 0, y: -3, scale: 0.99, transition: { duration: 0.09, ease: motionEase } },
} as const;

export const dialogSurfaceMotion = {
  initial: { opacity: 0, y: 4, scale: 0.985 },
  animate: { opacity: 1, y: 0, scale: 1, transition: { duration: 0.18, ease: motionEase } },
  exit: { opacity: 0, y: 2, scale: 0.99, transition: { duration: 0.12, ease: motionEase } },
} as const;

const loadMotionFeatures = () => import("./motionFeatures").then((module) => module.default);

export function ScholightMotionProvider({ children }: { children: React.ReactNode }) {
  return (
    <LazyMotion features={loadMotionFeatures} strict>
      <MotionConfig reducedMotion="user" transition={{ ease: motionEase }}>
        {children}
      </MotionConfig>
    </LazyMotion>
  );
}

export function AnimatedOutlet() {
  const location = useLocation();
  return (
    <div className={styles.routeStage}>
      <AnimatePresence initial={false} mode="wait">
        <m.div
          className={styles.routeScene}
          key={location.pathname}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0, transition: { duration: motionDuration.standard } }}
          exit={{ opacity: 0, y: -2, transition: { duration: 0.09 } }}
        >
          <Suspense fallback={<RouteSkeleton pathname={location.pathname} />}>
            <Outlet />
          </Suspense>
        </m.div>
      </AnimatePresence>
    </div>
  );
}
